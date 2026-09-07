---
name: draft-pr-review
description: Turn accepted automated findings or verified user-raised feedback into a preview or pending GitHub PR review when the user asks to draft review comments.
---

# Draft PR Review

Work in the Codex PR Reviewer project and keep the action tied to the current PR task.

1. Identify the exact repository, PR number, and completed `claim_key` for the current PR head. Inspect local history when needed:

   ```sh
   python3 bin/review_queue.py history --repository 'OWNER/REPO' --number NUMBER
   ```

   Prefer the newest completed round whose `head_sha` is still the PR's current head. Do not require a new GitHub review request merely because the discussion happened after the automated review.

2. Choose the applicable input path:

   - For an existing automated finding (`source: agent`), accept only findings the user explicitly selected or clearly approved. Record accepted or rejected IDs with:

   ```sh
   python3 bin/review_queue.py decide --key 'CLAIM_KEY' --accept F-01 F-02 --note 'Concise user decision'
   ```

   - If the user raises a concern, requirement, question, or suggestion that is not in the completed review, verify its facts against the exact current head and PR diff. Do not force it through the autonomous defect gate or claim it is a PR-introduced defect without evidence. Represent it faithfully as `defect`, `required_change`, `question`, or `suggestion`, with a tight changed-line anchor when one exists. Write a temporary JSON object containing the normal finding fields plus `kind` and a concise `source_note`, then append it without modifying the original report:

   ```json
   {
     "kind": "required_change",
     "source_note": "The user requires activation when either supported country is ready.",
     "severity": "P2",
     "title": "Allow partial-country activation",
     "path": "app/services/integration_activation.rb",
     "start_line": 18,
     "end_line": 18,
     "explanation": "The new all-countries check conflicts with the clarified requirement.",
     "failure_example": "A GB-only account remains blocked because CH is not configured.",
     "safeguard": "Cover GB-only and CH-only activation in focused tests.",
     "safeguard_kind": "regression_test",
     "review_comment": "This currently requires both countries to be ready. For example, a GB-only account remains blocked when CH is intentionally unconfigured. Could we allow activation when either configured country is ready and cover the GB-only and CH-only cases?"
   }
   ```

   ```sh
   python3 bin/review_queue.py add-user-finding --key 'CLAIM_KEY' --finding '/absolute/path/to/user-finding.json' --accept
   ```

   The user's request to include or draft their own feedback counts as acceptance of that item; do not ask them to accept it again. The command assigns an append-only `U-01`, `U-02`, and so on, records `source: user`, deduplicates retries, and refuses a stale head.

3. Generate and inspect the exact pending-review payload:

   ```sh
   python3 bin/review_queue.py preview-review --key 'CLAIM_KEY'
   ```

   Check the review body and every inline comment for accuracy, useful examples, and the intended line. Automated defects must still pass the full review gate. User-raised items may express a product requirement or question, but must not overstate the evidence.

4. If the user explicitly asks for wording only or a local preview, stop after previewing it. Treat `$draft-pr-review`, “draft this PR review,” or “put this on the PR” as a request to create the pending GitHub review, and run exactly once:

   ```sh
   python3 bin/review_queue.py draft-review --key 'CLAIM_KEY' --confirm DRAFT
   ```

The commands recheck the PR head, prevent duplicate local drafts and findings, and refuse to collide with another pending review. Do not bypass those safeguards or use `gh` directly. A pending review is not visible to the PR author until submitted.

Return the selected finding IDs, whether the result is local-only or pending on GitHub, and links to the PR and pending review when available. Never approve or submit the review from this skill.
