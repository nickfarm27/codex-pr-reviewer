#!/usr/bin/env python3
"""Track evidence-backed improvements to the local PR review workflow."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
import review_queue  # noqa: E402


DEFAULT_STATE = ROOT / ".state" / "reviews.db"
DEFAULT_CONFIG = ROOT / "config.json"
LOCAL_TIMEZONE = ZoneInfo("Asia/Kuala_Lumpur")
OVERLAP = timedelta(minutes=5)
MAX_PROPOSALS = 3
CATEGORIES = {
    "clarity",
    "evidence",
    "coverage",
    "calibration",
    "action_friction",
    "state_continuity",
    "reliability",
    "preference",
}
IMPACTS = {"low", "medium", "high"}
TARGET_LAYERS = {
    "prompt",
    "workflow_code",
    "skill",
    "state_model",
    "documentation",
    "no_change",
}
class CalibrationError(review_queue.QueueError):
    pass


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except FileNotFoundError as exc:
        raise CalibrationError(f"File not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise CalibrationError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise CalibrationError(f"Expected a JSON object in {path}")
    return value


def fingerprint(value: dict[str, Any], fields: tuple[str, ...]) -> str:
    normalized = {field: value.get(field) for field in fields}
    encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def parse_iso(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CalibrationError(f"Invalid ISO timestamp: {value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=LOCAL_TIMEZONE)
    return parsed.astimezone(timezone.utc)


def local_day_start_utc(moment: datetime) -> datetime:
    local = moment.astimezone(LOCAL_TIMEZONE)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(
        timezone.utc
    )


def _running_run(connection: Any) -> Any:
    return connection.execute(
        "SELECT * FROM calibration_runs WHERE status = 'running' ORDER BY id DESC LIMIT 1"
    ).fetchone()


def prepare_run(
    state_path: Path,
    config_path: Path,
    *,
    since: str | None = None,
    historical: bool = False,
    moment: datetime | None = None,
) -> dict[str, Any]:
    config = review_queue.load_config(config_path)
    current = (moment or review_queue.now()).astimezone(timezone.utc)
    connection = review_queue.connect_state(state_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        active = _running_run(connection)
        if active:
            connection.rollback()
            return _run_context(connection, active, config, resumed=True)

        if since:
            window_start = parse_iso(since)
        else:
            previous = connection.execute(
                """
                SELECT window_end FROM calibration_runs
                WHERE status = 'completed' AND historical = 0
                ORDER BY id DESC LIMIT 1
                """
            ).fetchone()
            window_start = (
                parse_iso(previous["window_end"]) - OVERLAP
                if previous
                else local_day_start_utc(current)
            )
        timestamp = review_queue.isoformat(current)
        cursor = connection.execute(
            """
            INSERT INTO calibration_runs(
                status, window_start, window_end, historical,
                started_at, updated_at
            ) VALUES ('running', ?, ?, ?, ?, ?)
            """,
            (
                review_queue.isoformat(window_start),
                timestamp,
                int(historical),
                timestamp,
                timestamp,
            ),
        )
        run_id = cursor.lastrowid
        connection.commit()
        run_row = connection.execute(
            "SELECT * FROM calibration_runs WHERE id = ?", (run_id,)
        ).fetchone()
        return _run_context(connection, run_row, config, resumed=False)
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


def _run_context(
    connection: Any, run_row: Any, config: dict[str, Any], *, resumed: bool
) -> dict[str, Any]:
    bound = [
        dict(row)
        for row in connection.execute(
            """
            SELECT pr.id AS pull_request_id, pr.repository, pr.number, pr.url,
                   pr.task_thread_id AS thread_id, pr.task_host_id AS host_id,
                   ct.last_audited_turn_id, ct.last_audited_at
            FROM pull_requests pr
            LEFT JOIN calibration_threads ct ON ct.thread_id = pr.task_thread_id
            WHERE pr.task_thread_id IS NOT NULL
            ORDER BY pr.updated_at DESC
            """
        ).fetchall()
    ]
    remembered = [
        dict(row)
        for row in connection.execute(
            """
            SELECT thread_id, host_id, title, source, project_id,
                   pull_request_id, last_seen_at, last_audited_turn_id,
                   last_audited_at
            FROM calibration_threads
            ORDER BY last_seen_at DESC
            """
        ).fetchall()
    ]
    return {
        "status": "resumed" if resumed else "prepared",
        "run": dict(run_row),
        "project_id": config["codex_project_id"],
        "bound_pr_tasks": bound,
        "remembered_tasks": remembered,
        "max_proposals": MAX_PROPOSALS,
    }


def _required_text(value: dict[str, Any], field: str, context: str) -> str:
    result = value.get(field)
    if not isinstance(result, str) or not result.strip():
        raise CalibrationError(f"{context}.{field} must be a non-empty string")
    return result.strip()


def validate_artifact(document: dict[str, Any]) -> None:
    for field in ("threads", "signals", "proposals"):
        if not isinstance(document.get(field, []), list):
            raise CalibrationError(f"{field} must be an array")
    if len(document.get("proposals", [])) > MAX_PROPOSALS:
        raise CalibrationError(f"A run may propose at most {MAX_PROPOSALS} changes")


def _next_proposal_id(connection: Any) -> str:
    rows = connection.execute("SELECT id FROM calibration_proposals").fetchall()
    numbers = [
        int(row["id"][2:])
        for row in rows
        if row["id"].startswith("C-") and row["id"][2:].isdigit()
    ]
    return f"C-{max(numbers, default=0) + 1:03d}"


def complete_run(
    state_path: Path, run_id: int, report_path: Path, artifact_path: Path
) -> dict[str, Any]:
    if not report_path.is_file():
        raise CalibrationError(f"Report not found: {report_path}")
    document = load_json(artifact_path)
    validate_artifact(document)
    connection = review_queue.connect_state(state_path)
    timestamp = review_queue.isoformat()
    try:
        connection.execute("BEGIN IMMEDIATE")
        run_row = connection.execute(
            "SELECT * FROM calibration_runs WHERE id = ?", (run_id,)
        ).fetchone()
        if not run_row:
            raise CalibrationError(f"Unknown calibration run: {run_id}")
        if run_row["status"] != "running":
            raise CalibrationError(
                f"Calibration run {run_id} is {run_row['status']}, not running"
            )

        known_pr_ids = {
            row["id"]
            for row in connection.execute("SELECT id FROM pull_requests").fetchall()
        }
        for item in document.get("threads", []):
            thread_id = _required_text(item, "thread_id", "thread")
            source = _required_text(item, "source", "thread")
            pull_request_id = item.get("pull_request_id")
            if pull_request_id is not None and pull_request_id not in known_pr_ids:
                raise CalibrationError(
                    f"thread.pull_request_id is unknown: {pull_request_id}"
                )
            last_seen = item.get("last_seen_at") or timestamp
            connection.execute(
                """
                INSERT INTO calibration_threads(
                    thread_id, host_id, title, source, project_id,
                    pull_request_id, first_seen_at, last_seen_at,
                    last_audited_turn_id, last_audited_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(thread_id) DO UPDATE SET
                    host_id = COALESCE(excluded.host_id, calibration_threads.host_id),
                    title = COALESCE(excluded.title, calibration_threads.title),
                    source = excluded.source,
                    project_id = COALESCE(excluded.project_id, calibration_threads.project_id),
                    pull_request_id = COALESCE(
                        excluded.pull_request_id, calibration_threads.pull_request_id
                    ),
                    last_seen_at = excluded.last_seen_at,
                    last_audited_turn_id = COALESCE(
                        excluded.last_audited_turn_id,
                        calibration_threads.last_audited_turn_id
                    ),
                    last_audited_at = CASE
                        WHEN excluded.last_audited_turn_id IS NOT NULL
                        THEN excluded.last_audited_at
                        ELSE calibration_threads.last_audited_at
                    END
                """,
                (
                    thread_id,
                    item.get("host_id"),
                    item.get("title"),
                    source,
                    item.get("project_id"),
                    pull_request_id,
                    timestamp,
                    last_seen,
                    item.get("latest_turn_id"),
                    item.get("latest_turn_at") or (
                        timestamp if item.get("latest_turn_id") else None
                    ),
                ),
            )

        signal_ids: dict[str, int] = {}
        signal_threads: dict[str, str] = {}
        signal_impacts: dict[str, str] = {}
        for index, item in enumerate(document.get("signals", []), start=1):
            local_id = _required_text(item, "id", f"signal[{index}]")
            if local_id in signal_ids:
                raise CalibrationError(f"Duplicate signal id: {local_id}")
            thread_id = _required_text(item, "thread_id", local_id)
            if not connection.execute(
                "SELECT 1 FROM calibration_threads WHERE thread_id = ?", (thread_id,)
            ).fetchone():
                raise CalibrationError(f"{local_id} references unknown task {thread_id}")
            category = _required_text(item, "category", local_id)
            impact = _required_text(item, "impact", local_id)
            if category not in CATEGORIES:
                raise CalibrationError(f"{local_id} has invalid category: {category}")
            if impact not in IMPACTS:
                raise CalibrationError(f"{local_id} has invalid impact: {impact}")
            signal_fingerprint = item.get("fingerprint") or fingerprint(
                item, ("thread_id", "turn_id", "category", "summary")
            )
            cursor = connection.execute(
                """
                INSERT INTO calibration_signals(
                    run_id, thread_id, turn_id, observed_at, category, impact,
                    summary, evidence_json, fingerprint, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(thread_id, turn_id, fingerprint) DO NOTHING
                """,
                (
                    run_id,
                    thread_id,
                    _required_text(item, "turn_id", local_id),
                    item.get("observed_at") or timestamp,
                    category,
                    impact,
                    _required_text(item, "summary", local_id),
                    json.dumps(item.get("evidence", {}), sort_keys=True),
                    signal_fingerprint,
                    timestamp,
                ),
            )
            if cursor.rowcount == 1:
                database_id = cursor.lastrowid
            else:
                row = connection.execute(
                    """
                    SELECT id FROM calibration_signals
                    WHERE thread_id = ? AND turn_id = ? AND fingerprint = ?
                    """,
                    (thread_id, item["turn_id"], signal_fingerprint),
                ).fetchone()
                database_id = row["id"]
            signal_ids[local_id] = database_id
            signal_threads[local_id] = thread_id
            signal_impacts[local_id] = impact

        created: list[str] = []
        updated: list[str] = []
        suppressed: list[str] = []
        for index, item in enumerate(document.get("proposals", []), start=1):
            context = f"proposal[{index}]"
            evidence = item.get("signal_ids")
            if not isinstance(evidence, list) or not evidence:
                raise CalibrationError(f"{context}.signal_ids must be a non-empty array")
            unknown = [signal_id for signal_id in evidence if signal_id not in signal_ids]
            if unknown:
                raise CalibrationError(f"{context} references unknown signals: {unknown}")
            distinct_threads = {signal_threads[signal_id] for signal_id in evidence}
            high_impact = any(signal_impacts[signal_id] == "high" for signal_id in evidence)
            if len(distinct_threads) < 2 and not high_impact:
                raise CalibrationError(
                    f"{context} needs evidence from two tasks or one high-impact signal"
                )
            target_layer = _required_text(item, "target_layer", context)
            if target_layer not in TARGET_LAYERS:
                raise CalibrationError(
                    f"{context} has invalid target_layer: {target_layer}"
                )
            proposal_fingerprint = item.get("fingerprint") or fingerprint(
                item, ("target_layer", "title", "summary", "proposed_change")
            )
            existing = connection.execute(
                "SELECT * FROM calibration_proposals WHERE fingerprint = ?",
                (proposal_fingerprint,),
            ).fetchone()
            if existing:
                proposal_id = existing["id"]
                if existing["status"] == "rejected":
                    suppressed.append(proposal_id)
                else:
                    updated.append(proposal_id)
            else:
                proposal_id = _next_proposal_id(connection)
                connection.execute(
                    """
                    INSERT INTO calibration_proposals(
                        id, fingerprint, title, target_layer, summary,
                        proposed_change, expected_benefit, downside,
                        regression_scenario, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'proposed', ?, ?)
                    """,
                    (
                        proposal_id,
                        proposal_fingerprint,
                        _required_text(item, "title", context),
                        target_layer,
                        _required_text(item, "summary", context),
                        _required_text(item, "proposed_change", context),
                        _required_text(item, "expected_benefit", context),
                        _required_text(item, "downside", context),
                        _required_text(item, "regression_scenario", context),
                        timestamp,
                        timestamp,
                    ),
                )
                created.append(proposal_id)
            for local_signal_id in evidence:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO calibration_proposal_evidence(
                        proposal_id, signal_id, created_at
                    ) VALUES (?, ?, ?)
                    """,
                    (proposal_id, signal_ids[local_signal_id], timestamp),
                )

        connection.execute(
            """
            UPDATE calibration_runs
            SET status = 'completed', report_path = ?, completed_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (str(report_path.resolve()), timestamp, timestamp, run_id),
        )
        connection.commit()
        return {
            "run_id": run_id,
            "status": "completed",
            "report": str(report_path.resolve()),
            "signals": len(signal_ids),
            "created_proposals": created,
            "updated_proposals": sorted(set(updated)),
            "suppressed_proposals": sorted(set(suppressed)),
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def fail_run(state_path: Path, run_id: int, reason: str) -> dict[str, Any]:
    connection = review_queue.connect_state(state_path)
    timestamp = review_queue.isoformat()
    try:
        cursor = connection.execute(
            """
            UPDATE calibration_runs
            SET status = 'failed', error = ?, updated_at = ?
            WHERE id = ? AND status = 'running'
            """,
            (reason, timestamp, run_id),
        )
        if cursor.rowcount != 1:
            raise CalibrationError(f"Calibration run {run_id} is not running")
        return {"run_id": run_id, "status": "failed", "reason": reason}
    finally:
        connection.close()


def proposal_history(state_path: Path) -> dict[str, Any]:
    connection = review_queue.connect_state(state_path)
    try:
        runs = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM calibration_runs ORDER BY id DESC"
            ).fetchall()
        ]
        proposals = []
        for row in connection.execute(
            """
            SELECT cp.*, COUNT(cpe.signal_id) AS evidence_count,
                   COUNT(DISTINCT cs.thread_id) AS task_count
            FROM calibration_proposals cp
            LEFT JOIN calibration_proposal_evidence cpe ON cpe.proposal_id = cp.id
            LEFT JOIN calibration_signals cs ON cs.id = cpe.signal_id
            GROUP BY cp.id
            ORDER BY cp.created_at DESC, cp.id DESC
            """
        ).fetchall():
            proposals.append(dict(row))
        return {"runs": runs, "proposals": proposals}
    finally:
        connection.close()


def decide_proposal(
    state_path: Path, proposal_id: str, status: str, note: str | None
) -> dict[str, Any]:
    if status not in {"accepted", "rejected", "deferred"}:
        raise CalibrationError(f"Invalid decision status: {status}")
    connection = review_queue.connect_state(state_path)
    timestamp = review_queue.isoformat()
    try:
        row = connection.execute(
            "SELECT status FROM calibration_proposals WHERE id = ?", (proposal_id,)
        ).fetchone()
        if not row:
            raise CalibrationError(f"Unknown proposal: {proposal_id}")
        allowed = {
            "proposed": {"accepted", "rejected", "deferred"},
            "deferred": {"accepted", "rejected", "deferred"},
        }
        if status not in allowed.get(row["status"], set()):
            raise CalibrationError(
                f"Cannot change {proposal_id} from {row['status']} to {status}"
            )
        connection.execute(
            """
            UPDATE calibration_proposals
            SET status = ?, decision_note = ?, updated_at = ? WHERE id = ?
            """,
            (status, note, timestamp, proposal_id),
        )
        return {"proposal_id": proposal_id, "status": status, "note": note}
    finally:
        connection.close()


def edit_proposal(state_path: Path, proposal_id: str, edit_path: Path) -> dict[str, Any]:
    edit = load_json(edit_path)
    allowed_fields = {
        "title",
        "target_layer",
        "summary",
        "proposed_change",
        "expected_benefit",
        "downside",
        "regression_scenario",
    }
    unknown = set(edit) - allowed_fields
    if unknown:
        raise CalibrationError(f"Unsupported proposal fields: {sorted(unknown)}")
    if not edit:
        raise CalibrationError("Proposal edit is empty")
    if "target_layer" in edit and edit["target_layer"] not in TARGET_LAYERS:
        raise CalibrationError(f"Invalid target_layer: {edit['target_layer']}")
    for field, value in edit.items():
        if not isinstance(value, str) or not value.strip():
            raise CalibrationError(f"{field} must be a non-empty string")
    connection = review_queue.connect_state(state_path)
    timestamp = review_queue.isoformat()
    try:
        row = connection.execute(
            "SELECT * FROM calibration_proposals WHERE id = ?", (proposal_id,)
        ).fetchone()
        if not row:
            raise CalibrationError(f"Unknown proposal: {proposal_id}")
        if row["status"] not in {"proposed", "accepted", "deferred"}:
            raise CalibrationError(f"Cannot edit a {row['status']} proposal")
        assignments = ", ".join(f"{field} = ?" for field in edit)
        connection.execute(
            f"UPDATE calibration_proposals SET {assignments}, updated_at = ? WHERE id = ?",
            (*[edit[field].strip() for field in edit], timestamp, proposal_id),
        )
        return {"proposal_id": proposal_id, "status": row["status"], "edited": sorted(edit)}
    finally:
        connection.close()


def mark_implemented(state_path: Path, proposal_id: str, commit: str) -> dict[str, Any]:
    connection = review_queue.connect_state(state_path)
    timestamp = review_queue.isoformat()
    try:
        row = connection.execute(
            "SELECT status FROM calibration_proposals WHERE id = ?", (proposal_id,)
        ).fetchone()
        if not row:
            raise CalibrationError(f"Unknown proposal: {proposal_id}")
        if row["status"] != "accepted":
            raise CalibrationError(
                f"Cannot mark {proposal_id} implemented from {row['status']}"
            )
        connection.execute(
            """
            UPDATE calibration_proposals
            SET status = 'implemented', implemented_commit = ?, updated_at = ?
            WHERE id = ?
            """,
            (commit, timestamp, proposal_id),
        )
        return {"proposal_id": proposal_id, "status": "implemented", "commit": commit}
    finally:
        connection.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    commands = parser.add_subparsers(dest="command", required=True)

    prepare = commands.add_parser("prepare")
    prepare.add_argument("--since")
    prepare.add_argument("--historical", action="store_true")

    complete = commands.add_parser("complete")
    complete.add_argument("--run-id", required=True, type=int)
    complete.add_argument("--report", required=True, type=Path)
    complete.add_argument("--proposals", required=True, type=Path)

    fail = commands.add_parser("fail")
    fail.add_argument("--run-id", required=True, type=int)
    fail.add_argument("--reason", required=True)

    commands.add_parser("history")

    decide = commands.add_parser("decide")
    choice = decide.add_mutually_exclusive_group(required=True)
    choice.add_argument("--accept")
    choice.add_argument("--reject")
    choice.add_argument("--defer")
    decide.add_argument("--note")

    edit = commands.add_parser("edit-proposal")
    edit.add_argument("--proposal", required=True)
    edit.add_argument("--edit", required=True, type=Path)

    implemented = commands.add_parser("mark-implemented")
    implemented.add_argument("--proposal", required=True)
    implemented.add_argument("--commit", required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "prepare":
            result = prepare_run(
                args.state,
                args.config,
                since=args.since,
                historical=args.historical,
            )
        elif args.command == "complete":
            result = complete_run(
                args.state,
                args.run_id,
                args.report.resolve(),
                args.proposals.resolve(),
            )
        elif args.command == "fail":
            result = fail_run(args.state, args.run_id, args.reason)
        elif args.command == "history":
            result = proposal_history(args.state)
        elif args.command == "decide":
            proposal_id = args.accept or args.reject or args.defer
            status = "accepted" if args.accept else "rejected" if args.reject else "deferred"
            result = decide_proposal(args.state, proposal_id, status, args.note)
        elif args.command == "edit-proposal":
            result = edit_proposal(args.state, args.proposal, args.edit.resolve())
        elif args.command == "mark-implemented":
            result = mark_implemented(
                args.state, args.proposal, args.commit
            )
        else:
            raise AssertionError(args.command)
        print(json.dumps({"status": "ok", **result}, indent=2))
        return 0
    except (CalibrationError, review_queue.QueueError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
