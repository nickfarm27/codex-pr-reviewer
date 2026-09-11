#!/usr/bin/env python3
"""Discover, claim, and track GitHub PRs requesting the current user's review."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config.json"
DEFAULT_STATE = ROOT / ".state" / "reviews.db"
LEGACY_STATE = ROOT / ".state" / "reviews.json"
SCHEMA_VERSION = 4
MAX_PR_BODY_CHARS = 12_000
ACTIVE_STATUSES = {"claimed", "dispatched", "preparing", "reviewing"}
FINDING_STATUSES = {
    "proposed",
    "accepted",
    "rejected",
    "drafted",
    "submitted",
    "resolved",
    "still_open",
    "obsolete",
}
USER_FINDING_KINDS = {"defect", "required_change", "question", "suggestion"}
AUTOMATED_FINDING_KINDS = {"defect", "maintainability"}
EDITABLE_FINDING_FIELDS = {
    "severity",
    "title",
    "explanation",
    "failure_example",
    "safeguard",
    "safeguard_kind",
    "review_comment",
    "kind",
    "source_note",
}
LINEAR_ISSUE_PATTERN = re.compile(
    r"(?<![A-Z0-9])([A-Z][A-Z0-9]{1,9}-\d+)(?![A-Z0-9]|\.\d)",
    re.IGNORECASE,
)
GITHUB_PR_URL_PATTERN = re.compile(
    r"^https://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/"
    r"(?P<repo>[A-Za-z0-9_.-]+)/pull/(?P<number>[1-9]\d*)/?$"
)
UPDATE_REVIEW_COMMENT_MUTATION = """
mutation UpdatePullRequestReviewComment($commentId: ID!, $body: String!) {
  updatePullRequestReviewComment(
    input: {pullRequestReviewCommentId: $commentId, body: $body}
  ) {
    pullRequestReviewComment {
      id
      body
      url
    }
  }
}
""".strip()


class QueueError(RuntimeError):
    pass


def now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat(value: datetime | None = None) -> str:
    return (value or now()).isoformat().replace("+00:00", "Z")


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def run(
    command: list[str], *, cwd: Path | None = None, input_text: str | None = None
) -> str:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            input=input_text,
        )
    except FileNotFoundError as exc:
        raise QueueError(f"Required command not found: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() or exc.stdout.strip() or f"exit {exc.returncode}"
        raise QueueError(f"Command failed: {' '.join(command)}\n{detail}") from exc
    return result.stdout.strip()


def run_json(
    command: list[str], *, cwd: Path | None = None, input_data: Any | None = None
) -> Any:
    input_text = None if input_data is None else json.dumps(input_data)
    output = run(command, cwd=cwd, input_text=input_text)
    try:
        return json.loads(output)
    except json.JSONDecodeError as exc:
        raise QueueError(f"Command returned invalid JSON: {' '.join(command)}") from exc


def load_config(path: Path) -> dict[str, Any]:
    try:
        config = json.loads(path.read_text())
    except FileNotFoundError as exc:
        raise QueueError(f"Configuration not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise QueueError(f"Invalid JSON in {path}: {exc}") from exc

    if not isinstance(config.get("repositories", []), list):
        raise QueueError("config.repositories must be an array")
    if not isinstance(config.get("local_repositories", {}), dict):
        raise QueueError("config.local_repositories must be an object")
    project_id = config.get("codex_project_id")
    if not isinstance(project_id, str) or not project_id.strip():
        raise QueueError("config.codex_project_id must be a non-empty string")
    if project_id == "REPLACE_WITH_YOUR_CODEX_PROJECT_ID":
        raise QueueError(
            "Replace config.codex_project_id with the local Codex project ID"
        )
    max_concurrent = config.get("max_concurrent_reviews", 4)
    if not isinstance(max_concurrent, int) or max_concurrent < 1:
        raise QueueError("config.max_concurrent_reviews must be a positive integer")
    return config


def github_login() -> str:
    return run(["gh", "api", "user", "--jq", ".login"])


def resolve_login(value: str, current_login: str) -> str:
    return current_login if value == "@me" else value


def extract_linear_issue_ids(*values: str | None) -> list[str]:
    """Return unique issue identifiers in first-seen order."""
    identifiers: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not value:
            continue
        for match in LINEAR_ISSUE_PATTERN.finditer(value):
            identifier = match.group(1).upper()
            if identifier not in seen:
                seen.add(identifier)
                identifiers.append(identifier)
    return identifiers


def search_prs(config: dict[str, Any]) -> list[dict[str, Any]]:
    base = [
        "gh",
        "search",
        "prs",
        f"--review-requested={config.get('reviewer', '@me')}",
        "--state=open",
        "--sort=updated",
        "--order=asc",
        "--json=number,title,url,repository,author,isDraft,updatedAt",
        "--limit=100",
    ]
    repositories = config.get("repositories", [])
    if not repositories:
        return run_json(base)

    results: list[dict[str, Any]] = []
    for repository in repositories:
        results.extend(run_json([*base, f"--repo={repository}"]))
    results.sort(key=lambda item: item["updatedAt"])
    return results


def pr_details(url: str) -> dict[str, Any]:
    return run_json(
        [
            "gh",
            "pr",
            "view",
            url,
            "--json=number,title,body,url,state,isDraft,author,headRefOid,headRefName,baseRefName,reviewRequests,mergeable,statusCheckRollup,labels,changedFiles,additions,deletions",
        ]
    )


def latest_review_request_event(
    repository: str, number: int, reviewer: str
) -> dict[str, Any] | None:
    """Return the latest explicit review-request event for the reviewer."""
    try:
        events = run_json(
            [
                "gh",
                "api",
                "--paginate",
                "--slurp",
                f"repos/{repository}/issues/{number}/events?per_page=100",
            ]
        )
    except QueueError:
        return None

    flattened = [event for page in events for event in page]
    for event in reversed(flattened):
        requested = event.get("requested_reviewer") or {}
        if event.get("event") == "review_requested" and requested.get("login") == reviewer:
            return {
                "id": str(event["id"]),
                "requested_at": event.get("created_at"),
            }
    return None


def candidates(config: dict[str, Any]) -> list[dict[str, Any]]:
    current_login = github_login()
    reviewer = resolve_login(config.get("reviewer", "@me"), current_login)
    excluded = {
        resolve_login(author, current_login)
        for author in config.get("exclude_authors", ["@me"])
    }

    found: list[dict[str, Any]] = []
    for item in search_prs(config):
        if item.get("isDraft"):
            continue
        author = (item.get("author") or {}).get("login")
        if author in excluded:
            continue

        details = pr_details(item["url"])
        requested = {
            request.get("login")
            for request in details.get("reviewRequests", [])
            if request.get("__typename") == "User"
        }
        if reviewer not in requested or details.get("isDraft"):
            continue

        repository = item["repository"]["nameWithOwner"]
        sha = details["headRefOid"]
        request_event = latest_review_request_event(
            repository, details["number"], reviewer
        )
        request_event_id = request_event["id"] if request_event else None
        key_suffix = f"~request-{request_event_id}" if request_event_id else ""
        body = details.get("body") or ""
        found.append(
            {
                "repository": repository,
                "number": details["number"],
                "title": details["title"],
                "body": body[:MAX_PR_BODY_CHARS],
                "body_truncated": len(body) > MAX_PR_BODY_CHARS,
                "url": details["url"],
                "author": author,
                "base_ref": details["baseRefName"],
                "head_ref": details["headRefName"],
                "head_sha": sha,
                "key": f"{repository}#{details['number']}@{sha}{key_suffix}",
                "review_request_event_id": request_event_id,
                "review_requested_at": (
                    request_event.get("requested_at") if request_event else None
                ),
                "updated_at": item["updatedAt"],
                "mergeable": details.get("mergeable"),
                "checks": details.get("statusCheckRollup", []),
                "labels": [
                    label.get("name")
                    for label in details.get("labels", [])
                    if label.get("name")
                ],
                "changed_files": details.get("changedFiles"),
                "additions": details.get("additions"),
                "deletions": details.get("deletions"),
                "linear_issue_ids": extract_linear_issue_ids(
                    details.get("title"), body, details.get("headRefName")
                ),
            }
        )
    return found


def row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def connect_state(path: Path, *, import_legacy: bool = True) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 10000")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS pull_requests (
            id INTEGER PRIMARY KEY,
            repository TEXT NOT NULL,
            number INTEGER NOT NULL,
            url TEXT NOT NULL,
            title TEXT,
            author TEXT,
            linear_issue_ids_json TEXT NOT NULL DEFAULT '[]',
            task_thread_id TEXT,
            task_host_id TEXT,
            task_client_thread_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            UNIQUE(repository, number)
        );

        CREATE TABLE IF NOT EXISTS review_rounds (
            id INTEGER PRIMARY KEY,
            pull_request_id INTEGER NOT NULL REFERENCES pull_requests(id) ON DELETE CASCADE,
            claim_key TEXT NOT NULL UNIQUE,
            head_sha TEXT NOT NULL,
            base_ref TEXT,
            head_ref TEXT,
            review_request_event_id TEXT,
            review_requested_at TEXT,
            status TEXT NOT NULL,
            claimed_at TEXT,
            dispatched_at TEXT,
            prepared_at TEXT,
            completed_at TEXT,
            failed_at TEXT,
            updated_at TEXT NOT NULL,
            report_path TEXT,
            findings_path TEXT,
            checkout_path TEXT,
            error TEXT,
            candidate_json TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS review_rounds_pr_time
            ON review_rounds(pull_request_id, id DESC);
        CREATE INDEX IF NOT EXISTS review_rounds_status
            ON review_rounds(status, claimed_at);

        CREATE TABLE IF NOT EXISTS findings (
            id INTEGER PRIMARY KEY,
            review_round_id INTEGER NOT NULL REFERENCES review_rounds(id) ON DELETE CASCADE,
            finding_key TEXT NOT NULL,
            severity TEXT NOT NULL,
            title TEXT NOT NULL,
            path TEXT NOT NULL,
            start_line INTEGER,
            end_line INTEGER,
            explanation TEXT NOT NULL,
            failure_example TEXT NOT NULL,
            safeguard TEXT NOT NULL,
            safeguard_kind TEXT NOT NULL,
            review_comment TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'agent',
            kind TEXT NOT NULL DEFAULT 'defect',
            added_after_completion INTEGER NOT NULL DEFAULT 0,
            source_note TEXT,
            origin_finding_id INTEGER REFERENCES findings(id) ON DELETE SET NULL,
            status TEXT NOT NULL DEFAULT 'proposed',
            decision_note TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(review_round_id, finding_key)
        );

        CREATE TABLE IF NOT EXISTS github_reviews (
            id INTEGER PRIMARY KEY,
            review_round_id INTEGER NOT NULL REFERENCES review_rounds(id) ON DELETE CASCADE,
            github_review_id INTEGER NOT NULL UNIQUE,
            state TEXT NOT NULL,
            event TEXT,
            commit_sha TEXT NOT NULL,
            body TEXT,
            html_url TEXT,
            payload_hash TEXT,
            created_at TEXT NOT NULL,
            submitted_at TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS github_review_comments (
            id INTEGER PRIMARY KEY,
            github_review_id INTEGER NOT NULL REFERENCES github_reviews(id) ON DELETE CASCADE,
            finding_key TEXT,
            github_comment_id INTEGER,
            path TEXT,
            line INTEGER,
            side TEXT,
            body TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY,
            pull_request_id INTEGER REFERENCES pull_requests(id) ON DELETE CASCADE,
            review_round_id INTEGER REFERENCES review_rounds(id) ON DELETE CASCADE,
            event_type TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS calibration_runs (
            id INTEGER PRIMARY KEY,
            status TEXT NOT NULL,
            window_start TEXT NOT NULL,
            window_end TEXT NOT NULL,
            historical INTEGER NOT NULL DEFAULT 0,
            report_path TEXT,
            error TEXT,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE UNIQUE INDEX IF NOT EXISTS calibration_one_running
            ON calibration_runs(status) WHERE status = 'running';

        CREATE TABLE IF NOT EXISTS calibration_threads (
            thread_id TEXT PRIMARY KEY,
            host_id TEXT,
            title TEXT,
            source TEXT NOT NULL,
            project_id TEXT,
            pull_request_id INTEGER REFERENCES pull_requests(id) ON DELETE SET NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            last_audited_turn_id TEXT,
            last_audited_at TEXT
        );

        CREATE TABLE IF NOT EXISTS calibration_signals (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL REFERENCES calibration_runs(id) ON DELETE CASCADE,
            thread_id TEXT NOT NULL REFERENCES calibration_threads(thread_id) ON DELETE CASCADE,
            turn_id TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            category TEXT NOT NULL,
            impact TEXT NOT NULL,
            summary TEXT NOT NULL,
            evidence_json TEXT NOT NULL DEFAULT '{}',
            fingerprint TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(thread_id, turn_id, fingerprint)
        );

        CREATE TABLE IF NOT EXISTS calibration_proposals (
            id TEXT PRIMARY KEY,
            fingerprint TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            target_layer TEXT NOT NULL,
            summary TEXT NOT NULL,
            proposed_change TEXT NOT NULL,
            expected_benefit TEXT NOT NULL,
            downside TEXT NOT NULL,
            regression_scenario TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'proposed',
            decision_note TEXT,
            implemented_commit TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS calibration_proposal_evidence (
            proposal_id TEXT NOT NULL REFERENCES calibration_proposals(id) ON DELETE CASCADE,
            signal_id INTEGER NOT NULL REFERENCES calibration_signals(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL,
            PRIMARY KEY(proposal_id, signal_id)
        );
        """
    )
    version_row = connection.execute(
        "SELECT value FROM metadata WHERE key = 'schema_version'"
    ).fetchone()
    if version_row is None:
        connection.execute(
            "INSERT INTO metadata(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
    elif int(version_row["value"]) > SCHEMA_VERSION:
        connection.close()
        raise QueueError(
            f"State database schema {version_row['value']} is newer than supported "
            f"schema {SCHEMA_VERSION}"
        )
    elif int(version_row["value"]) < SCHEMA_VERSION:
        migrate_schema(connection, int(version_row["value"]))
    if import_legacy and path.resolve() == DEFAULT_STATE.resolve():
        migrate_legacy_state(connection, LEGACY_STATE)
    return connection


def migrate_schema(connection: sqlite3.Connection, version: int) -> None:
    """Upgrade an existing state database without discarding review history."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        if version == 1:
            existing_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(findings)").fetchall()
            }
            additions = {
                "source": "TEXT NOT NULL DEFAULT 'agent'",
                "kind": "TEXT NOT NULL DEFAULT 'defect'",
                "added_after_completion": "INTEGER NOT NULL DEFAULT 0",
                "source_note": "TEXT",
            }
            for name, declaration in additions.items():
                if name not in existing_columns:
                    connection.execute(
                        f"ALTER TABLE findings ADD COLUMN {name} {declaration}"
                    )
            version = 2
        if version == 2:
            # Calibration tables are created idempotently by connect_state before
            # migration. Advancing the version records that the additive schema is
            # available without rewriting any review lifecycle data.
            version = 3
        if version == 3:
            existing_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(findings)").fetchall()
            }
            if "origin_finding_id" not in existing_columns:
                connection.execute(
                    "ALTER TABLE findings ADD COLUMN origin_finding_id "
                    "INTEGER REFERENCES findings(id) ON DELETE SET NULL"
                )
            version = 4
        if version != SCHEMA_VERSION:
            raise QueueError(
                f"No migration path from schema {version} to {SCHEMA_VERSION}"
            )
        connection.execute(
            "UPDATE metadata SET value = ? WHERE key = 'schema_version'",
            (str(SCHEMA_VERSION),),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def record_event(
    connection: sqlite3.Connection,
    event_type: str,
    *,
    pull_request_id: int | None = None,
    review_round_id: int | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO events(
            pull_request_id, review_round_id, event_type, payload_json, created_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (
            pull_request_id,
            review_round_id,
            event_type,
            json.dumps(payload or {}, sort_keys=True),
            isoformat(),
        ),
    )


def upsert_pull_request(
    connection: sqlite3.Connection, candidate: dict[str, Any]
) -> sqlite3.Row:
    timestamp = isoformat()
    connection.execute(
        """
        INSERT INTO pull_requests(
            repository, number, url, title, author, linear_issue_ids_json,
            created_at, updated_at, last_seen_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(repository, number) DO UPDATE SET
            url = excluded.url,
            title = excluded.title,
            author = excluded.author,
            linear_issue_ids_json = excluded.linear_issue_ids_json,
            updated_at = excluded.updated_at,
            last_seen_at = excluded.last_seen_at
        """,
        (
            candidate["repository"],
            candidate["number"],
            candidate["url"],
            candidate.get("title"),
            candidate.get("author"),
            json.dumps(candidate.get("linear_issue_ids", [])),
            timestamp,
            timestamp,
            timestamp,
        ),
    )
    row = connection.execute(
        "SELECT * FROM pull_requests WHERE repository = ? AND number = ?",
        (candidate["repository"], candidate["number"]),
    ).fetchone()
    assert row is not None
    return row


def migrate_legacy_state(connection: sqlite3.Connection, legacy_path: Path) -> None:
    already = connection.execute(
        "SELECT 1 FROM metadata WHERE key = 'legacy_json_imported_at'"
    ).fetchone()
    if already or not legacy_path.is_file():
        return
    try:
        legacy = json.loads(legacy_path.read_text())
    except json.JSONDecodeError as exc:
        raise QueueError(f"Invalid legacy state JSON in {legacy_path}: {exc}") from exc

    connection.execute("BEGIN IMMEDIATE")
    try:
        imported = 0
        for claim_key, entry in legacy.get("entries", {}).items():
            candidate = dict(entry.get("candidate") or {})
            candidate.setdefault("repository", entry.get("repository"))
            candidate.setdefault("number", entry.get("number"))
            candidate.setdefault("head_sha", entry.get("head_sha"))
            candidate.setdefault("title", entry.get("title") or "")
            candidate.setdefault("url", entry.get("url") or "")
            candidate.setdefault("linear_issue_ids", [])
            if not all(
                candidate.get(field)
                for field in ("repository", "number", "head_sha", "url")
            ):
                continue
            pr = upsert_pull_request(connection, candidate)
            timestamp = entry.get("updated_at") or entry.get("claimed_at") or isoformat()
            connection.execute(
                """
                INSERT OR IGNORE INTO review_rounds(
                    pull_request_id, claim_key, head_sha, base_ref, head_ref,
                    status, claimed_at, prepared_at, completed_at, failed_at,
                    updated_at, report_path, checkout_path, error, candidate_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pr["id"],
                    claim_key,
                    candidate["head_sha"],
                    candidate.get("base_ref"),
                    candidate.get("head_ref"),
                    entry.get("status", "completed"),
                    entry.get("claimed_at"),
                    entry.get("prepared_at"),
                    entry.get("completed_at"),
                    entry.get("failed_at"),
                    timestamp,
                    entry.get("report"),
                    entry.get("checkout_path"),
                    entry.get("reason"),
                    json.dumps(candidate, sort_keys=True),
                ),
            )
            imported += 1
        connection.execute(
            "INSERT INTO metadata(key, value) VALUES('legacy_json_imported_at', ?)",
            (isoformat(),),
        )
        record_event(
            connection,
            "legacy_json_imported",
            payload={"path": str(legacy_path), "entries": imported},
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def active_claim_count(
    connection: sqlite3.Connection, ttl: timedelta
) -> int:
    cutoff = isoformat(now() - ttl)
    placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
    row = connection.execute(
        f"""
        SELECT COUNT(*) AS count FROM review_rounds
        WHERE status IN ({placeholders}) AND claimed_at >= ?
        """,
        (*sorted(ACTIVE_STATUSES), cutoff),
    ).fetchone()
    return int(row["count"])


def candidate_is_available(
    connection: sqlite3.Connection,
    candidate: dict[str, Any],
    ttl: timedelta,
) -> bool:
    cutoff = isoformat(now() - ttl)
    placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
    other_active = connection.execute(
        f"""
        SELECT rr.claim_key FROM review_rounds rr
        JOIN pull_requests pr ON pr.id = rr.pull_request_id
        WHERE pr.repository = ? AND pr.number = ?
          AND rr.claim_key != ?
          AND rr.status IN ({placeholders}) AND rr.claimed_at >= ?
        LIMIT 1
        """,
        (
            candidate["repository"],
            candidate["number"],
            candidate["key"],
            *sorted(ACTIVE_STATUSES),
            cutoff,
        ),
    ).fetchone()
    if other_active:
        return False

    exact = connection.execute(
        "SELECT * FROM review_rounds WHERE claim_key = ?", (candidate["key"],)
    ).fetchone()
    if exact:
        if exact["status"] in {"completed", "failed"}:
            return False
        claimed_at = exact["claimed_at"]
        return not claimed_at or now() - parse_time(claimed_at) > ttl

    prior = connection.execute(
        """
        SELECT rr.* FROM review_rounds rr
        JOIN pull_requests pr ON pr.id = rr.pull_request_id
        WHERE pr.repository = ? AND pr.number = ?
          AND rr.head_sha = ? AND rr.status = 'completed'
        ORDER BY rr.completed_at DESC, rr.id DESC LIMIT 1
        """,
        (candidate["repository"], candidate["number"], candidate["head_sha"]),
    ).fetchone()
    if not prior:
        return True

    request_event_id = candidate.get("review_request_event_id")
    if (
        request_event_id
        and prior["review_request_event_id"]
        and request_event_id != prior["review_request_event_id"]
    ):
        return True

    requested_at = candidate.get("review_requested_at")
    completed_at = prior["completed_at"]
    if not requested_at or not completed_at:
        return False
    return parse_time(requested_at) > parse_time(completed_at)


def suggested_task_title(
    candidate: dict[str, Any], value: datetime | None = None
) -> str:
    timestamp = (value or datetime.now().astimezone()).strftime("%b %d %H:%M")
    repository = candidate["repository"].split("/")[-1]
    parts = []
    issue_ids = candidate.get("linear_issue_ids") or []
    if issue_ids:
        parts.append(issue_ids[0])
    parts.extend([f"{repository}#{candidate['number']}", timestamp])
    return " · ".join(parts)


def dispatch_descriptor(
    candidate: dict[str, Any], pull_request: sqlite3.Row
) -> dict[str, Any]:
    """Return only the fields the dispatcher needs to create a worker."""
    return {
        "key": candidate["key"],
        "repository": candidate["repository"],
        "number": candidate["number"],
        "url": candidate["url"],
        "linear_issue_ids": candidate.get("linear_issue_ids", []),
        "task_title": suggested_task_title(candidate),
        "task_thread_id": pull_request["task_thread_id"],
        "task_host_id": pull_request["task_host_id"],
        "task_client_thread_id": pull_request["task_client_thread_id"],
        "dispatch_action": (
            "continue_task" if pull_request["task_thread_id"] else "create_task"
        ),
    }


def claim_candidate(config: dict[str, Any], state_path: Path) -> dict[str, Any] | None:
    result = reserve_dispatch_batch(config, state_path, limit=1)
    if not result["reserved"]:
        return None
    key = result["reserved"][0]["key"]
    connection = connect_state(state_path)
    try:
        row = connection.execute(
            "SELECT candidate_json FROM review_rounds WHERE claim_key = ?", (key,)
        ).fetchone()
        return json.loads(row["candidate_json"])
    finally:
        connection.close()


def reserve_dispatch_batch(
    config: dict[str, Any], state_path: Path, limit: int | None = None
) -> dict[str, Any]:
    """Atomically reserve one worker slot per eligible PR."""
    maximum = (
        int(config.get("max_concurrent_reviews", 4)) if limit is None else limit
    )
    if maximum < 1:
        raise QueueError("dispatch limit must be a positive integer")
    available = candidates(config)
    ttl = timedelta(minutes=int(config.get("claim_ttl_minutes", 180)))

    connection = connect_state(state_path)
    connection.execute("BEGIN IMMEDIATE")
    try:
        active = active_claim_count(connection, ttl)
        slots = max(0, maximum - active)
        reserved: list[tuple[dict[str, Any], sqlite3.Row]] = []

        for candidate in available:
            if len(reserved) >= slots:
                break
            if not candidate_is_available(connection, candidate, ttl):
                continue
            pr = upsert_pull_request(connection, candidate)
            timestamp = isoformat()
            connection.execute(
                """
                INSERT INTO review_rounds(
                    pull_request_id, claim_key, head_sha, base_ref, head_ref,
                    review_request_event_id, review_requested_at, status,
                    claimed_at, updated_at, candidate_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'claimed', ?, ?, ?)
                ON CONFLICT(claim_key) DO UPDATE SET
                    status = 'claimed', claimed_at = excluded.claimed_at,
                    updated_at = excluded.updated_at, error = NULL,
                    candidate_json = excluded.candidate_json
                """,
                (
                    pr["id"],
                    candidate["key"],
                    candidate["head_sha"],
                    candidate.get("base_ref"),
                    candidate.get("head_ref"),
                    candidate.get("review_request_event_id"),
                    candidate.get("review_requested_at"),
                    timestamp,
                    timestamp,
                    json.dumps(candidate, sort_keys=True),
                ),
            )
            round_row = connection.execute(
                "SELECT id FROM review_rounds WHERE claim_key = ?",
                (candidate["key"],),
            ).fetchone()
            record_event(
                connection,
                "review_reserved",
                pull_request_id=pr["id"],
                review_round_id=round_row["id"],
                payload={"head_sha": candidate["head_sha"]},
            )
            reserved.append((candidate, pr))
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    status = "reserved" if reserved else ("at_capacity" if slots == 0 else "empty")
    return {
        "status": status,
        "active_before": active,
        "max_concurrent_reviews": maximum,
        "codex_project_id": config.get("codex_project_id"),
        "project_root": str(ROOT),
        "reserved": [
            dispatch_descriptor(candidate, pull_request)
            for candidate, pull_request in reserved
        ],
    }


def claimed_candidate(state_path: Path, key: str) -> dict[str, Any]:
    connection = connect_state(state_path)
    connection.execute("BEGIN IMMEDIATE")
    try:
        entry = connection.execute(
            "SELECT * FROM review_rounds WHERE claim_key = ?", (key,)
        ).fetchone()
        if entry is None:
            raise QueueError(f"Unknown claim key: {key}")
        if entry["status"] not in {"claimed", "dispatched"}:
            raise QueueError(
                f"Claim is not ready for preparation: {key} ({entry['status']})"
            )
        candidate = json.loads(entry["candidate_json"])
        if not candidate:
            raise QueueError(
                f"Claim predates dispatcher metadata and must be reset before dispatch: {key}"
            )
        timestamp = isoformat()
        connection.execute(
            "UPDATE review_rounds SET status = 'preparing', updated_at = ? WHERE id = ?",
            (timestamp, entry["id"]),
        )
        record_event(
            connection,
            "review_preparing",
            pull_request_id=entry["pull_request_id"],
            review_round_id=entry["id"],
        )
        connection.commit()
        return candidate
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def active_carried_findings(
    connection: sqlite3.Connection,
    pull_request_id: int,
    current_review_id: int,
) -> list[sqlite3.Row]:
    """Return the latest active snapshot of each finding lineage."""
    return connection.execute(
        """
        WITH ranked AS (
            SELECT rr.claim_key, rr.id AS source_round_id, f.*,
                   ROW_NUMBER() OVER (
                       PARTITION BY COALESCE(f.origin_finding_id, f.id)
                       ORDER BY rr.completed_at DESC, rr.id DESC, f.id DESC
                   ) AS lineage_rank
            FROM findings f
            JOIN review_rounds rr ON rr.id = f.review_round_id
            WHERE rr.pull_request_id = ? AND rr.id != ?
              AND rr.status = 'completed'
        )
        SELECT * FROM ranked
        WHERE lineage_rank = 1
          AND status IN ('accepted', 'drafted', 'submitted', 'still_open')
        ORDER BY source_round_id, id
        """,
        (pull_request_id, current_review_id),
    ).fetchall()


def previous_review_context(state_path: Path, key: str) -> dict[str, Any] | None:
    connection = connect_state(state_path)
    try:
        current = connection.execute(
            "SELECT id, pull_request_id FROM review_rounds WHERE claim_key = ?", (key,)
        ).fetchone()
        if current is None:
            raise QueueError(f"Unknown claim key: {key}")
        previous = connection.execute(
            """
            SELECT * FROM review_rounds
            WHERE pull_request_id = ? AND id != ? AND status = 'completed'
            ORDER BY completed_at DESC, id DESC LIMIT 1
            """,
            (current["pull_request_id"], current["id"]),
        ).fetchone()
        if previous is None:
            return None
        findings = [
            dict(row)
            for row in active_carried_findings(
                connection, current["pull_request_id"], current["id"]
            )
        ]
        github_review = connection.execute(
            """
            SELECT github_review_id, state, event, commit_sha, html_url, submitted_at
            FROM github_reviews WHERE review_round_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            (previous["id"],),
        ).fetchone()
        return {
            "claim_key": previous["claim_key"],
            "head_sha": previous["head_sha"],
            "completed_at": previous["completed_at"],
            "report_path": previous["report_path"],
            "accepted_findings": findings,
            "github_review": row_dict(github_review),
        }
    finally:
        connection.close()


def prepare_claimed_candidate(
    config: dict[str, Any], state_path: Path, key: str
) -> dict[str, Any]:
    candidate = claimed_candidate(state_path, key)
    try:
        destination = prepare_checkout(candidate, config)
    except Exception as exc:
        update_entry(
            state_path,
            key,
            "failed",
            failed_at=isoformat(),
            reason=str(exc),
        )
        raise

    candidate["checkout_path"] = str(destination)
    candidate["diff_range"] = (
        f"origin/{candidate['base_ref']}...{candidate['head_sha']}"
    )
    candidate["suggested_report_path"] = str(report_path(candidate))
    candidate["suggested_findings_path"] = str(findings_path(candidate))
    candidate["previous_review"] = previous_review_context(state_path, key)
    update_entry(
        state_path,
        key,
        "reviewing",
        prepared_at=isoformat(),
        checkout_path=str(destination),
    )
    return candidate


def prepare_rereview(
    config: dict[str, Any],
    state_path: Path,
    repository: str,
    number: int,
) -> dict[str, Any]:
    """Create and prepare a user-triggered round for a PR's current head."""
    connection = connect_state(state_path)
    try:
        pull_request = connection.execute(
            "SELECT * FROM pull_requests WHERE repository = ? AND number = ?",
            (repository, number),
        ).fetchone()
        if pull_request is None:
            raise QueueError(
                f"PR is not bound to a continuing review task: {repository}#{number}"
            )
        if not pull_request["task_thread_id"]:
            raise QueueError(
                f"PR has no continuing review task: {repository}#{number}"
            )
        url = pull_request["url"]
        remembered_issue_ids = json.loads(
            pull_request["linear_issue_ids_json"] or "[]"
        )
    finally:
        connection.close()

    details = pr_details(url)
    if details.get("number") != number:
        raise QueueError(
            f"GitHub returned PR #{details.get('number')} for requested PR #{number}"
        )
    if details.get("state") not in {None, "OPEN"}:
        raise QueueError(f"PR is not open: {repository}#{number}")
    if details.get("isDraft"):
        raise QueueError(f"Draft PRs cannot be re-reviewed: {repository}#{number}")

    timestamp = isoformat()
    connection = connect_state(state_path)
    connection.execute("BEGIN IMMEDIATE")
    try:
        pull_request = connection.execute(
            "SELECT * FROM pull_requests WHERE repository = ? AND number = ?",
            (repository, number),
        ).fetchone()
        assert pull_request is not None
        active = connection.execute(
            f"""
            SELECT claim_key FROM review_rounds
            WHERE pull_request_id = ?
              AND status IN ({','.join('?' for _ in ACTIVE_STATUSES)})
            ORDER BY id DESC LIMIT 1
            """,
            (pull_request["id"], *sorted(ACTIVE_STATUSES)),
        ).fetchone()
        if active:
            raise QueueError(
                "This PR already has an active review round: "
                f"{active['claim_key']}"
            )

        sequence = connection.execute(
            """
            SELECT COUNT(*) AS count FROM review_rounds
            WHERE pull_request_id = ? AND claim_key LIKE '%~rereview-%'
            """,
            (pull_request["id"],),
        ).fetchone()["count"] + 1
        head_sha = details["headRefOid"]
        key = f"{repository}#{number}@{head_sha}~rereview-{sequence}"
        issue_ids = extract_linear_issue_ids(
            details.get("title"), details.get("body"), details.get("headRefName")
        ) or remembered_issue_ids
        author = details.get("author") or {}
        candidate = {
            "repository": repository,
            "number": number,
            "title": details["title"],
            "body": (details.get("body") or "")[:MAX_PR_BODY_CHARS],
            "body_truncated": len(details.get("body") or "") > MAX_PR_BODY_CHARS,
            "url": details["url"],
            "author": author.get("login") if isinstance(author, dict) else author,
            "base_ref": details["baseRefName"],
            "head_ref": details["headRefName"],
            "head_sha": head_sha,
            "key": key,
            "review_request_event_id": None,
            "review_requested_at": timestamp,
            "review_trigger": "user_rereview",
            "rereview_id": sequence,
            "updated_at": timestamp,
            "mergeable": details.get("mergeable"),
            "checks": details.get("statusCheckRollup", []),
            "labels": [
                label.get("name")
                for label in details.get("labels", [])
                if label.get("name")
            ],
            "changed_files": details.get("changedFiles"),
            "additions": details.get("additions"),
            "deletions": details.get("deletions"),
            "linear_issue_ids": issue_ids,
        }
        upsert_pull_request(connection, candidate)
        connection.execute(
            """
            INSERT INTO review_rounds(
                pull_request_id, claim_key, head_sha, base_ref, head_ref,
                review_requested_at, status, claimed_at, dispatched_at,
                updated_at, candidate_json
            ) VALUES (?, ?, ?, ?, ?, ?, 'dispatched', ?, ?, ?, ?)
            """,
            (
                pull_request["id"],
                key,
                head_sha,
                candidate.get("base_ref"),
                candidate.get("head_ref"),
                timestamp,
                timestamp,
                timestamp,
                timestamp,
                json.dumps(candidate, sort_keys=True),
            ),
        )
        review = connection.execute(
            "SELECT id FROM review_rounds WHERE claim_key = ?", (key,)
        ).fetchone()
        record_event(
            connection,
            "user_rereview_started",
            pull_request_id=pull_request["id"],
            review_round_id=review["id"],
            payload={"head_sha": head_sha, "rereview_id": sequence},
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    return prepare_claimed_candidate(config, state_path, key)


def prepare_related_pr(url: str, config: dict[str, Any]) -> dict[str, Any]:
    """Prepare an explicitly linked PR without creating or updating queue state."""
    match = GITHUB_PR_URL_PATTERN.fullmatch(url)
    if not match:
        raise QueueError(
            "Related PR must be a canonical https://github.com/OWNER/REPO/pull/NUMBER URL"
        )

    details = pr_details(url)
    expected_number = int(match.group("number"))
    if details.get("number") != expected_number:
        raise QueueError(
            f"GitHub returned PR #{details.get('number')} for requested PR #{expected_number}"
        )

    repository = f"{match.group('owner')}/{match.group('repo')}"
    candidate = {
        "repository": repository,
        "number": expected_number,
        "title": details["title"],
        "url": details["url"],
        "base_ref": details["baseRefName"],
        "head_ref": details["headRefName"],
        "head_sha": details["headRefOid"],
    }
    destination = prepare_checkout(candidate, config)
    return {
        **candidate,
        "checkout_path": str(destination),
        "diff_range": f"origin/{candidate['base_ref']}...{candidate['head_sha']}",
    }


def checkout_path(candidate: dict[str, Any]) -> Path:
    safe_repo = candidate["repository"].replace("/", "--")
    return ROOT / ".cache" / "checkouts" / safe_repo / f"pr-{candidate['number']}"


def prepare_checkout(candidate: dict[str, Any], config: dict[str, Any]) -> Path:
    destination = checkout_path(candidate)
    destination.parent.mkdir(parents=True, exist_ok=True)
    repository = candidate["repository"]
    created = not destination.exists()

    if created:
        local_source = config.get("local_repositories", {}).get(repository)
        if local_source and Path(local_source).is_dir():
            run(
                [
                    "git",
                    "clone",
                    "--no-checkout",
                    "--reference-if-able",
                    local_source,
                    f"https://github.com/{repository}.git",
                    str(destination),
                ]
            )
        else:
            run(
                [
                    "gh",
                    "repo",
                    "clone",
                    repository,
                    str(destination),
                    "--",
                    "--no-checkout",
                    "--filter=blob:none",
                ]
            )

    if not (destination / ".git").exists():
        raise QueueError(f"Checkout path is not a Git repository: {destination}")
    # A fresh --no-checkout clone reports every indexed file as deleted until
    # the first checkout, so only enforce cleanliness on reused checkouts.
    if not created and run(["git", "status", "--porcelain"], cwd=destination):
        raise QueueError(f"Cached checkout has local changes: {destination}")

    number = str(candidate["number"])
    base = candidate["base_ref"]
    run(
        [
            "git",
            "fetch",
            "--force",
            "origin",
            f"+refs/pull/{number}/head:refs/remotes/origin/pr/{number}",
            f"+refs/heads/{base}:refs/remotes/origin/{base}",
        ],
        cwd=destination,
    )
    run(["git", "cat-file", "-e", f"{candidate['head_sha']}^{{commit}}"], cwd=destination)
    run(["git", "checkout", "--detach", candidate["head_sha"]], cwd=destination)
    return destination


def update_entry(state_path: Path, key: str, status: str, **fields: Any) -> dict[str, Any]:
    column_aliases = {"report": "report_path", "reason": "error"}
    allowed = {
        "claimed_at",
        "dispatched_at",
        "prepared_at",
        "completed_at",
        "failed_at",
        "report_path",
        "findings_path",
        "checkout_path",
        "error",
    }
    normalized = {column_aliases.get(name, name): value for name, value in fields.items()}
    unexpected = set(normalized) - allowed
    if unexpected:
        raise QueueError(f"Unsupported review state fields: {sorted(unexpected)}")

    connection = connect_state(state_path)
    connection.execute("BEGIN IMMEDIATE")
    try:
        entry = connection.execute(
            "SELECT * FROM review_rounds WHERE claim_key = ?", (key,)
        ).fetchone()
        if entry is None:
            raise QueueError(f"Unknown claim key: {key}")
        values = {"status": status, "updated_at": isoformat(), **normalized}
        assignments = ", ".join(f"{name} = ?" for name in values)
        connection.execute(
            f"UPDATE review_rounds SET {assignments} WHERE id = ?",
            (*values.values(), entry["id"]),
        )
        record_event(
            connection,
            f"review_{status}",
            pull_request_id=entry["pull_request_id"],
            review_round_id=entry["id"],
            payload=normalized,
        )
        connection.commit()
        updated = connection.execute(
            "SELECT * FROM review_rounds WHERE id = ?", (entry["id"],)
        ).fetchone()
        return dict(updated)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def reset_entry(state_path: Path, key: str) -> None:
    connection = connect_state(state_path)
    connection.execute("BEGIN IMMEDIATE")
    try:
        entry = connection.execute(
            "SELECT * FROM review_rounds WHERE claim_key = ?", (key,)
        ).fetchone()
        if entry is None:
            raise QueueError(f"Unknown claim key: {key}")
        external = connection.execute(
            "SELECT 1 FROM github_reviews WHERE review_round_id = ?", (entry["id"],)
        ).fetchone()
        if entry["status"] == "completed" or external:
            raise QueueError("Completed or externally drafted reviews cannot be reset")
        record_event(
            connection,
            "review_reset",
            pull_request_id=entry["pull_request_id"],
            payload={"claim_key": key, "previous_status": entry["status"]},
        )
        connection.execute("DELETE FROM review_rounds WHERE id = ?", (entry["id"],))
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def heartbeat_review(state_path: Path, key: str) -> dict[str, Any]:
    """Refresh an active review's lease without changing its lifecycle state."""
    connection = connect_state(state_path)
    connection.execute("BEGIN IMMEDIATE")
    try:
        review, pull_request = get_round(connection, key)
        if review["status"] not in ACTIVE_STATUSES:
            raise QueueError(
                f"Only an active review can be refreshed ({review['status']})"
            )
        timestamp = isoformat()
        connection.execute(
            "UPDATE review_rounds SET claimed_at = ?, updated_at = ? WHERE id = ?",
            (timestamp, timestamp, review["id"]),
        )
        record_event(
            connection,
            "review_heartbeat",
            pull_request_id=pull_request["id"],
            review_round_id=review["id"],
        )
        connection.commit()
        return {"claim_key": key, "status": review["status"], "refreshed_at": timestamp}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def bind_task(
    state_path: Path,
    key: str,
    *,
    thread_id: str | None,
    host_id: str | None,
    client_thread_id: str | None,
) -> dict[str, Any]:
    if not thread_id and not client_thread_id:
        raise QueueError("A task thread ID or queued client thread ID is required")
    connection = connect_state(state_path)
    connection.execute("BEGIN IMMEDIATE")
    try:
        review = connection.execute(
            "SELECT * FROM review_rounds WHERE claim_key = ?", (key,)
        ).fetchone()
        if review is None:
            raise QueueError(f"Unknown claim key: {key}")
        timestamp = isoformat()
        connection.execute(
            """
            UPDATE pull_requests SET
                task_thread_id = COALESCE(?, task_thread_id),
                task_host_id = COALESCE(?, task_host_id),
                task_client_thread_id = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                thread_id,
                host_id,
                None if thread_id else client_thread_id,
                timestamp,
                review["pull_request_id"],
            ),
        )
        if review["status"] == "claimed":
            connection.execute(
                """
                UPDATE review_rounds
                SET status = 'dispatched', dispatched_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (timestamp, timestamp, review["id"]),
            )
        record_event(
            connection,
            "task_bound",
            pull_request_id=review["pull_request_id"],
            review_round_id=review["id"],
            payload={
                "thread_id": thread_id,
                "host_id": host_id,
                "client_thread_id": client_thread_id,
            },
        )
        connection.commit()
        row = connection.execute(
            "SELECT * FROM pull_requests WHERE id = ?",
            (review["pull_request_id"],),
        ).fetchone()
        return dict(row)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def findings_path(candidate: dict[str, Any]) -> Path:
    return report_path(candidate).with_suffix(".findings.json")


def load_findings_document(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text())
    except FileNotFoundError as exc:
        raise QueueError(f"Findings file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise QueueError(f"Invalid findings JSON in {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise QueueError("Findings document must be an object")
    findings = document.get("findings", [])
    dispositions = document.get("previous_findings", [])
    if not isinstance(findings, list) or not isinstance(dispositions, list):
        raise QueueError("findings and previous_findings must be arrays")
    return {"findings": findings, "previous_findings": dispositions}


def validate_finding(raw: dict[str, Any], *, id_prefix: str = "F") -> dict[str, Any]:
    required = {
        "id",
        "severity",
        "title",
        "path",
        "explanation",
        "failure_example",
        "safeguard",
        "safeguard_kind",
        "review_comment",
    }
    if not isinstance(raw, dict):
        raise QueueError("Each finding must be an object")
    missing = required - raw.keys()
    if missing:
        raise QueueError(
            f"Finding is missing required fields: {sorted(missing)}"
        )
    if not re.fullmatch(rf"{re.escape(id_prefix)}-\d{{2}}", str(raw["id"])):
        raise QueueError(f"Invalid finding ID: {raw['id']}")
    if raw["severity"] not in {"P0", "P1", "P2", "P3"}:
        raise QueueError(f"Invalid severity for {raw['id']}: {raw['severity']}")
    if raw["safeguard_kind"] not in {"implementation", "regression_test"}:
        raise QueueError(f"Invalid safeguard_kind for {raw['id']}")
    for text_field in required - {"id", "severity", "safeguard_kind"}:
        value = raw[text_field]
        if not isinstance(value, str) or not value.strip():
            raise QueueError(f"{text_field} for {raw['id']} must be non-empty text")
    for line_field in ("start_line", "end_line"):
        value = raw.get(line_field)
        if value is not None and (not isinstance(value, int) or value < 1):
            raise QueueError(f"{line_field} for {raw['id']} must be a positive integer")
    if (
        raw.get("start_line") is not None
        and raw.get("end_line") is not None
        and raw["end_line"] < raw["start_line"]
    ):
        raise QueueError(f"end_line for {raw['id']} cannot precede start_line")
    normalized = {name: raw.get(name) for name in required}
    normalized["start_line"] = raw.get("start_line")
    normalized["end_line"] = raw.get("end_line") or raw.get("start_line")
    fingerprint_source = "\n".join(
        str(normalized[name]).strip().lower()
        for name in ("path", "title", "failure_example")
    )
    normalized["fingerprint"] = hashlib.sha256(
        fingerprint_source.encode("utf-8")
    ).hexdigest()
    return normalized


def complete_review(
    state_path: Path,
    key: str,
    report: Path,
    findings_document: Path | None,
) -> dict[str, Any]:
    if not report.is_file():
        raise QueueError(f"Report not found: {report}")
    document = (
        load_findings_document(findings_document)
        if findings_document is not None
        else {"findings": [], "previous_findings": []}
    )
    findings = []
    for item in document["findings"]:
        finding = validate_finding(item)
        kind = item.get("kind", "defect")
        if not isinstance(kind, str) or kind not in AUTOMATED_FINDING_KINDS:
            raise QueueError(
                f"Invalid automated finding kind for {finding['id']}: {kind}; "
                f"choose from {sorted(AUTOMATED_FINDING_KINDS)}"
            )
        finding["kind"] = kind
        findings.append(finding)
    finding_ids = [item["id"] for item in findings]
    if len(finding_ids) != len(set(finding_ids)):
        raise QueueError("Finding IDs must be unique within a review round")

    connection = connect_state(state_path)
    connection.execute("BEGIN IMMEDIATE")
    try:
        review = connection.execute(
            "SELECT * FROM review_rounds WHERE claim_key = ?", (key,)
        ).fetchone()
        if review is None:
            raise QueueError(f"Unknown claim key: {key}")
        if review["status"] not in {"reviewing", "preparing", "dispatched", "claimed"}:
            raise QueueError(f"Review cannot be completed from status {review['status']}")
        disposition_pairs: list[tuple[str, str]] = []
        for disposition in document["previous_findings"]:
            if not isinstance(disposition, dict):
                raise QueueError("Previous finding dispositions must be objects")
            source_key = disposition.get("claim_key")
            finding_key = disposition.get("finding_id")
            if not isinstance(source_key, str) or not isinstance(finding_key, str):
                raise QueueError(
                    "Previous finding dispositions require claim_key and finding_id"
                )
            disposition_pairs.append((source_key, finding_key))
        if len(disposition_pairs) != len(set(disposition_pairs)):
            raise QueueError("Previous finding dispositions must be unique")

        carried_rows = active_carried_findings(
            connection, review["pull_request_id"], review["id"]
        )
        carried_pairs = {
            (row["claim_key"], row["finding_key"]) for row in carried_rows
        }
        provided_pairs = set(disposition_pairs)
        if provided_pairs != carried_pairs:
            missing = sorted(carried_pairs - provided_pairs)
            unexpected = sorted(provided_pairs - carried_pairs)
            raise QueueError(
                "Previous finding dispositions must reconcile every carried finding; "
                f"missing={missing}, unexpected={unexpected}"
            )
        timestamp = isoformat()
        for finding in findings:
            connection.execute(
                """
                INSERT INTO findings(
                    review_round_id, finding_key, severity, title, path,
                    start_line, end_line, explanation, failure_example,
                    safeguard, safeguard_kind, review_comment, fingerprint,
                    source, kind, added_after_completion, status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          'agent', ?, 0, 'proposed', ?, ?)
                """,
                (
                    review["id"],
                    finding["id"],
                    finding["severity"],
                    finding["title"],
                    finding["path"],
                    finding["start_line"],
                    finding["end_line"],
                    finding["explanation"],
                    finding["failure_example"],
                    finding["safeguard"],
                    finding["safeguard_kind"],
                    finding["review_comment"],
                    finding["fingerprint"],
                    finding["kind"],
                    timestamp,
                    timestamp,
                ),
            )

        carried_by_pair = {
            (row["claim_key"], row["finding_key"]): row for row in carried_rows
        }
        carried_into_round: list[dict[str, str]] = []
        for disposition in document["previous_findings"]:
            status = disposition.get("status")
            if status not in {"resolved", "still_open", "obsolete"}:
                raise QueueError(f"Invalid previous finding status: {status}")
            source_key = disposition.get("claim_key")
            finding_key = disposition.get("finding_id")
            source = connection.execute(
                """
                SELECT f.id, rr.pull_request_id FROM findings f
                JOIN review_rounds rr ON rr.id = f.review_round_id
                WHERE rr.claim_key = ? AND f.finding_key = ?
                """,
                (source_key, finding_key),
            ).fetchone()
            if source is None or source["pull_request_id"] != review["pull_request_id"]:
                raise QueueError(
                    f"Previous finding not found on this PR: {source_key} {finding_key}"
                )
            connection.execute(
                """
                UPDATE findings SET status = ?, decision_note = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, disposition.get("note"), timestamp, source["id"]),
            )
            if status == "still_open":
                source_finding = carried_by_pair[(source_key, finding_key)]
                origin_id = (
                    source_finding["origin_finding_id"] or source_finding["id"]
                )
                current_match = connection.execute(
                    """
                    SELECT * FROM findings
                    WHERE review_round_id = ?
                      AND (origin_finding_id = ? OR fingerprint = ?)
                    ORDER BY id LIMIT 1
                    """,
                    (review["id"], origin_id, source_finding["fingerprint"]),
                ).fetchone()
                if current_match is None:
                    used_keys = {
                        row["finding_key"]
                        for row in connection.execute(
                            "SELECT finding_key FROM findings WHERE review_round_id = ?",
                            (review["id"],),
                        ).fetchall()
                    }
                    carried_key = source_finding["finding_key"]
                    if carried_key in used_keys:
                        prefix = (
                            carried_key[0]
                            if carried_key[:1] in {"F", "U"}
                            else "F"
                        )
                        sequence = 1
                        while f"{prefix}-{sequence:02d}" in used_keys:
                            sequence += 1
                        carried_key = f"{prefix}-{sequence:02d}"
                    connection.execute(
                        """
                        INSERT INTO findings(
                            review_round_id, finding_key, severity, title, path,
                            start_line, end_line, explanation, failure_example,
                            safeguard, safeguard_kind, review_comment, fingerprint,
                            source, kind, added_after_completion, source_note,
                            origin_finding_id, status, decision_note,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                  0, ?, ?, 'accepted', ?, ?, ?)
                        """,
                        (
                            review["id"],
                            carried_key,
                            source_finding["severity"],
                            source_finding["title"],
                            source_finding["path"],
                            source_finding["start_line"],
                            source_finding["end_line"],
                            source_finding["explanation"],
                            source_finding["failure_example"],
                            source_finding["safeguard"],
                            source_finding["safeguard_kind"],
                            source_finding["review_comment"],
                            source_finding["fingerprint"],
                            source_finding["source"],
                            source_finding["kind"],
                            source_finding["source_note"],
                            origin_id,
                            (
                                f"Carried from {source_key} {finding_key} after "
                                "current-head verification."
                            ),
                            timestamp,
                            timestamp,
                        ),
                    )
                else:
                    carried_key = current_match["finding_key"]
                    connection.execute(
                        """
                        UPDATE findings SET origin_finding_id = ?, source = ?,
                            kind = ?, source_note = ?, status = 'accepted',
                            updated_at = ? WHERE id = ?
                        """,
                        (
                            origin_id,
                            source_finding["source"],
                            source_finding["kind"],
                            source_finding["source_note"],
                            timestamp,
                            current_match["id"],
                        ),
                    )
                carried_into_round.append(
                    {
                        "source_claim_key": source_key,
                        "source_finding_id": finding_key,
                        "finding_id": carried_key,
                    }
                )
            record_event(
                connection,
                "previous_finding_reconciled",
                pull_request_id=review["pull_request_id"],
                review_round_id=review["id"],
                payload={
                    "source_claim_key": source_key,
                    "finding_id": finding_key,
                    "status": status,
                    "note": disposition.get("note"),
                },
            )

        connection.execute(
            """
            UPDATE review_rounds SET status = 'completed', completed_at = ?,
                updated_at = ?, report_path = ?, findings_path = ?
            WHERE id = ?
            """,
            (
                timestamp,
                timestamp,
                str(report),
                str(findings_document) if findings_document else None,
                review["id"],
            ),
        )
        record_event(
            connection,
            "review_completed",
            pull_request_id=review["pull_request_id"],
            review_round_id=review["id"],
            payload={
                "report": str(report),
                "findings": finding_ids,
                "carried_findings": carried_into_round,
            },
        )
        connection.commit()
        updated = connection.execute(
            "SELECT * FROM review_rounds WHERE id = ?", (review["id"],)
        ).fetchone()
        return dict(updated)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def get_round(
    connection: sqlite3.Connection, key: str
) -> tuple[sqlite3.Row, sqlite3.Row]:
    review = connection.execute(
        "SELECT * FROM review_rounds WHERE claim_key = ?", (key,)
    ).fetchone()
    if review is None:
        raise QueueError(f"Unknown claim key: {key}")
    pull_request = connection.execute(
        "SELECT * FROM pull_requests WHERE id = ?", (review["pull_request_id"],)
    ).fetchone()
    assert pull_request is not None
    return review, pull_request


def load_user_finding(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError as exc:
        raise QueueError(f"User finding file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise QueueError(f"Invalid user finding JSON in {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise QueueError("User finding document must be an object")
    return raw


def load_finding_edit(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError as exc:
        raise QueueError(f"Finding edit file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise QueueError(f"Invalid finding edit JSON in {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise QueueError("Finding edit document must be an object")
    unknown = set(raw) - EDITABLE_FINDING_FIELDS - {"id"}
    if unknown:
        raise QueueError(f"Finding edit contains unsupported fields: {sorted(unknown)}")
    if not (set(raw) & EDITABLE_FINDING_FIELDS):
        raise QueueError("Finding edit must change at least one editable field")
    return raw


def add_user_finding(
    state_path: Path,
    key: str,
    finding_document: Path,
    *,
    accept: bool,
) -> dict[str, Any]:
    """Append verified user feedback to a completed review round."""
    raw = load_user_finding(finding_document)
    kind = raw.get("kind", "required_change")
    if kind not in USER_FINDING_KINDS:
        raise QueueError(
            f"Invalid user finding kind: {kind}; choose from "
            f"{sorted(USER_FINDING_KINDS)}"
        )
    source_note = raw.get("source_note")
    if source_note is not None and (
        not isinstance(source_note, str) or not source_note.strip()
    ):
        raise QueueError("source_note must be non-empty text when provided")

    connection = connect_state(state_path)
    try:
        review, pull_request = get_round(connection, key)
        if review["status"] != "completed":
            raise QueueError("User feedback can only be added to a completed review")
        expected_round_id = review["id"]
        expected_head = review["head_sha"]
        pr_url = pull_request["url"]
    finally:
        connection.close()

    if current_pr_head(pr_url) != expected_head:
        raise QueueError(
            "PR head changed after this review; verify the feedback against a "
            "completed round for the current head before adding it"
        )

    connection = connect_state(state_path)
    connection.execute("BEGIN IMMEDIATE")
    try:
        review, pull_request = get_round(connection, key)
        if review["id"] != expected_round_id or review["head_sha"] != expected_head:
            raise QueueError("Review round changed while adding user feedback")
        if review["status"] != "completed":
            raise QueueError("User feedback can only be added to a completed review")

        existing_keys = {
            row["finding_key"]
            for row in connection.execute(
                "SELECT finding_key FROM findings WHERE review_round_id = ?",
                (review["id"],),
            ).fetchall()
        }
        sequence = 1
        while f"U-{sequence:02d}" in existing_keys:
            sequence += 1
        candidate = dict(raw)
        candidate["id"] = f"U-{sequence:02d}"
        candidate.pop("kind", None)
        candidate.pop("source_note", None)
        finding = validate_finding(candidate, id_prefix="U")

        duplicate = connection.execute(
            """
            SELECT * FROM findings
            WHERE review_round_id = ? AND fingerprint = ?
            ORDER BY id LIMIT 1
            """,
            (review["id"], finding["fingerprint"]),
        ).fetchone()
        timestamp = isoformat()
        if duplicate is not None:
            accepted_now = False
            if accept and duplicate["status"] == "proposed":
                connection.execute(
                    "UPDATE findings SET status = 'accepted', updated_at = ? WHERE id = ?",
                    (timestamp, duplicate["id"]),
                )
                accepted_now = True
            record_event(
                connection,
                "user_finding_deduplicated",
                pull_request_id=pull_request["id"],
                review_round_id=review["id"],
                payload={
                    "finding_id": duplicate["finding_key"],
                    "requested_kind": kind,
                    "accepted": accepted_now,
                },
            )
            connection.commit()
            return {
                "claim_key": key,
                "finding_id": duplicate["finding_key"],
                "source": duplicate["source"],
                "kind": duplicate["kind"],
                "status": "accepted" if accepted_now else duplicate["status"],
                "deduplicated": True,
            }

        status = "accepted" if accept else "proposed"
        connection.execute(
            """
            INSERT INTO findings(
                review_round_id, finding_key, severity, title, path,
                start_line, end_line, explanation, failure_example,
                safeguard, safeguard_kind, review_comment, fingerprint,
                source, kind, added_after_completion, source_note, status,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      'user', ?, 1, ?, ?, ?, ?)
            """,
            (
                review["id"],
                finding["id"],
                finding["severity"],
                finding["title"],
                finding["path"],
                finding["start_line"],
                finding["end_line"],
                finding["explanation"],
                finding["failure_example"],
                finding["safeguard"],
                finding["safeguard_kind"],
                finding["review_comment"],
                finding["fingerprint"],
                kind,
                source_note.strip() if source_note else None,
                status,
                timestamp,
                timestamp,
            ),
        )
        record_event(
            connection,
            "user_finding_added",
            pull_request_id=pull_request["id"],
            review_round_id=review["id"],
            payload={
                "finding_id": finding["id"],
                "kind": kind,
                "accepted": accept,
            },
        )
        connection.commit()
        return {
            "claim_key": key,
            "finding_id": finding["id"],
            "source": "user",
            "kind": kind,
            "status": status,
            "deduplicated": False,
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def decide_findings(
    state_path: Path,
    key: str,
    *,
    accept: list[str],
    reject: list[str],
    note: str | None,
) -> dict[str, Any]:
    if not accept and not reject:
        raise QueueError("Choose at least one finding to accept or reject")
    if set(accept) & set(reject):
        raise QueueError("A finding cannot be accepted and rejected together")
    connection = connect_state(state_path)
    connection.execute("BEGIN IMMEDIATE")
    try:
        review, pull_request = get_round(connection, key)
        if review["status"] != "completed":
            raise QueueError("Finding decisions require a completed review")
        available = {
            row["finding_key"]: row
            for row in connection.execute(
                "SELECT * FROM findings WHERE review_round_id = ?", (review["id"],)
            ).fetchall()
        }
        missing = (set(accept) | set(reject)) - set(available)
        if missing:
            raise QueueError(f"Unknown findings for this review: {sorted(missing)}")
        timestamp = isoformat()
        for finding_key, status in [
            *((item, "accepted") for item in accept),
            *((item, "rejected") for item in reject),
        ]:
            current = available[finding_key]["status"]
            if current in {"drafted", "submitted", "resolved", "obsolete"}:
                raise QueueError(
                    f"{finding_key} cannot change decision from status {current}"
                )
            connection.execute(
                """
                UPDATE findings SET status = ?, decision_note = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, note, timestamp, available[finding_key]["id"]),
            )
            record_event(
                connection,
                "finding_decided",
                pull_request_id=pull_request["id"],
                review_round_id=review["id"],
                payload={
                    "finding_id": finding_key,
                    "decision": status,
                    "note": note,
                },
            )
        connection.commit()
        return {
            "claim_key": key,
            "accepted": accept,
            "rejected": reject,
            "note": note,
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def selected_findings(
    connection: sqlite3.Connection,
    review_round_id: int,
    finding_keys: list[str] | None,
) -> list[sqlite3.Row]:
    rows = connection.execute(
        "SELECT * FROM findings WHERE review_round_id = ? ORDER BY id",
        (review_round_id,),
    ).fetchall()
    available = {row["finding_key"]: row for row in rows}
    if finding_keys:
        missing = set(finding_keys) - set(available)
        if missing:
            raise QueueError(f"Unknown findings for this review: {sorted(missing)}")
        selected = [available[key] for key in finding_keys]
    else:
        selected = [
            row for row in rows if row["status"] in {"accepted", "drafted"}
        ]
    if not selected:
        raise QueueError("No accepted findings selected for the PR review")
    unaccepted = [
        row["finding_key"]
        for row in selected
        if row["status"] not in {"accepted", "drafted"}
    ]
    if unaccepted:
        raise QueueError(
            f"Only explicitly accepted findings can be drafted: {unaccepted}"
        )
    return selected


def build_review_payload(
    review: sqlite3.Row,
    findings: list[sqlite3.Row | dict[str, Any]],
) -> tuple[dict[str, Any], str]:
    body = (
        f"Review of `{review['head_sha'][:12]}`. "
        f"This review includes {len(findings)} item"
        f"{'s' if len(findings) != 1 else ''} to address before merge."
    )
    comments: list[dict[str, Any]] = []
    body_only: list[str] = []
    for finding in findings:
        if finding["end_line"]:
            comments.append(
                {
                    "path": finding["path"],
                    "line": finding["end_line"],
                    "side": "RIGHT",
                    "body": finding["review_comment"],
                }
            )
        else:
            body_only.append(
                f"### {finding['finding_key']} — {finding['title']}\n\n"
                f"{finding['review_comment']}"
            )
    if body_only:
        body = f"{body}\n\n" + "\n\n".join(body_only)
    payload = {
        "commit_id": review["head_sha"],
        "body": body,
        "comments": comments,
    }
    payload_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return payload, payload_hash


def preview_review(
    state_path: Path, key: str, finding_keys: list[str] | None
) -> dict[str, Any]:
    connection = connect_state(state_path)
    try:
        review, pull_request = get_round(connection, key)
        if review["status"] != "completed":
            raise QueueError("Only a completed review can be drafted")
        findings = selected_findings(connection, review["id"], finding_keys)
        payload, payload_hash = build_review_payload(review, findings)
        return {
            "claim_key": key,
            "repository": pull_request["repository"],
            "number": pull_request["number"],
            "url": pull_request["url"],
            "head_sha": review["head_sha"],
            "finding_ids": [row["finding_key"] for row in findings],
            "findings": [
                {
                    "id": row["finding_key"],
                    "title": row["title"],
                    "source": row["source"],
                    "kind": row["kind"],
                }
                for row in findings
            ],
            "payload_hash": payload_hash,
            "review": payload,
        }
    finally:
        connection.close()


def current_pr_head(url: str) -> str:
    return pr_details(url)["headRefOid"]


def remote_pending_reviews(repository: str, number: int) -> list[dict[str, Any]]:
    login = github_login()
    reviews = run_json(
        ["gh", "api", f"repos/{repository}/pulls/{number}/reviews?per_page=100"]
    )
    return [
        review
        for review in reviews
        if review.get("state") == "PENDING"
        and (review.get("user") or {}).get("login") == login
    ]


def recorded_prior_pending_review(
    state_path: Path,
    key: str,
    remote_pending: list[dict[str, Any]],
) -> dict[str, Any] | None:
    remote_ids = {
        int(item["id"])
        for item in remote_pending
        if item.get("id") is not None
    }
    if not remote_ids:
        return None
    connection = connect_state(state_path)
    try:
        review, _ = get_round(connection, key)
        placeholders = ",".join("?" for _ in remote_ids)
        rows = connection.execute(
            f"""
            SELECT gr.*, rr.claim_key AS source_claim_key
            FROM github_reviews gr
            JOIN review_rounds rr ON rr.id = gr.review_round_id
            WHERE rr.pull_request_id = ? AND rr.id != ?
              AND gr.state = 'PENDING'
              AND gr.github_review_id IN ({placeholders})
            ORDER BY gr.id DESC
            """,
            (review["pull_request_id"], review["id"], *sorted(remote_ids)),
        ).fetchall()
        if len(rows) != 1 or len(remote_pending) != 1:
            return None
        return dict(rows[0])
    finally:
        connection.close()


def verify_recorded_pending_unchanged(
    repository: str,
    number: int,
    pending_review: dict[str, Any],
    state_path: Path,
) -> None:
    github_review_id = pending_review["github_review_id"]
    remote_review = run_json(
        [
            "gh",
            "api",
            f"repos/{repository}/pulls/{number}/reviews/{github_review_id}",
        ]
    )
    if remote_review.get("state") != "PENDING":
        raise QueueError(
            "The recorded prior review is no longer pending on GitHub: "
            f"{remote_review.get('state')}"
        )
    if (remote_review.get("body") or "") != (pending_review.get("body") or ""):
        raise QueueError(
            "The prior pending review body was edited outside this workflow; "
            "it was left untouched"
        )

    connection = connect_state(state_path)
    try:
        local_comments = [
            dict(row)
            for row in connection.execute(
                """
                SELECT * FROM github_review_comments
                WHERE github_review_id = ? AND line IS NOT NULL
                ORDER BY id
                """,
                (pending_review["id"],),
            ).fetchall()
        ]
    finally:
        connection.close()
    remote_comments = run_json(
        [
            "gh",
            "api",
            f"repos/{repository}/pulls/{number}/reviews/"
            f"{github_review_id}/comments?per_page=100",
        ]
    )
    if len(remote_comments) != len(local_comments):
        raise QueueError(
            "The prior pending review comments changed outside this workflow; "
            "it was left untouched"
        )
    for local_comment in local_comments:
        remote_comment = match_remote_review_comment(local_comment, remote_comments)
        if remote_comment is None or remote_comment.get("body") != local_comment["body"]:
            raise QueueError(
                "A prior pending review comment was edited outside this workflow; "
                "it was left untouched"
            )


def mark_pending_review_deleted(
    state_path: Path,
    key: str,
    pending_review: dict[str, Any],
) -> None:
    connection = connect_state(state_path)
    connection.execute("BEGIN IMMEDIATE")
    try:
        review, pull_request = get_round(connection, key)
        stored = connection.execute(
            "SELECT * FROM github_reviews WHERE id = ?",
            (pending_review["id"],),
        ).fetchone()
        if stored is None or stored["state"] != "PENDING":
            raise QueueError("Prior pending review state changed during replacement")
        timestamp = isoformat()
        connection.execute(
            "UPDATE github_reviews SET state = 'DELETED', updated_at = ? WHERE id = ?",
            (timestamp, stored["id"]),
        )
        record_event(
            connection,
            "github_pending_review_deleted_for_rereview",
            pull_request_id=pull_request["id"],
            review_round_id=review["id"],
            payload={
                "deleted_github_review_id": stored["github_review_id"],
                "source_claim_key": pending_review["source_claim_key"],
            },
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def store_draft_review(
    state_path: Path,
    key: str,
    findings: list[str],
    payload: dict[str, Any],
    payload_hash: str,
    response: dict[str, Any],
) -> dict[str, Any]:
    connection = connect_state(state_path)
    connection.execute("BEGIN IMMEDIATE")
    try:
        review, pull_request = get_round(connection, key)
        timestamp = isoformat()
        review_id = int(response["id"])
        html_url = response.get("html_url") or (
            f"{pull_request['url']}#pullrequestreview-{review_id}"
        )
        connection.execute(
            """
            INSERT INTO github_reviews(
                review_round_id, github_review_id, state, event, commit_sha,
                body, html_url, payload_hash, created_at, updated_at
            ) VALUES (?, ?, 'PENDING', NULL, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(github_review_id) DO UPDATE SET
                state = 'PENDING', body = excluded.body,
                html_url = excluded.html_url, payload_hash = excluded.payload_hash,
                updated_at = excluded.updated_at
            """,
            (
                review["id"],
                review_id,
                review["head_sha"],
                payload["body"],
                html_url,
                payload_hash,
                timestamp,
                timestamp,
            ),
        )
        github_review = connection.execute(
            "SELECT id FROM github_reviews WHERE github_review_id = ?", (review_id,)
        ).fetchone()
        connection.execute(
            "DELETE FROM github_review_comments WHERE github_review_id = ?",
            (github_review["id"],),
        )
        finding_rows = {
            row["finding_key"]: row
            for row in connection.execute(
                "SELECT * FROM findings WHERE review_round_id = ?", (review["id"],)
            ).fetchall()
        }
        for finding_key in findings:
            finding = finding_rows[finding_key]
            connection.execute(
                """
                INSERT INTO github_review_comments(
                    github_review_id, finding_key, path, line, side, body, created_at
                ) VALUES (?, ?, ?, ?, 'RIGHT', ?, ?)
                """,
                (
                    github_review["id"],
                    finding_key,
                    finding["path"],
                    finding["end_line"],
                    finding["review_comment"],
                    timestamp,
                ),
            )
            connection.execute(
                "UPDATE findings SET status = 'drafted', updated_at = ? WHERE id = ?",
                (timestamp, finding["id"]),
            )
        record_event(
            connection,
            "github_review_drafted",
            pull_request_id=pull_request["id"],
            review_round_id=review["id"],
            payload={
                "github_review_id": review_id,
                "finding_ids": findings,
                "payload_hash": payload_hash,
            },
        )
        connection.commit()
        return {
            "github_review_id": review_id,
            "state": "PENDING",
            "html_url": html_url,
            "finding_ids": findings,
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def draft_review(
    state_path: Path,
    key: str,
    finding_keys: list[str] | None,
    confirmation: str,
) -> dict[str, Any]:
    if confirmation != "DRAFT":
        raise QueueError("Creating a GitHub draft requires --confirm DRAFT")
    preview = preview_review(state_path, key, finding_keys)
    if current_pr_head(preview["url"]) != preview["head_sha"]:
        raise QueueError("PR head changed after review; re-review before drafting comments")

    existing_review: dict[str, Any] | None = None
    connection = connect_state(state_path)
    try:
        review, _ = get_round(connection, key)
        existing = connection.execute(
            """
            SELECT * FROM github_reviews
            WHERE review_round_id = ? AND state = 'PENDING'
            ORDER BY id DESC LIMIT 1
            """,
            (review["id"],),
        ).fetchone()
        if existing and existing["payload_hash"] == preview["payload_hash"]:
            existing_review = dict(existing)
        if existing:
            if existing_review is None:
                raise QueueError(
                    "This review round already has a different pending GitHub review"
                )
    finally:
        connection.close()

    if existing_review:
        remote = run_json(
            [
                "gh",
                "api",
                f"repos/{preview['repository']}/pulls/{preview['number']}"
                f"/reviews/{existing_review['github_review_id']}",
            ]
        )
        if remote.get("state") != "PENDING":
            raise QueueError(
                "The locally recorded draft is no longer pending on GitHub: "
                f"{remote.get('state')}"
            )
        return {
            "github_review_id": existing_review["github_review_id"],
            "state": "PENDING",
            "html_url": existing_review["html_url"],
            "finding_ids": preview["finding_ids"],
            "idempotent": True,
        }

    pending = remote_pending_reviews(preview["repository"], preview["number"])
    replaced_review_id = None
    if pending:
        prior_pending = recorded_prior_pending_review(state_path, key, pending)
        if prior_pending is None:
            raise QueueError(
                "GitHub has a pending review that is not the single recorded prior "
                f"review for this PR: {pending[-1]['id']}"
            )
        verify_recorded_pending_unchanged(
            preview["repository"], preview["number"], prior_pending, state_path
        )
        replaced_review_id = prior_pending["github_review_id"]
        run(
            [
                "gh",
                "api",
                "--method",
                "DELETE",
                f"repos/{preview['repository']}/pulls/{preview['number']}"
                f"/reviews/{replaced_review_id}",
            ]
        )
        mark_pending_review_deleted(state_path, key, prior_pending)
        if current_pr_head(preview["url"]) != preview["head_sha"]:
            raise QueueError(
                "PR head changed while replacing the prior pending review; "
                "nothing was published"
            )
    response = run_json(
        [
            "gh",
            "api",
            "--method",
            "POST",
            f"repos/{preview['repository']}/pulls/{preview['number']}/reviews",
            "--input",
            "-",
        ],
        input_data=preview["review"],
    )
    result = store_draft_review(
        state_path,
        key,
        preview["finding_ids"],
        preview["review"],
        preview["payload_hash"],
        response,
    )
    if replaced_review_id is not None:
        result["replaced_github_review_id"] = replaced_review_id
    return result


def match_remote_review_comment(
    local_comment: dict[str, Any],
    remote_comments: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Match a stored inline item to its GitHub review comment."""
    comment_id = local_comment.get("github_comment_id")
    if comment_id is not None:
        matches = [item for item in remote_comments if item.get("id") == comment_id]
        return matches[0] if len(matches) == 1 else None

    candidates = [
        item
        for item in remote_comments
        if item.get("path") == local_comment.get("path")
    ]
    line = local_comment.get("line")
    if line is not None:
        line_matches = [
            item
            for item in candidates
            if line in {item.get("line"), item.get("original_line")}
        ]
        if len(line_matches) == 1:
            return line_matches[0]
        if line_matches:
            candidates = line_matches
    body_matches = [
        item for item in candidates if item.get("body") == local_comment.get("body")
    ]
    if len(body_matches) == 1:
        return body_matches[0]
    return candidates[0] if len(candidates) == 1 else None


def update_remote_review_comment(
    remote_comment: dict[str, Any], body: str
) -> dict[str, Any]:
    """Update a pending review comment through its GraphQL node id."""
    node_id = remote_comment.get("node_id")
    if not node_id:
        raise QueueError("GitHub review comment is missing its GraphQL node id")

    response = run_json(
        ["gh", "api", "graphql", "--input", "-"],
        input_data={
            "query": UPDATE_REVIEW_COMMENT_MUTATION,
            "variables": {"commentId": node_id, "body": body},
        },
    )
    errors = response.get("errors") or []
    if errors:
        messages = ", ".join(
            error.get("message", "unknown GraphQL error") for error in errors
        )
        raise QueueError(f"GitHub could not update the review comment: {messages}")

    updated = (
        response.get("data", {})
        .get("updatePullRequestReviewComment", {})
        .get("pullRequestReviewComment")
    )
    if not updated:
        raise QueueError("GitHub did not return the updated review comment")

    return {
        **remote_comment,
        "node_id": updated.get("id") or node_id,
        "body": updated.get("body") or body,
        "html_url": updated.get("url") or remote_comment.get("html_url"),
    }


def edit_draft_review(
    state_path: Path,
    key: str,
    finding_key: str,
    edit_document: Path,
    confirmation: str,
) -> dict[str, Any]:
    if confirmation != "EDIT":
        raise QueueError("Editing a GitHub draft requires --confirm EDIT")
    changes = load_finding_edit(edit_document)

    connection = connect_state(state_path)
    try:
        review, pull_request = get_round(connection, key)
        if review["status"] != "completed":
            raise QueueError("Only a completed review can have its draft edited")
        draft = connection.execute(
            """
            SELECT * FROM github_reviews
            WHERE review_round_id = ? AND state = 'PENDING'
            ORDER BY id DESC LIMIT 1
            """,
            (review["id"],),
        ).fetchone()
        if draft is None:
            raise QueueError("No recorded pending GitHub review exists for this round")
        finding = connection.execute(
            """
            SELECT * FROM findings
            WHERE review_round_id = ? AND finding_key = ?
            """,
            (review["id"], finding_key),
        ).fetchone()
        if finding is None:
            raise QueueError(f"Unknown finding for this review: {finding_key}")
        if finding["status"] != "drafted":
            raise QueueError(f"Finding is not part of the pending draft: {finding_key}")
        if changes.get("id") not in {None, finding_key}:
            raise QueueError(
                f"Finding edit ID {changes['id']} does not match {finding_key}"
            )

        merged = {
            "id": finding["finding_key"],
            "severity": finding["severity"],
            "title": finding["title"],
            "path": finding["path"],
            "start_line": finding["start_line"],
            "end_line": finding["end_line"],
            "explanation": finding["explanation"],
            "failure_example": finding["failure_example"],
            "safeguard": finding["safeguard"],
            "safeguard_kind": finding["safeguard_kind"],
            "review_comment": finding["review_comment"],
        }
        merged.update(
            {name: value for name, value in changes.items() if name in merged}
        )
        normalized = validate_finding(merged, id_prefix=finding_key[0])
        new_kind = changes.get("kind", finding["kind"])
        editable_kinds = USER_FINDING_KINDS | AUTOMATED_FINDING_KINDS
        if not isinstance(new_kind, str) or new_kind not in editable_kinds:
            raise QueueError(
                f"Invalid finding kind: {new_kind}; choose from "
                f"{sorted(editable_kinds)}"
            )
        new_source_note = changes.get("source_note", finding["source_note"])
        if new_source_note is not None and (
            not isinstance(new_source_note, str) or not new_source_note.strip()
        ):
            raise QueueError("source_note must be non-empty text when provided")
        if isinstance(new_source_note, str):
            new_source_note = new_source_note.strip()

        local_comments = [
            dict(row)
            for row in connection.execute(
                """
                SELECT * FROM github_review_comments
                WHERE github_review_id = ? ORDER BY id
                """,
                (draft["id"],),
            ).fetchall()
        ]
        local_comment = next(
            (
                item
                for item in local_comments
                if item["finding_key"] == finding_key
            ),
            None,
        )
        if local_comment is None:
            raise QueueError(f"Draft metadata is missing finding: {finding_key}")
        selected_keys = [item["finding_key"] for item in local_comments]
        selected_rows = []
        for selected_key in selected_keys:
            row = connection.execute(
                """
                SELECT * FROM findings
                WHERE review_round_id = ? AND finding_key = ?
                """,
                (review["id"], selected_key),
            ).fetchone()
            if row is None:
                raise QueueError(f"Draft metadata is missing finding: {selected_key}")
            selected_rows.append(dict(row))
        old_payload, _ = build_review_payload(review, selected_rows)
        for row in selected_rows:
            if row["finding_key"] == finding_key:
                row.update(normalized)
                row["finding_key"] = finding_key
                row["kind"] = new_kind
                row["source_note"] = new_source_note
        new_payload, new_payload_hash = build_review_payload(review, selected_rows)

        expected_round_id = review["id"]
        expected_head = review["head_sha"]
        expected_draft_id = draft["id"]
        github_review_id = draft["github_review_id"]
        repository = pull_request["repository"]
        number = pull_request["number"]
        url = pull_request["url"]
    finally:
        connection.close()

    if current_pr_head(url) != expected_head:
        raise QueueError("PR head changed after review; re-review before editing the draft")
    remote_review = run_json(
        [
            "gh",
            "api",
            f"repos/{repository}/pulls/{number}/reviews/{github_review_id}",
        ]
    )
    if remote_review.get("state") != "PENDING":
        raise QueueError(
            f"Remote GitHub review is not pending: {remote_review.get('state')}"
        )
    remote_comments = run_json(
        [
            "gh",
            "api",
            f"repos/{repository}/pulls/{number}/reviews/"
            f"{github_review_id}/comments?per_page=100",
        ]
    )

    remote_comment = None
    comment_response = None
    if local_comment["line"] is not None:
        remote_comment = match_remote_review_comment(local_comment, remote_comments)
        if remote_comment is None:
            raise QueueError(
                f"Could not uniquely match GitHub comment for {finding_key}"
            )
        if remote_comment.get("body") != normalized["review_comment"]:
            comment_response = update_remote_review_comment(
                remote_comment,
                normalized["review_comment"],
            )

    summary_changed = old_payload["body"] != new_payload["body"]
    review_response = None
    if summary_changed and remote_review.get("body") != new_payload["body"]:
        review_response = run_json(
            [
                "gh",
                "api",
                "--method",
                "PATCH",
                f"repos/{repository}/pulls/{number}/reviews/{github_review_id}",
                "--input",
                "-",
            ],
            input_data={"body": new_payload["body"]},
        )

    final_review_body = (
        (review_response or {}).get("body")
        if summary_changed
        else remote_review.get("body")
    ) or new_payload["body"]
    stored_payload = dict(new_payload)
    stored_payload["body"] = final_review_body
    stored_payload_hash = hashlib.sha256(
        json.dumps(stored_payload, sort_keys=True).encode("utf-8")
    ).hexdigest()
    changed_fields = sorted(
        name
        for name in EDITABLE_FINDING_FIELDS
        if name in changes and changes[name] != finding[name]
    )

    connection = connect_state(state_path)
    connection.execute("BEGIN IMMEDIATE")
    try:
        review, pull_request = get_round(connection, key)
        draft = connection.execute(
            "SELECT * FROM github_reviews WHERE id = ?",
            (expected_draft_id,),
        ).fetchone()
        if (
            review["id"] != expected_round_id
            or review["head_sha"] != expected_head
            or draft is None
            or draft["state"] != "PENDING"
            or draft["github_review_id"] != github_review_id
        ):
            raise QueueError("Draft state changed while editing the GitHub review")
        target = connection.execute(
            """
            SELECT * FROM findings
            WHERE review_round_id = ? AND finding_key = ?
            """,
            (review["id"], finding_key),
        ).fetchone()
        if target is None or target["status"] != "drafted":
            raise QueueError("Finding state changed while editing the GitHub review")
        timestamp = isoformat()
        connection.execute(
            """
            UPDATE findings SET severity = ?, title = ?, explanation = ?,
                failure_example = ?, safeguard = ?, safeguard_kind = ?,
                review_comment = ?, fingerprint = ?, kind = ?, source_note = ?,
                updated_at = ? WHERE id = ?
            """,
            (
                normalized["severity"],
                normalized["title"],
                normalized["explanation"],
                normalized["failure_example"],
                normalized["safeguard"],
                normalized["safeguard_kind"],
                normalized["review_comment"],
                normalized["fingerprint"],
                new_kind,
                new_source_note,
                timestamp,
                target["id"],
            ),
        )
        github_comment_id = (
            (comment_response or remote_comment or {}).get("id")
            if local_comment["line"] is not None
            else None
        )
        connection.execute(
            """
            UPDATE github_review_comments SET github_comment_id = ?, body = ?
            WHERE id = ?
            """,
            (
                github_comment_id,
                normalized["review_comment"],
                local_comment["id"],
            ),
        )
        connection.execute(
            """
            UPDATE github_reviews SET body = ?, payload_hash = ?, updated_at = ?
            WHERE id = ?
            """,
            (final_review_body, stored_payload_hash, timestamp, draft["id"]),
        )
        record_event(
            connection,
            "github_draft_edited",
            pull_request_id=pull_request["id"],
            review_round_id=review["id"],
            payload={
                "github_review_id": github_review_id,
                "finding_id": finding_key,
                "changed_fields": changed_fields,
                "payload_hash": new_payload_hash,
            },
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    response_comment = comment_response or remote_comment or {}
    return {
        "github_review_id": github_review_id,
        "state": "PENDING",
        "html_url": remote_review.get("html_url") or f"{url}#pullrequestreview-{github_review_id}",
        "finding_id": finding_key,
        "changed_fields": changed_fields,
        "comment_url": response_comment.get("html_url"),
        "idempotent": not comment_response and not review_response and not changed_fields,
    }


def request_changes(
    state_path: Path, key: str, confirmation: str
) -> dict[str, Any]:
    if confirmation != "REQUEST_CHANGES":
        raise QueueError(
            "Submitting a GitHub review requires --confirm REQUEST_CHANGES"
        )
    connection = connect_state(state_path)
    try:
        review, pull_request = get_round(connection, key)
        draft = connection.execute(
            """
            SELECT * FROM github_reviews
            WHERE review_round_id = ? ORDER BY id DESC LIMIT 1
            """,
            (review["id"],),
        ).fetchone()
        if draft is None:
            raise QueueError("No recorded pending GitHub review exists for this round")
        if draft["state"] == "CHANGES_REQUESTED":
            return {
                "github_review_id": draft["github_review_id"],
                "state": draft["state"],
                "html_url": draft["html_url"],
                "idempotent": True,
            }
        if draft["state"] != "PENDING":
            raise QueueError(f"GitHub review is not pending: {draft['state']}")
        review_id = draft["github_review_id"]
        expected_head = review["head_sha"]
        url = pull_request["url"]
        repository = pull_request["repository"]
        number = pull_request["number"]
    finally:
        connection.close()

    if current_pr_head(url) != expected_head:
        raise QueueError("PR head changed after review; re-review before requesting changes")
    remote = run_json(
        ["gh", "api", f"repos/{repository}/pulls/{number}/reviews/{review_id}"]
    )
    if remote.get("state") == "CHANGES_REQUESTED":
        response = remote
    elif remote.get("state") == "PENDING":
        response = run_json(
            [
                "gh",
                "api",
                "--method",
                "POST",
                f"repos/{repository}/pulls/{number}/reviews/{review_id}/events",
                "--input",
                "-",
            ],
            input_data={"event": "REQUEST_CHANGES"},
        )
    else:
        raise QueueError(f"Remote GitHub review is not pending: {remote.get('state')}")

    comments = run_json(
        [
            "gh",
            "api",
            f"repos/{repository}/pulls/{number}/reviews/{review_id}/comments?per_page=100",
        ]
    )
    connection = connect_state(state_path)
    connection.execute("BEGIN IMMEDIATE")
    try:
        review, pull_request = get_round(connection, key)
        draft = connection.execute(
            "SELECT * FROM github_reviews WHERE github_review_id = ?", (review_id,)
        ).fetchone()
        timestamp = isoformat()
        local_comments = [
            dict(row)
            for row in connection.execute(
                """
                SELECT * FROM github_review_comments
                WHERE github_review_id = ? ORDER BY id
                """,
                (draft["id"],),
            ).fetchall()
        ]
        for local_comment in local_comments:
            if local_comment["line"] is None:
                continue
            remote_comment = match_remote_review_comment(local_comment, comments)
            if remote_comment is None:
                continue
            connection.execute(
                """
                UPDATE github_review_comments
                SET github_comment_id = ?, body = ? WHERE id = ?
                """,
                (
                    remote_comment.get("id"),
                    remote_comment.get("body") or local_comment["body"],
                    local_comment["id"],
                ),
            )
            if remote_comment.get("body") and local_comment.get("finding_key"):
                connection.execute(
                    """
                    UPDATE findings SET review_comment = ?, updated_at = ?
                    WHERE review_round_id = ? AND finding_key = ?
                    """,
                    (
                        remote_comment["body"],
                        timestamp,
                        review["id"],
                        local_comment["finding_key"],
                    ),
                )
        remote_body = response.get("body") or draft["body"]
        selected_keys = [item["finding_key"] for item in local_comments]
        selected_rows = [
            connection.execute(
                """
                SELECT * FROM findings
                WHERE review_round_id = ? AND finding_key = ?
                """,
                (review["id"], finding_key),
            ).fetchone()
            for finding_key in selected_keys
        ]
        submitted_payload, _ = build_review_payload(review, selected_rows)
        submitted_payload["body"] = remote_body
        submitted_payload_hash = hashlib.sha256(
            json.dumps(submitted_payload, sort_keys=True).encode("utf-8")
        ).hexdigest()
        connection.execute(
            """
            UPDATE github_reviews SET state = 'CHANGES_REQUESTED',
                event = 'REQUEST_CHANGES', body = ?, html_url = ?, payload_hash = ?,
                submitted_at = ?, updated_at = ? WHERE id = ?
            """,
            (
                remote_body,
                response.get("html_url") or draft["html_url"],
                submitted_payload_hash,
                response.get("submitted_at") or timestamp,
                timestamp,
                draft["id"],
            ),
        )
        connection.execute(
            """
            UPDATE findings SET status = 'submitted', updated_at = ?
            WHERE review_round_id = ? AND status = 'drafted'
            """,
            (timestamp, review["id"]),
        )
        record_event(
            connection,
            "github_changes_requested",
            pull_request_id=pull_request["id"],
            review_round_id=review["id"],
            payload={"github_review_id": review_id},
        )
        connection.commit()
        return {
            "github_review_id": review_id,
            "state": "CHANGES_REQUESTED",
            "html_url": response.get("html_url") or draft["html_url"],
            "submitted_at": response.get("submitted_at") or timestamp,
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def review_history(
    state_path: Path, repository: str, number: int
) -> dict[str, Any]:
    connection = connect_state(state_path)
    try:
        pull_request = connection.execute(
            "SELECT * FROM pull_requests WHERE repository = ? AND number = ?",
            (repository, number),
        ).fetchone()
        if pull_request is None:
            raise QueueError(f"PR not found in local history: {repository}#{number}")
        rounds = []
        for review in connection.execute(
            """
            SELECT * FROM review_rounds WHERE pull_request_id = ?
            ORDER BY id DESC
            """,
            (pull_request["id"],),
        ).fetchall():
            findings = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT finding_key, severity, title, path, start_line, end_line,
                           status, decision_note, failure_example, safeguard,
                           safeguard_kind, review_comment, source, kind,
                           added_after_completion, source_note, origin_finding_id
                    FROM findings WHERE review_round_id = ? ORDER BY id
                    """,
                    (review["id"],),
                ).fetchall()
            ]
            github_reviews = []
            for github_review in connection.execute(
                """
                SELECT id, github_review_id, state, event, commit_sha, body,
                       html_url, payload_hash, created_at, submitted_at, updated_at
                FROM github_reviews WHERE review_round_id = ? ORDER BY id
                """,
                (review["id"],),
            ).fetchall():
                review_data = dict(github_review)
                local_review_id = review_data.pop("id")
                review_data["comments"] = [
                    dict(row)
                    for row in connection.execute(
                        """
                        SELECT finding_key, github_comment_id, path, line, side,
                               body, created_at
                        FROM github_review_comments
                        WHERE github_review_id = ? ORDER BY id
                        """,
                        (local_review_id,),
                    ).fetchall()
                ]
                github_reviews.append(review_data)
            round_data = dict(review)
            round_data.pop("candidate_json", None)
            round_data["findings"] = findings
            round_data["github_reviews"] = github_reviews
            rounds.append(round_data)
        pr_data = dict(pull_request)
        pr_data["linear_issue_ids"] = json.loads(
            pr_data.pop("linear_issue_ids_json") or "[]"
        )
        return {"pull_request": pr_data, "review_rounds": rounds}
    finally:
        connection.close()


def report_path(candidate: dict[str, Any]) -> Path:
    date = now().date().isoformat()
    repo = candidate["repository"].replace("/", "--")
    short_sha = candidate["head_sha"][:12]
    request_id = candidate.get("review_request_event_id")
    rereview_id = candidate.get("rereview_id")
    request_suffix = f"--request-{request_id}" if request_id else ""
    if rereview_id is not None:
        request_suffix = f"--rereview-{rereview_id}"
    filename = (
        f"{repo}--pr-{candidate['number']}--{short_sha}{request_suffix}.md"
    )
    return ROOT / "reports" / date / filename


def command_doctor(config: dict[str, Any], state_path: Path) -> None:
    checks: dict[str, Any] = {
        "gh": shutil.which("gh"),
        "git": shutil.which("git"),
        "codex": shutil.which("codex"),
    }
    if not all(checks.values()):
        missing = ", ".join(name for name, path in checks.items() if not path)
        raise QueueError(f"Missing required commands: {missing}")
    checks["github_login"] = github_login()
    checks["github_auth"] = run(["gh", "auth", "status"])
    checks["codex_version"] = run(["codex", "--version"])
    checks["repositories"] = config.get("repositories", []) or "all accessible repositories"
    checks["codex_project_id"] = config["codex_project_id"]
    checks["project_root"] = str(ROOT)
    connection = connect_state(state_path)
    try:
        checks["state_database"] = str(state_path.resolve())
        checks["schema_version"] = connection.execute(
            "SELECT value FROM metadata WHERE key = 'schema_version'"
        ).fetchone()["value"]
        checks["review_rounds"] = connection.execute(
            "SELECT COUNT(*) AS count FROM review_rounds"
        ).fetchone()["count"]
    finally:
        connection.close()
    print(json.dumps({"status": "ok", **checks}, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("doctor")
    subparsers.add_parser("list")
    dispatch = subparsers.add_parser("dispatch")
    dispatch.add_argument("--limit", type=int)
    claim = subparsers.add_parser("claim")
    claim.add_argument("--prepare", action="store_true")

    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--key", required=True)

    prepare_rereview_parser = subparsers.add_parser("prepare-rereview")
    prepare_rereview_parser.add_argument("--repository", required=True)
    prepare_rereview_parser.add_argument("--number", required=True, type=int)

    prepare_related = subparsers.add_parser("prepare-related")
    prepare_related.add_argument("--pr-url", required=True)

    bind = subparsers.add_parser("bind-task")
    bind.add_argument("--key", required=True)
    bind.add_argument("--thread-id")
    bind.add_argument("--host-id")
    bind.add_argument("--client-thread-id")

    heartbeat = subparsers.add_parser("heartbeat")
    heartbeat.add_argument("--key", required=True)

    complete = subparsers.add_parser("complete")
    complete.add_argument("--key", required=True)
    complete.add_argument("--report", required=True, type=Path)
    complete.add_argument("--findings", type=Path)

    decide = subparsers.add_parser("decide")
    decide.add_argument("--key", required=True)
    decide.add_argument("--accept", nargs="*", default=[])
    decide.add_argument("--reject", nargs="*", default=[])
    decide.add_argument("--note")

    add_user = subparsers.add_parser("add-user-finding")
    add_user.add_argument("--key", required=True)
    add_user.add_argument("--finding", required=True, type=Path)
    add_user.add_argument("--accept", action="store_true")

    preview = subparsers.add_parser("preview-review")
    preview.add_argument("--key", required=True)
    preview.add_argument("--findings", nargs="*")

    draft_review_parser = subparsers.add_parser("draft-review")
    draft_review_parser.add_argument("--key", required=True)
    draft_review_parser.add_argument("--findings", nargs="*")
    draft_review_parser.add_argument("--confirm", required=True)

    edit_draft = subparsers.add_parser("edit-draft-review")
    edit_draft.add_argument("--key", required=True)
    edit_draft.add_argument("--finding", required=True)
    edit_draft.add_argument("--edit", required=True, type=Path)
    edit_draft.add_argument("--confirm", required=True)

    request_changes_parser = subparsers.add_parser("request-changes")
    request_changes_parser.add_argument("--key", required=True)
    request_changes_parser.add_argument("--confirm", required=True)

    history = subparsers.add_parser("history")
    history.add_argument("--repository", required=True)
    history.add_argument("--number", required=True, type=int)

    subparsers.add_parser("migrate-state")

    fail = subparsers.add_parser("fail")
    fail.add_argument("--key", required=True)
    fail.add_argument("--reason", required=True)

    reset = subparsers.add_parser("reset")
    reset.add_argument("--key", required=True)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        if args.command == "doctor":
            command_doctor(config, args.state)
        elif args.command == "list":
            print(json.dumps({"status": "ok", "candidates": candidates(config)}, indent=2))
        elif args.command == "dispatch":
            print(
                json.dumps(
                    reserve_dispatch_batch(config, args.state, args.limit), indent=2
                )
            )
        elif args.command == "claim":
            candidate = claim_candidate(config, args.state)
            if not candidate:
                print(json.dumps({"status": "empty"}, indent=2))
                return 0
            if args.prepare:
                candidate = prepare_claimed_candidate(
                    config, args.state, candidate["key"]
                )
            print(json.dumps({"status": "claimed", "candidate": candidate}, indent=2))
        elif args.command == "prepare":
            candidate = prepare_claimed_candidate(
                config, args.state, args.key
            )
            print(json.dumps({"status": "prepared", "candidate": candidate}, indent=2))
        elif args.command == "prepare-rereview":
            candidate = prepare_rereview(
                config, args.state, args.repository, args.number
            )
            print(
                json.dumps(
                    {"status": "prepared-rereview", "candidate": candidate},
                    indent=2,
                )
            )
        elif args.command == "prepare-related":
            candidate = prepare_related_pr(args.pr_url, config)
            print(
                json.dumps(
                    {"status": "prepared-related", "candidate": candidate}, indent=2
                )
            )
        elif args.command == "bind-task":
            pull_request = bind_task(
                args.state,
                args.key,
                thread_id=args.thread_id,
                host_id=args.host_id,
                client_thread_id=args.client_thread_id,
            )
            print(json.dumps({"status": "task-bound", "pull_request": pull_request}, indent=2))
        elif args.command == "heartbeat":
            result = heartbeat_review(args.state, args.key)
            print(json.dumps({"status": "refreshed", **result}, indent=2))
        elif args.command == "complete":
            report = args.report.resolve()
            findings = args.findings.resolve() if args.findings else None
            entry = complete_review(
                args.state,
                args.key,
                report,
                findings,
            )
            print(json.dumps({"status": "completed", "entry": entry}, indent=2))
        elif args.command == "decide":
            result = decide_findings(
                args.state,
                args.key,
                accept=args.accept,
                reject=args.reject,
                note=args.note,
            )
            print(json.dumps({"status": "decided", **result}, indent=2))
        elif args.command == "add-user-finding":
            result = add_user_finding(
                args.state,
                args.key,
                args.finding.resolve(),
                accept=args.accept,
            )
            print(json.dumps({"status": "user-finding-added", **result}, indent=2))
        elif args.command == "preview-review":
            result = preview_review(args.state, args.key, args.findings)
            print(json.dumps({"status": "preview", **result}, indent=2))
        elif args.command == "draft-review":
            result = draft_review(
                args.state, args.key, args.findings, args.confirm
            )
            print(json.dumps({"status": "drafted", **result}, indent=2))
        elif args.command == "edit-draft-review":
            result = edit_draft_review(
                args.state,
                args.key,
                args.finding,
                args.edit.resolve(),
                args.confirm,
            )
            print(json.dumps({"status": "draft-edited", **result}, indent=2))
        elif args.command == "request-changes":
            result = request_changes(args.state, args.key, args.confirm)
            print(json.dumps({"status": "submitted", **result}, indent=2))
        elif args.command == "history":
            result = review_history(args.state, args.repository, args.number)
            print(json.dumps({"status": "ok", **result}, indent=2))
        elif args.command == "migrate-state":
            connection = connect_state(args.state)
            try:
                rounds = connection.execute(
                    "SELECT COUNT(*) AS count FROM review_rounds"
                ).fetchone()["count"]
            finally:
                connection.close()
            print(
                json.dumps(
                    {
                        "status": "migrated",
                        "database": str(args.state.resolve()),
                        "review_rounds": rounds,
                    },
                    indent=2,
                )
            )
        elif args.command == "fail":
            entry = update_entry(
                args.state,
                args.key,
                "failed",
                failed_at=isoformat(),
                reason=args.reason,
            )
            print(json.dumps({"status": "failed", "entry": entry}, indent=2))
        elif args.command == "reset":
            reset_entry(args.state, args.key)
            print(json.dumps({"status": "reset", "key": args.key}, indent=2))
        return 0
    except QueueError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
