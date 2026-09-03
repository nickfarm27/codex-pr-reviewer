#!/usr/bin/env python3
"""Discover, claim, and track GitHub PRs requesting the current user's review."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config.json"
DEFAULT_STATE = ROOT / ".state" / "reviews.json"
LOCK_PATH = ROOT / ".state" / "queue.lock"
MAX_PR_BODY_CHARS = 12_000
ACTIVE_STATUSES = {"claimed", "dispatched", "preparing", "reviewing"}
LINEAR_ISSUE_PATTERN = re.compile(
    r"(?<![A-Z0-9])([A-Z][A-Z0-9]{1,9}-\d+)(?![A-Z0-9]|\.\d)",
    re.IGNORECASE,
)
GITHUB_PR_URL_PATTERN = re.compile(
    r"^https://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/"
    r"(?P<repo>[A-Za-z0-9_.-]+)/pull/(?P<number>[1-9]\d*)/?$"
)


class QueueError(RuntimeError):
    pass


def now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat(value: datetime | None = None) -> str:
    return (value or now()).isoformat().replace("+00:00", "Z")


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def run(command: list[str], *, cwd: Path | None = None) -> str:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError as exc:
        raise QueueError(f"Required command not found: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() or exc.stdout.strip() or f"exit {exc.returncode}"
        raise QueueError(f"Command failed: {' '.join(command)}\n{detail}") from exc
    return result.stdout.strip()


def run_json(command: list[str], *, cwd: Path | None = None) -> Any:
    output = run(command, cwd=cwd)
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
            "--json=number,title,body,url,isDraft,author,headRefOid,headRefName,baseRefName,reviewRequests,mergeable,statusCheckRollup,labels,changedFiles,additions,deletions",
        ]
    )


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
                "key": f"{repository}#{details['number']}@{sha}",
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


def read_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "entries": {}}
    try:
        state = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise QueueError(f"Invalid state JSON in {path}: {exc}") from exc
    state.setdefault("version", 1)
    state.setdefault("entries", {})
    return state


def write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="reviews-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class StateLock:
    def __enter__(self) -> None:
        LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.handle = LOCK_PATH.open("a+")
        fcntl.flock(self.handle, fcntl.LOCK_EX)

    def __exit__(self, *_args: object) -> None:
        fcntl.flock(self.handle, fcntl.LOCK_UN)
        self.handle.close()


def claim_is_active(entry: dict[str, Any], ttl: timedelta) -> bool:
    if entry.get("status") not in ACTIVE_STATUSES or not entry.get("claimed_at"):
        return False
    return now() - parse_time(entry["claimed_at"]) <= ttl


def candidate_is_available(
    candidate: dict[str, Any], entries: dict[str, Any], ttl: timedelta
) -> bool:
    entry = entries.get(candidate["key"])
    if not entry:
        return True
    if entry.get("status") in {"completed", "failed"}:
        return False
    return not claim_is_active(entry, ttl)


def claimed_entry(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "claimed",
        "claimed_at": isoformat(),
        "repository": candidate["repository"],
        "number": candidate["number"],
        "head_sha": candidate["head_sha"],
        "title": candidate["title"],
        "url": candidate["url"],
        "candidate": candidate,
    }


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


def dispatch_descriptor(candidate: dict[str, Any]) -> dict[str, Any]:
    """Return only the fields the dispatcher needs to create a worker."""
    return {
        "key": candidate["key"],
        "repository": candidate["repository"],
        "number": candidate["number"],
        "url": candidate["url"],
        "linear_issue_ids": candidate.get("linear_issue_ids", []),
        "task_title": suggested_task_title(candidate),
    }


def claim_candidate(config: dict[str, Any], state_path: Path) -> dict[str, Any] | None:
    available = candidates(config)
    ttl = timedelta(minutes=int(config.get("claim_ttl_minutes", 180)))

    with StateLock():
        state = read_state(state_path)
        entries = state["entries"]
        for candidate in available:
            if not candidate_is_available(candidate, entries, ttl):
                continue

            entries[candidate["key"]] = claimed_entry(candidate)
            write_state(state_path, state)
            return candidate
    return None


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

    with StateLock():
        state = read_state(state_path)
        entries = state["entries"]
        active = sum(claim_is_active(entry, ttl) for entry in entries.values())
        slots = max(0, maximum - active)
        reserved: list[dict[str, Any]] = []

        for candidate in available:
            if len(reserved) >= slots:
                break
            if not candidate_is_available(candidate, entries, ttl):
                continue
            entries[candidate["key"]] = claimed_entry(candidate)
            reserved.append(candidate)

        if reserved:
            write_state(state_path, state)

    status = "reserved" if reserved else ("at_capacity" if slots == 0 else "empty")
    return {
        "status": status,
        "active_before": active,
        "max_concurrent_reviews": maximum,
        "codex_project_id": config.get("codex_project_id"),
        "project_root": str(ROOT),
        "reserved": [dispatch_descriptor(candidate) for candidate in reserved],
    }


def claimed_candidate(state_path: Path, key: str) -> dict[str, Any]:
    with StateLock():
        state = read_state(state_path)
        entry = state["entries"].get(key)
        if not entry:
            raise QueueError(f"Unknown claim key: {key}")
        if entry.get("status") not in {"claimed", "dispatched"}:
            raise QueueError(
                f"Claim is not ready for preparation: {key} ({entry.get('status')})"
            )
        candidate = entry.get("candidate")
        if not candidate:
            raise QueueError(
                f"Claim predates dispatcher metadata and must be reset before dispatch: {key}"
            )
        entry.update({"status": "preparing", "updated_at": isoformat()})
        write_state(state_path, state)
        return candidate


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
    update_entry(
        state_path,
        key,
        "reviewing",
        prepared_at=isoformat(),
        checkout_path=str(destination),
    )
    return candidate


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
    with StateLock():
        state = read_state(state_path)
        entry = state["entries"].get(key)
        if not entry:
            raise QueueError(f"Unknown claim key: {key}")
        entry.update({"status": status, "updated_at": isoformat(), **fields})
        write_state(state_path, state)
        return entry


def reset_entry(state_path: Path, key: str) -> None:
    with StateLock():
        state = read_state(state_path)
        if key not in state["entries"]:
            raise QueueError(f"Unknown claim key: {key}")
        del state["entries"][key]
        write_state(state_path, state)


def report_path(candidate: dict[str, Any]) -> Path:
    date = now().date().isoformat()
    repo = candidate["repository"].replace("/", "--")
    short_sha = candidate["head_sha"][:12]
    return ROOT / "reports" / date / f"{repo}--pr-{candidate['number']}--{short_sha}.md"


def command_doctor(config: dict[str, Any]) -> None:
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

    prepare_related = subparsers.add_parser("prepare-related")
    prepare_related.add_argument("--pr-url", required=True)

    complete = subparsers.add_parser("complete")
    complete.add_argument("--key", required=True)
    complete.add_argument("--report", required=True, type=Path)

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
            command_doctor(config)
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
                try:
                    destination = prepare_checkout(candidate, config)
                except Exception as exc:
                    update_entry(args.state, candidate["key"], "failed", reason=str(exc))
                    raise
                candidate["checkout_path"] = str(destination)
                candidate["diff_range"] = f"origin/{candidate['base_ref']}...{candidate['head_sha']}"
                candidate["suggested_report_path"] = str(report_path(candidate))
            print(json.dumps({"status": "claimed", "candidate": candidate}, indent=2))
        elif args.command == "prepare":
            candidate = prepare_claimed_candidate(
                config, args.state, args.key
            )
            print(json.dumps({"status": "prepared", "candidate": candidate}, indent=2))
        elif args.command == "prepare-related":
            candidate = prepare_related_pr(args.pr_url, config)
            print(
                json.dumps(
                    {"status": "prepared-related", "candidate": candidate}, indent=2
                )
            )
        elif args.command == "complete":
            report = args.report.resolve()
            if not report.is_file():
                raise QueueError(f"Report not found: {report}")
            entry = update_entry(
                args.state,
                args.key,
                "completed",
                completed_at=isoformat(),
                report=str(report),
            )
            print(json.dumps({"status": "completed", "entry": entry}, indent=2))
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
