from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ReviewPromptTests(unittest.TestCase):
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
