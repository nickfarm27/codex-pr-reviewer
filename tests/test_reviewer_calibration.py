from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
QUEUE_SCRIPT = ROOT / "bin" / "review_queue.py"
QUEUE_SPEC = importlib.util.spec_from_file_location("review_queue", QUEUE_SCRIPT)
review_queue = importlib.util.module_from_spec(QUEUE_SPEC)
assert QUEUE_SPEC.loader
QUEUE_SPEC.loader.exec_module(review_queue)
sys.modules["review_queue"] = review_queue

SCRIPT = ROOT / "bin" / "reviewer_calibration.py"
SPEC = importlib.util.spec_from_file_location("reviewer_calibration", SCRIPT)
calibration = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(calibration)


class CalibrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state = self.root / "reviews.db"
        self.config = self.root / "config.json"
        self.config.write_text(
            json.dumps(
                {
                    "codex_project_id": "project-test",
                    "repositories": [],
                    "local_repositories": {},
                }
            )
        )
        self.moment = datetime(2026, 9, 10, 14, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def prepare(self, **kwargs):
        return calibration.prepare_run(
            self.state, self.config, moment=self.moment, **kwargs
        )

    def artifact(
        self,
        *,
        high_impact: bool = False,
        proposal_count: int = 1,
        proposal_fingerprint: str | None = None,
    ) -> dict:
        thread_count = 1 if high_impact else 2
        threads = [
            {
                "thread_id": f"thread-{index}",
                "title": f"Review task {index}",
                "source": "project",
                "project_id": "project-test",
                "last_seen_at": f"2026-09-10T1{index}:00:00Z",
                "latest_turn_id": f"turn-{index}",
                "latest_turn_at": f"2026-09-10T1{index}:00:00Z",
            }
            for index in range(1, thread_count + 1)
        ]
        signals = [
            {
                "id": f"S-{index:02d}",
                "thread_id": f"thread-{index}",
                "turn_id": f"turn-{index}",
                "observed_at": f"2026-09-10T1{index}:00:00Z",
                "category": "clarity",
                "impact": "high" if high_impact else "medium",
                "summary": f"The explanation needed clarification {index}.",
            }
            for index in range(1, thread_count + 1)
        ]
        proposals = []
        for index in range(1, proposal_count + 1):
            item = {
                "title": f"Make explanations clearer {index}",
                "target_layer": "prompt",
                "summary": "The same clarification was needed in review tasks.",
                "proposed_change": "Add a small illustrative solution shape.",
                "expected_benefit": "Fewer clarification turns.",
                "downside": "Examples may look prescriptive.",
                "regression_scenario": "Alternative valid designs remain acceptable.",
                "signal_ids": [signal["id"] for signal in signals],
            }
            if proposal_fingerprint:
                item["fingerprint"] = proposal_fingerprint
            proposals.append(item)
        return {"threads": threads, "signals": signals, "proposals": proposals}

    def complete(self, run_id: int, document: dict, suffix: str = ""):
        report = self.root / f"report{suffix}.md"
        artifact = self.root / f"artifact{suffix}.json"
        report.write_text("# Calibration\n")
        artifact.write_text(json.dumps(document))
        return calibration.complete_run(self.state, run_id, report, artifact)

    def test_schema_two_migrates_without_losing_review_history(self) -> None:
        connection = review_queue.connect_state(self.state)
        connection.execute(
            """
            INSERT INTO pull_requests(
                repository, number, url, created_at, updated_at, last_seen_at
            ) VALUES ('acme/widgets', 12, 'https://example.test/pr/12', 'then', 'then', 'then')
            """
        )
        connection.execute(
            "UPDATE metadata SET value = '2' WHERE key = 'schema_version'"
        )
        connection.close()

        migrated = review_queue.connect_state(self.state)
        version = migrated.execute(
            "SELECT value FROM metadata WHERE key = 'schema_version'"
        ).fetchone()["value"]
        review_count = migrated.execute(
            "SELECT COUNT(*) AS count FROM pull_requests"
        ).fetchone()["count"]
        calibration_table = migrated.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'calibration_runs'"
        ).fetchone()
        migrated.close()

        self.assertEqual(version, "3")
        self.assertEqual(review_count, 1)
        self.assertIsNotNone(calibration_table)

    def test_prepare_resumes_the_only_active_run(self) -> None:
        first = self.prepare()
        second = self.prepare()

        self.assertEqual(first["run"]["id"], second["run"]["id"])
        self.assertEqual(second["status"], "resumed")

    def test_failed_retry_does_not_advance_checkpoints(self) -> None:
        first = self.prepare()
        self.complete(first["run"]["id"], self.artifact(), "-first")
        second = self.prepare()
        calibration.fail_run(self.state, second["run"]["id"], "temporary failure")
        third = self.prepare()

        remembered = {item["thread_id"]: item for item in third["remembered_tasks"]}
        self.assertEqual(remembered["thread-1"]["last_audited_turn_id"], "turn-1")
        self.assertEqual(third["run"]["window_start"], second["run"]["window_start"])

    def test_completion_is_atomic_when_proposal_evidence_is_too_weak(self) -> None:
        prepared = self.prepare()
        document = self.artifact(high_impact=False)
        document["threads"] = document["threads"][:1]
        document["signals"] = document["signals"][:1]
        document["proposals"][0]["signal_ids"] = ["S-01"]

        with self.assertRaisesRegex(calibration.CalibrationError, "two tasks"):
            self.complete(prepared["run"]["id"], document)

        connection = review_queue.connect_state(self.state)
        run_status = connection.execute(
            "SELECT status FROM calibration_runs WHERE id = ?",
            (prepared["run"]["id"],),
        ).fetchone()["status"]
        thread_count = connection.execute(
            "SELECT COUNT(*) AS count FROM calibration_threads"
        ).fetchone()["count"]
        connection.close()
        self.assertEqual(run_status, "running")
        self.assertEqual(thread_count, 0)

    def test_high_impact_single_event_can_create_a_proposal(self) -> None:
        prepared = self.prepare()
        result = self.complete(
            prepared["run"]["id"], self.artifact(high_impact=True)
        )

        self.assertEqual(result["created_proposals"], ["C-001"])

    def test_duplicate_proposal_updates_evidence_instead_of_creating_another(self) -> None:
        fingerprint = "same-general-change"
        first = self.prepare()
        self.complete(
            first["run"]["id"],
            self.artifact(proposal_fingerprint=fingerprint),
            "-first",
        )
        self.moment = datetime(2026, 9, 11, 14, 0, tzinfo=timezone.utc)
        second = self.prepare()
        document = self.artifact(proposal_fingerprint=fingerprint)
        for thread in document["threads"]:
            thread["thread_id"] += "-new"
            thread["latest_turn_id"] += "-new"
        for signal in document["signals"]:
            signal["thread_id"] += "-new"
            signal["turn_id"] += "-new"
        result = self.complete(second["run"]["id"], document, "-second")

        history = calibration.proposal_history(self.state)
        self.assertEqual(len(history["proposals"]), 1)
        self.assertEqual(result["updated_proposals"], ["C-001"])
        self.assertEqual(history["proposals"][0]["task_count"], 4)

    def test_rejected_proposal_is_suppressed_on_repeat(self) -> None:
        fingerprint = "rejected-change"
        first = self.prepare()
        self.complete(
            first["run"]["id"],
            self.artifact(proposal_fingerprint=fingerprint),
            "-first",
        )
        calibration.decide_proposal(self.state, "C-001", "rejected", "too broad")
        self.moment = datetime(2026, 9, 11, 14, 0, tzinfo=timezone.utc)
        second = self.prepare()
        document = self.artifact(proposal_fingerprint=fingerprint)
        for thread in document["threads"]:
            thread["thread_id"] += "-new"
        for signal in document["signals"]:
            signal["thread_id"] += "-new"
            signal["turn_id"] += "-new"
        result = self.complete(second["run"]["id"], document, "-second")

        self.assertEqual(result["suppressed_proposals"], ["C-001"])
        self.assertEqual(len(calibration.proposal_history(self.state)["proposals"]), 1)

    def test_proposal_decision_and_implementation_transitions(self) -> None:
        prepared = self.prepare()
        self.complete(prepared["run"]["id"], self.artifact())

        accepted = calibration.decide_proposal(
            self.state, "C-001", "accepted", "use this"
        )
        implemented = calibration.mark_implemented(
            self.state, "C-001", "abc123"
        )

        self.assertEqual(accepted["status"], "accepted")
        self.assertEqual(implemented["status"], "implemented")
        with self.assertRaisesRegex(calibration.CalibrationError, "Cannot change"):
            calibration.decide_proposal(
                self.state, "C-001", "rejected", "changed mind"
            )

    def test_remembered_task_survives_loss_of_project_association(self) -> None:
        prepared = self.prepare()
        self.complete(prepared["run"]["id"], self.artifact())
        connection = review_queue.connect_state(self.state)
        connection.execute(
            "UPDATE calibration_threads SET project_id = NULL WHERE thread_id = 'thread-1'"
        )
        connection.close()
        self.moment = datetime(2026, 9, 11, 14, 0, tzinfo=timezone.utc)

        next_run = self.prepare()
        remembered = {item["thread_id"] for item in next_run["remembered_tasks"]}

        self.assertIn("thread-1", remembered)

    def test_more_than_three_proposals_is_rejected(self) -> None:
        prepared = self.prepare()
        with self.assertRaisesRegex(calibration.CalibrationError, "at most 3"):
            self.complete(
                prepared["run"]["id"], self.artifact(proposal_count=4)
            )


if __name__ == "__main__":
    unittest.main()
