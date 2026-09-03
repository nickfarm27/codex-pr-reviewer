import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "review_queue.py"
SPEC = importlib.util.spec_from_file_location("review_queue", SCRIPT)
review_queue = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(review_queue)


def candidate(sha: str = "a" * 40, number: int = 12) -> dict:
    return {
        "repository": "acme/widgets",
        "number": number,
        "title": "Fix widget",
        "url": f"https://github.com/acme/widgets/pull/{number}",
        "author": "someone-else",
        "base_ref": "main",
        "head_ref": "fix-widget",
        "head_sha": sha,
        "key": f"acme/widgets#{number}@{sha}",
        "updated_at": "2026-09-03T00:00:00Z",
        "mergeable": "MERGEABLE",
        "checks": [],
    }


def finding_document() -> dict:
    return {
        "findings": [
            {
                "id": "F-01",
                "severity": "P2",
                "title": "Keep widget ownership stable",
                "path": "app/models/widget.rb",
                "start_line": 12,
                "end_line": 15,
                "explanation": "Changing the owner breaks later reconciliation.",
                "failure_example": "A retry loads credentials for another owner.",
                "safeguard": "Reject owner changes after registration.",
                "safeguard_kind": "implementation",
                "review_comment": (
                    "The owner remains writable after registration. For example, a "
                    "retry can load another owner's credentials. Could we reject owner "
                    "changes after registration and cover that with a focused test?"
                ),
            }
        ],
        "previous_findings": [],
    }


class QueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.state_path = Path(self.temporary.name) / "state.db"
        self.config = {
            "claim_ttl_minutes": 180,
            "codex_project_id": "project-test-id",
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def complete_with_finding(self, item: dict) -> tuple[Path, Path]:
        report = Path(self.temporary.name) / f"report-{item['number']}.md"
        findings = Path(self.temporary.name) / f"findings-{item['number']}.json"
        report.write_text("# Review\n")
        findings.write_text(json.dumps(finding_document()))
        review_queue.complete_review(
            self.state_path, item["key"], report, findings
        )
        return report, findings

    @patch.object(review_queue, "run_json")
    def test_latest_review_request_event_uses_last_paginated_match(
        self, run_json_mock
    ) -> None:
        run_json_mock.return_value = [
            [
                {
                    "id": 10,
                    "event": "review_requested",
                    "created_at": "2026-09-01T00:00:00Z",
                    "requested_reviewer": {"login": "nick"},
                }
            ],
            [
                {
                    "id": 20,
                    "event": "review_requested",
                    "created_at": "2026-09-02T00:00:00Z",
                    "requested_reviewer": {"login": "nick"},
                }
            ],
        ]

        event = review_queue.latest_review_request_event("acme/widgets", 12, "nick")

        self.assertEqual(event["id"], "20")

    @patch.object(review_queue, "candidates")
    def test_completed_sha_is_not_claimed_twice(self, candidates_mock) -> None:
        item = candidate()
        candidates_mock.return_value = [item]
        first = review_queue.claim_candidate(self.config, self.state_path)
        self.assertEqual(first, item)
        review_queue.update_entry(self.state_path, item["key"], "completed")
        self.assertIsNone(review_queue.claim_candidate(self.config, self.state_path))

    @patch.object(review_queue, "candidates")
    def test_new_sha_for_same_pr_is_a_new_claim(self, candidates_mock) -> None:
        old = candidate("a" * 40)
        new = candidate("b" * 40)
        candidates_mock.return_value = [old]
        review_queue.claim_candidate(self.config, self.state_path)
        review_queue.update_entry(self.state_path, old["key"], "completed")
        candidates_mock.return_value = [new]
        self.assertEqual(review_queue.claim_candidate(self.config, self.state_path), new)

    @patch.object(review_queue, "candidates")
    def test_active_claim_is_not_duplicated(self, candidates_mock) -> None:
        item = candidate()
        candidates_mock.return_value = [item]
        review_queue.claim_candidate(self.config, self.state_path)
        self.assertIsNone(review_queue.claim_candidate(self.config, self.state_path))

    @patch.object(review_queue, "candidates")
    def test_new_head_waits_for_active_round_on_same_pr(self, candidates_mock) -> None:
        first = candidate("a" * 40)
        second = candidate("b" * 40)
        candidates_mock.return_value = [first]
        review_queue.claim_candidate(self.config, self.state_path)
        candidates_mock.return_value = [second]

        self.assertIsNone(review_queue.claim_candidate(self.config, self.state_path))

    @patch.object(review_queue, "candidates")
    def test_expired_claim_can_be_reclaimed(self, candidates_mock) -> None:
        item = candidate()
        candidates_mock.return_value = [item]
        review_queue.claim_candidate(self.config, self.state_path)
        connection = review_queue.connect_state(self.state_path)
        connection.execute(
            "UPDATE review_rounds SET claimed_at = ? WHERE claim_key = ?",
            (
                review_queue.isoformat(
                    review_queue.now() - timedelta(minutes=181)
                ),
                item["key"],
            ),
        )
        connection.close()
        self.assertEqual(review_queue.claim_candidate(self.config, self.state_path), item)

    @patch.object(review_queue, "candidates")
    def test_failed_claim_waits_for_manual_reset(self, candidates_mock) -> None:
        item = candidate()
        candidates_mock.return_value = [item]
        review_queue.claim_candidate(self.config, self.state_path)
        review_queue.update_entry(self.state_path, item["key"], "failed", reason="boom")
        self.assertIsNone(review_queue.claim_candidate(self.config, self.state_path))
        review_queue.reset_entry(self.state_path, item["key"])
        self.assertEqual(review_queue.claim_candidate(self.config, self.state_path), item)

        connection = review_queue.connect_state(self.state_path)
        reset_event = connection.execute(
            "SELECT payload_json FROM events WHERE event_type = 'review_reset'"
        ).fetchone()
        connection.close()
        self.assertEqual(json.loads(reset_event["payload_json"])["claim_key"], item["key"])

    @patch.object(review_queue, "candidates")
    def test_heartbeat_refreshes_active_lease(self, candidates_mock) -> None:
        item = candidate()
        candidates_mock.return_value = [item]
        review_queue.claim_candidate(self.config, self.state_path)

        result = review_queue.heartbeat_review(self.state_path, item["key"])

        self.assertEqual(result["status"], "claimed")
        connection = review_queue.connect_state(self.state_path)
        row = connection.execute(
            "SELECT claimed_at FROM review_rounds WHERE claim_key = ?", (item["key"],)
        ).fetchone()
        connection.close()
        self.assertEqual(row["claimed_at"], result["refreshed_at"])

    def test_state_database_has_expected_schema(self) -> None:
        connection = review_queue.connect_state(self.state_path)
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        connection.close()
        self.assertTrue(
            {"pull_requests", "review_rounds", "findings", "github_reviews"}
            <= tables
        )

    def test_example_project_id_must_be_replaced(self) -> None:
        config_path = Path(self.temporary.name) / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    "repositories": [],
                    "local_repositories": {},
                    "codex_project_id": "REPLACE_WITH_YOUR_CODEX_PROJECT_ID",
                }
            )
        )

        with self.assertRaisesRegex(review_queue.QueueError, "local Codex project ID"):
            review_queue.load_config(config_path)

    @patch.object(review_queue, "candidates")
    def test_dispatch_reserves_every_pending_pr_within_capacity(
        self, candidates_mock
    ) -> None:
        items = [candidate(number=number) for number in (12, 13, 14)]
        candidates_mock.return_value = items
        self.config["max_concurrent_reviews"] = 4

        result = review_queue.reserve_dispatch_batch(self.config, self.state_path)

        self.assertEqual(result["status"], "reserved")
        self.assertEqual(result["codex_project_id"], "project-test-id")
        self.assertEqual(result["project_root"], str(review_queue.ROOT))
        self.assertEqual(
            [item["key"] for item in result["reserved"]],
            [item["key"] for item in items],
        )
        connection = review_queue.connect_state(self.state_path)
        rows = connection.execute(
            "SELECT status, candidate_json FROM review_rounds"
        ).fetchall()
        connection.close()
        self.assertEqual({row["status"] for row in rows}, {"claimed"})
        self.assertTrue(all(json.loads(row["candidate_json"]) for row in rows))

    @patch.object(review_queue, "candidates")
    def test_dispatch_respects_global_active_review_capacity(
        self, candidates_mock
    ) -> None:
        items = [candidate(number=number) for number in (12, 13, 14)]
        candidates_mock.return_value = items
        self.config["max_concurrent_reviews"] = 2
        review_queue.claim_candidate(self.config, self.state_path)

        result = review_queue.reserve_dispatch_batch(self.config, self.state_path)

        self.assertEqual(result["active_before"], 1)
        self.assertEqual(result["reserved"][0]["key"], items[1]["key"])

    @patch.object(review_queue, "candidates")
    def test_dispatch_does_not_reserve_active_pr_twice(self, candidates_mock) -> None:
        item = candidate()
        candidates_mock.return_value = [item]
        first = review_queue.reserve_dispatch_batch(self.config, self.state_path)
        second = review_queue.reserve_dispatch_batch(self.config, self.state_path)

        self.assertEqual(first["reserved"][0]["key"], item["key"])
        self.assertEqual(second["reserved"], [])

    @patch.object(review_queue, "candidates")
    def test_same_head_is_reclaimed_after_a_new_review_request_event(
        self, candidates_mock
    ) -> None:
        first = candidate()
        first["review_request_event_id"] = "100"
        first["review_requested_at"] = "2026-09-03T00:00:00Z"
        first["key"] += "~request-100"
        candidates_mock.return_value = [first]
        review_queue.claim_candidate(self.config, self.state_path)
        review_queue.update_entry(
            self.state_path,
            first["key"],
            "completed",
            completed_at="2026-09-03T01:00:00Z",
        )

        second = dict(first)
        second["review_request_event_id"] = "101"
        second["review_requested_at"] = "2026-09-03T02:00:00Z"
        second["key"] = second["key"].replace("request-100", "request-101")
        candidates_mock.return_value = [second]

        self.assertEqual(
            review_queue.claim_candidate(self.config, self.state_path), second
        )

    @patch.object(review_queue, "candidates")
    def test_new_review_request_id_wins_over_timestamp_precision(
        self, candidates_mock
    ) -> None:
        first = candidate()
        first["review_request_event_id"] = "100"
        first["review_requested_at"] = "2026-09-03T01:00:00Z"
        first["key"] += "~request-100"
        candidates_mock.return_value = [first]
        review_queue.claim_candidate(self.config, self.state_path)
        review_queue.update_entry(
            self.state_path,
            first["key"],
            "completed",
            completed_at="2026-09-03T01:00:00.900000Z",
        )

        second = dict(first)
        second["review_request_event_id"] = "101"
        second["key"] = second["key"].replace("request-100", "request-101")
        candidates_mock.return_value = [second]

        self.assertEqual(
            review_queue.claim_candidate(self.config, self.state_path), second
        )

    @patch.object(review_queue, "candidates")
    def test_dispatch_reuses_bound_task_for_new_round(self, candidates_mock) -> None:
        first = candidate()
        candidates_mock.return_value = [first]
        review_queue.reserve_dispatch_batch(self.config, self.state_path)
        review_queue.bind_task(
            self.state_path,
            first["key"],
            thread_id="thread-123",
            host_id="local",
            client_thread_id=None,
        )
        review_queue.update_entry(
            self.state_path,
            first["key"],
            "completed",
            completed_at="2026-09-03T01:00:00Z",
        )

        second = candidate("b" * 40)
        candidates_mock.return_value = [second]
        result = review_queue.reserve_dispatch_batch(self.config, self.state_path)

        self.assertEqual(result["reserved"][0]["dispatch_action"], "continue_task")
        self.assertEqual(result["reserved"][0]["task_thread_id"], "thread-123")

    def test_suggested_task_title_uses_issue_repo_pr_and_local_time(self) -> None:
        item = candidate(number=6675)
        item["repository"] = "acme/widgets"
        item["linear_issue_ids"] = ["PC-10042"]
        moment = datetime(2026, 9, 3, 17, 5, tzinfo=timezone.utc)

        self.assertEqual(
            review_queue.suggested_task_title(item, moment),
            "PC-10042 · widgets#6675 · Sep 03 17:05",
        )

    def test_same_head_rereviews_get_distinct_report_paths(self) -> None:
        first = candidate()
        first["review_request_event_id"] = "100"
        second = dict(first)
        second["review_request_event_id"] = "101"

        self.assertNotEqual(
            review_queue.report_path(first), review_queue.report_path(second)
        )
        self.assertIn("request-100", review_queue.report_path(first).name)

    @patch.object(review_queue, "prepare_checkout")
    @patch.object(review_queue, "candidates")
    def test_worker_prepares_only_its_reserved_claim(
        self, candidates_mock, prepare_checkout_mock
    ) -> None:
        item = candidate()
        candidates_mock.return_value = [item]
        review_queue.reserve_dispatch_batch(self.config, self.state_path)
        destination = Path(self.temporary.name) / "checkout"
        prepare_checkout_mock.return_value = destination

        prepared = review_queue.prepare_claimed_candidate(
            self.config, self.state_path, item["key"]
        )

        self.assertEqual(prepared["checkout_path"], str(destination))
        connection = review_queue.connect_state(self.state_path)
        status = connection.execute(
            "SELECT status FROM review_rounds WHERE claim_key = ?", (item["key"],)
        ).fetchone()["status"]
        connection.close()
        self.assertEqual(status, "reviewing")

    def test_worker_rejects_unknown_claim(self) -> None:
        with self.assertRaises(review_queue.QueueError):
            review_queue.prepare_claimed_candidate(
                self.config, self.state_path, "acme/widgets#404@missing"
            )

    def test_dispatch_rejects_zero_limit_before_discovery(self) -> None:
        with patch.object(review_queue, "candidates") as candidates_mock:
            with self.assertRaises(review_queue.QueueError):
                review_queue.reserve_dispatch_batch(
                    self.config, self.state_path, limit=0
                )
            candidates_mock.assert_not_called()

    def test_extract_linear_issue_ids_from_pr_metadata(self) -> None:
        self.assertEqual(
            review_queue.extract_linear_issue_ids(
                "Give a connection its own row (PC-10042)",
                "Depends on https://linear.app/acme/issue/PC-9878/details",
                "feature/pc-10042-connection-row",
            ),
            ["PC-10042", "PC-9878"],
        )

    def test_extract_linear_issue_ids_ignores_non_issue_hyphens(self) -> None:
        self.assertEqual(
            review_queue.extract_linear_issue_ids(
                "oauth-2.1 refresh-token work", "release/2026-09-03"
            ),
            [],
        )

    @patch.object(review_queue, "candidates")
    def test_completed_review_stores_structured_findings(
        self, candidates_mock
    ) -> None:
        item = candidate()
        candidates_mock.return_value = [item]
        review_queue.claim_candidate(self.config, self.state_path)
        self.complete_with_finding(item)

        history = review_queue.review_history(
            self.state_path, item["repository"], item["number"]
        )

        finding = history["review_rounds"][0]["findings"][0]
        self.assertEqual(finding["finding_key"], "F-01")
        self.assertEqual(finding["status"], "proposed")
        self.assertIn("another owner", finding["failure_example"])

    @patch.object(review_queue, "candidates")
    def test_binding_task_after_completion_preserves_round_status(
        self, candidates_mock
    ) -> None:
        item = candidate()
        candidates_mock.return_value = [item]
        review_queue.claim_candidate(self.config, self.state_path)
        self.complete_with_finding(item)

        review_queue.bind_task(
            self.state_path,
            item["key"],
            thread_id="task-123",
            host_id="local",
            client_thread_id=None,
        )

        connection = review_queue.connect_state(self.state_path)
        row = connection.execute(
            "SELECT status FROM review_rounds WHERE claim_key = ?", (item["key"],)
        ).fetchone()
        connection.close()
        self.assertEqual(row["status"], "completed")

    @patch.object(review_queue, "candidates")
    def test_binding_task_does_not_move_reviewing_round_backwards(
        self, candidates_mock
    ) -> None:
        item = candidate()
        candidates_mock.return_value = [item]
        review_queue.claim_candidate(self.config, self.state_path)
        review_queue.update_entry(self.state_path, item["key"], "reviewing")

        review_queue.bind_task(
            self.state_path,
            item["key"],
            thread_id="task-123",
            host_id="local",
            client_thread_id=None,
        )

        connection = review_queue.connect_state(self.state_path)
        row = connection.execute(
            "SELECT status FROM review_rounds WHERE claim_key = ?", (item["key"],)
        ).fetchone()
        connection.close()
        self.assertEqual(row["status"], "reviewing")

    @patch.object(review_queue, "candidates")
    def test_previous_review_context_only_carries_accepted_findings(
        self, candidates_mock
    ) -> None:
        first = candidate()
        candidates_mock.return_value = [first]
        review_queue.claim_candidate(self.config, self.state_path)
        self.complete_with_finding(first)
        review_queue.decide_findings(
            self.state_path,
            first["key"],
            accept=["F-01"],
            reject=[],
            note="Worth enforcing",
        )

        second = candidate("b" * 40)
        candidates_mock.return_value = [second]
        review_queue.claim_candidate(self.config, self.state_path)
        context = review_queue.previous_review_context(
            self.state_path, second["key"]
        )

        self.assertEqual(context["head_sha"], first["head_sha"])
        self.assertEqual(
            context["accepted_findings"][0]["finding_key"], "F-01"
        )

    @patch.object(review_queue, "candidates")
    def test_rereview_requires_a_disposition_for_every_carried_finding(
        self, candidates_mock
    ) -> None:
        first = candidate()
        candidates_mock.return_value = [first]
        review_queue.claim_candidate(self.config, self.state_path)
        self.complete_with_finding(first)
        review_queue.decide_findings(
            self.state_path,
            first["key"],
            accept=["F-01"],
            reject=[],
            note=None,
        )

        second = candidate("b" * 40)
        candidates_mock.return_value = [second]
        review_queue.claim_candidate(self.config, self.state_path)
        report = Path(self.temporary.name) / "rereview.md"
        findings = Path(self.temporary.name) / "rereview.findings.json"
        report.write_text("# Re-review\n")
        findings.write_text(json.dumps({"findings": [], "previous_findings": []}))

        with self.assertRaisesRegex(review_queue.QueueError, "reconcile every"):
            review_queue.complete_review(
                self.state_path, second["key"], report, findings
            )

    def test_finding_line_range_cannot_run_backwards(self) -> None:
        finding = finding_document()["findings"][0]
        finding["start_line"] = 15
        finding["end_line"] = 12

        with self.assertRaisesRegex(review_queue.QueueError, "cannot precede"):
            review_queue.validate_finding(finding)

    @patch.object(review_queue, "candidates")
    def test_preview_review_uses_only_accepted_findings(
        self, candidates_mock
    ) -> None:
        item = candidate()
        candidates_mock.return_value = [item]
        review_queue.claim_candidate(self.config, self.state_path)
        self.complete_with_finding(item)
        review_queue.decide_findings(
            self.state_path,
            item["key"],
            accept=["F-01"],
            reject=[],
            note=None,
        )

        preview = review_queue.preview_review(self.state_path, item["key"], None)

        self.assertEqual(preview["finding_ids"], ["F-01"])
        self.assertEqual(preview["review"]["commit_id"], item["head_sha"])
        self.assertEqual(preview["review"]["comments"][0]["line"], 15)

    @patch.object(review_queue, "candidates")
    def test_preview_rejects_unaccepted_explicit_finding(
        self, candidates_mock
    ) -> None:
        item = candidate()
        candidates_mock.return_value = [item]
        review_queue.claim_candidate(self.config, self.state_path)
        self.complete_with_finding(item)

        with self.assertRaisesRegex(review_queue.QueueError, "explicitly accepted"):
            review_queue.preview_review(self.state_path, item["key"], ["F-01"])

    @patch.object(review_queue, "run_json")
    @patch.object(review_queue, "remote_pending_reviews", return_value=[])
    @patch.object(review_queue, "current_pr_head")
    @patch.object(review_queue, "candidates")
    def test_draft_and_submit_review_are_recorded(
        self,
        candidates_mock,
        current_head_mock,
        _pending_mock,
        run_json_mock,
    ) -> None:
        item = candidate()
        candidates_mock.return_value = [item]
        review_queue.claim_candidate(self.config, self.state_path)
        self.complete_with_finding(item)
        review_queue.decide_findings(
            self.state_path,
            item["key"],
            accept=["F-01"],
            reject=[],
            note=None,
        )
        current_head_mock.return_value = item["head_sha"]
        run_json_mock.side_effect = [
            {
                "id": 700,
                "state": "PENDING",
                "html_url": "https://github.com/acme/widgets/pull/12#review-700",
            },
            {"id": 700, "state": "PENDING"},
            {
                "id": 700,
                "state": "CHANGES_REQUESTED",
                "html_url": "https://github.com/acme/widgets/pull/12#review-700",
                "submitted_at": "2026-09-03T04:00:00Z",
                "body": "Please address this.",
            },
            [
                {
                    "id": 701,
                    "path": "app/models/widget.rb",
                    "body": finding_document()["findings"][0]["review_comment"],
                }
            ],
        ]

        drafted = review_queue.draft_review(
            self.state_path, item["key"], None, "DRAFT"
        )
        submitted = review_queue.request_changes(
            self.state_path, item["key"], "REQUEST_CHANGES"
        )

        self.assertEqual(drafted["state"], "PENDING")
        self.assertEqual(submitted["state"], "CHANGES_REQUESTED")
        history = review_queue.review_history(
            self.state_path, item["repository"], item["number"]
        )
        self.assertEqual(
            history["review_rounds"][0]["findings"][0]["status"], "submitted"
        )

    @patch.object(review_queue, "run_json")
    @patch.object(review_queue, "remote_pending_reviews", return_value=[])
    @patch.object(review_queue, "current_pr_head")
    @patch.object(review_queue, "candidates")
    def test_idempotent_draft_rechecks_remote_pending_state(
        self,
        candidates_mock,
        current_head_mock,
        _pending_mock,
        run_json_mock,
    ) -> None:
        item = candidate()
        candidates_mock.return_value = [item]
        review_queue.claim_candidate(self.config, self.state_path)
        self.complete_with_finding(item)
        review_queue.decide_findings(
            self.state_path,
            item["key"],
            accept=["F-01"],
            reject=[],
            note=None,
        )
        current_head_mock.return_value = item["head_sha"]
        run_json_mock.side_effect = [
            {
                "id": 700,
                "state": "PENDING",
                "html_url": "https://github.com/acme/widgets/pull/12#review-700",
            },
            {"id": 700, "state": "PENDING"},
        ]

        review_queue.draft_review(self.state_path, item["key"], None, "DRAFT")
        repeated = review_queue.draft_review(
            self.state_path, item["key"], None, "DRAFT"
        )

        self.assertTrue(repeated["idempotent"])
        self.assertEqual(run_json_mock.call_count, 2)

    @patch.object(review_queue, "current_pr_head", return_value="c" * 40)
    @patch.object(review_queue, "candidates")
    def test_draft_aborts_when_pr_head_changed(
        self, candidates_mock, _current_head_mock
    ) -> None:
        item = candidate()
        candidates_mock.return_value = [item]
        review_queue.claim_candidate(self.config, self.state_path)
        self.complete_with_finding(item)
        review_queue.decide_findings(
            self.state_path,
            item["key"],
            accept=["F-01"],
            reject=[],
            note=None,
        )

        with self.assertRaisesRegex(review_queue.QueueError, "head changed"):
            review_queue.draft_review(
                self.state_path, item["key"], None, "DRAFT"
            )

    @patch.object(review_queue, "prepare_checkout")
    @patch.object(review_queue, "pr_details")
    def test_prepare_related_pr_uses_exact_linked_head(
        self, pr_details_mock, prepare_checkout_mock
    ) -> None:
        url = "https://github.com/acme/api/pull/81"
        pr_details_mock.return_value = {
            "number": 81,
            "title": "Add widget contract",
            "url": url,
            "baseRefName": "main",
            "headRefName": "widget-contract",
            "headRefOid": "b" * 40,
        }
        destination = Path(self.temporary.name) / "related-checkout"
        prepare_checkout_mock.return_value = destination

        prepared = review_queue.prepare_related_pr(url, self.config)

        self.assertEqual(prepared["repository"], "acme/api")
        self.assertEqual(prepared["head_sha"], "b" * 40)
        self.assertEqual(prepared["checkout_path"], str(destination))
        self.assertEqual(prepared["diff_range"], f"origin/main...{'b' * 40}")
        prepare_checkout_mock.assert_called_once()

    @patch.object(review_queue, "pr_details")
    def test_prepare_related_pr_rejects_noncanonical_url(
        self, pr_details_mock
    ) -> None:
        with self.assertRaises(review_queue.QueueError):
            review_queue.prepare_related_pr(
                "https://example.com/acme/api/pull/81", self.config
            )
        pr_details_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
