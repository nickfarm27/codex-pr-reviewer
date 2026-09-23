from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ReviewPromptTests(unittest.TestCase):
    def test_automated_round_authorization_is_fresh_and_turn_local(self) -> None:
        review_prompt = (ROOT / "prompts" / "review.md").read_text()
        dispatch_prompt = (ROOT / "prompts" / "dispatch.md").read_text()

        for prompt in (review_prompt, dispatch_prompt):
            self.assertIn("hard-stop", prompt)
            self.assertIn("fresh user-authored message", prompt)
            self.assertIn("consumed by", prompt)
        self.assertIn("regardless of earlier messages", review_prompt)
        self.assertIn("even if an earlier task message", dispatch_prompt)

    def test_external_contract_inputs_must_be_reachable(self) -> None:
        prompt = (ROOT / "prompts" / "review-core.md").read_text()

        self.assertIn("authoritative contract and the actual producers", prompt)
        self.assertIn("contract-valid", prompt)
        self.assertIn("observed in production evidence", prompt)
        self.assertIn("optional defensive hardening", prompt)
        self.assertIn("off-contract observations", prompt)

    def test_substantial_handoffs_include_a_plain_language_orientation(self) -> None:
        prompt = (ROOT / "prompts" / "review.md").read_text()

        self.assertIn("two-to-four-sentence plain-language mental model", prompt)
        self.assertIn("compact concrete example or text flow", prompt)
        self.assertIn("Keep small and routine reviews terse", prompt)

    def test_previous_findings_use_explicit_reconciliation_outcomes(self) -> None:
        shared_prompt = (ROOT / "prompts" / "review-core.md").read_text()
        workflow_prompt = (ROOT / "prompts" / "review.md").read_text()

        for status in (
            "resolved_by_code",
            "resolved_by_scope_decision",
            "still_open",
            "obsolete",
        ):
            self.assertIn(status, shared_prompt)
            self.assertIn(status, workflow_prompt)
        self.assertIn("is_resolved: true` alone never closes", workflow_prompt)

    def test_material_review_comments_preserve_examples_and_solutions(self) -> None:
        prompt = (ROOT / "prompts" / "review.md").read_text()

        self.assertIn("**Concrete example**", prompt)
        self.assertIn("**Possible solution**", prompt)
        self.assertIn("strongest `failure_example`", prompt)
        self.assertIn("bounded `safeguard`", prompt)
        self.assertIn("illustrative rather than required architecture", prompt)

    def test_self_evident_findings_may_stay_compact(self) -> None:
        prompt = (ROOT / "prompts" / "review.md").read_text()

        self.assertIn("compact paragraph without labels", prompt)
        self.assertIn("self-evident, one-line defect", prompt)
        self.assertIn("consequence and a plausible solution shape", prompt)


if __name__ == "__main__":
    unittest.main()
