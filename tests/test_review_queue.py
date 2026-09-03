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


class QueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.state_path = Path(self.temporary.name) / "state.json"
        self.config = {
            "claim_ttl_minutes": 180,
            "codex_project_id": "project-test-id",
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

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
    def test_expired_claim_can_be_reclaimed(self, candidates_mock) -> None:
        item = candidate()
        candidates_mock.return_value = [item]
        review_queue.claim_candidate(self.config, self.state_path)
        state = review_queue.read_state(self.state_path)
        state["entries"][item["key"]]["claimed_at"] = review_queue.isoformat(
            review_queue.now() - timedelta(minutes=181)
        )
        review_queue.write_state(self.state_path, state)
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

    def test_state_is_valid_json_after_atomic_write(self) -> None:
        state = {"version": 1, "entries": {"one": {"status": "completed"}}}
        review_queue.write_state(self.state_path, state)
        self.assertEqual(json.loads(self.state_path.read_text()), state)

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
        state = review_queue.read_state(self.state_path)
        self.assertEqual(
            {entry["status"] for entry in state["entries"].values()}, {"claimed"}
        )
        self.assertTrue(
            all("candidate" in entry for entry in state["entries"].values())
        )

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

    def test_suggested_task_title_uses_issue_repo_pr_and_local_time(self) -> None:
        item = candidate(number=6675)
        item["repository"] = "acme/widgets"
        item["linear_issue_ids"] = ["PC-10042"]
        moment = datetime(2026, 9, 3, 17, 5, tzinfo=timezone.utc)

        self.assertEqual(
            review_queue.suggested_task_title(item, moment),
            "PC-10042 · widgets#6675 · Sep 03 17:05",
        )

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
        self.assertEqual(
            review_queue.read_state(self.state_path)["entries"][item["key"]]["status"],
            "reviewing",
        )

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
