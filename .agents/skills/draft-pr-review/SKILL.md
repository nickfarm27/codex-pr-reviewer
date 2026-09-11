---
name: draft-pr-review
description: Create or revise a pending GitHub PR review from accepted automated findings or verified user-raised feedback when the user asks to draft or edit review comments.
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
     "safeguard": "Treat the supported-country list as capabilities, require at least one ready account, and expose methods only for ready countries.",
     "safeguard_kind": "implementation",
     "review_comment": "This currently requires both countries to be ready. For example, a GB-only account remains blocked when CH is intentionally unconfigured. Could we treat the country list as capabilities instead: allow activation when at least one account is ready and expose methods only for ready countries? Focused GB-only and CH-only tests can verify the chosen implementation."
   }
   ```

   ```sh
   python3 bin/review_queue.py add-user-finding --key 'CLAIM_KEY' --finding '/absolute/path/to/user-finding.json' --accept
   ```

   The user's request to include or draft their own feedback counts as acceptance of that item; do not ask them to accept it again. The command assigns an append-only `U-01`, `U-02`, and so on, records `source: user`, deduplicates retries, and refuses a stale head.

   Make the comment understandable without a follow-up: state the problem, why it matters, one concrete example, and a small possible solution shape. Prefer implementation sketches, responsibility splits, before/after values, or short event timelines. Use a regression test as the main example only when it genuinely explains the behavior better. Tiny text diagrams are optional when they clarify architecture, state, or data flow; omit them when prose is simpler. Treat every proposed solution as illustrative rather than required architecture.

3. Generate and inspect the exact pending-review payload:

   ```sh
   python3 bin/review_queue.py preview-review --key 'CLAIM_KEY'
   ```

   Check the review body and every inline comment for accuracy, useful examples, and the intended line. Automated defects must still pass the full review gate. User-raised items may express a product requirement or question, but must not overstate the evidence.

4. If the user explicitly asks for wording only or a local preview, stop after previewing it. Treat `$draft-pr-review`, “draft this PR review,” or “put this on the PR” as a request to create the pending GitHub review, and run exactly once:

   ```sh
   python3 bin/review_queue.py draft-review --key 'CLAIM_KEY' --confirm DRAFT
   ```

The commands recheck the PR head and prevent duplicate local drafts and findings. After a completed current-head re-review, `draft-review` may replace exactly one unchanged pending review recorded by this workflow on the prior round; it leaves unrecorded, unrelated, or remotely edited pending reviews untouched. Do not bypass those safeguards or use `gh` directly. A pending review is not visible to the PR author until submitted.

## Revise an existing draft

When the current round already has a pending review and the user asks to improve, rewrite, shorten, clarify, or otherwise edit one of its comments, inspect the stored draft in `history` and prepare a JSON object containing only the fields that should change. Preserve the user's intended requirement and make the reasoning easy to follow: explain why it matters, show the practical failure, and include concise examples when they make the behavior clearer.

For example:

```json
{
  "title": "Treat supported countries as optional, not mandatory",
  "explanation": "Supported countries describe platform capability, while each retailer may configure only the countries where it operates.",
  "failure_example": "A GB-only retailer cannot activate because the absent CH account is treated as required.",
  "safeguard": "Cover GB-only, CH-only, one-ready/one-incomplete, and no-ready-account cases.",
  "review_comment": "`SUPPORTED_COUNTRY_CODES` describes what Exporto can support, not what every retailer must configure. Because this requires every country to be ready, a usable GB account is blocked by an absent or incomplete CH account, and adding another supported country later would unexpectedly block existing retailers. Could activation require at least one ready account and enable only methods from ready accounts? Please cover GB-only, CH-only, one-ready/one-incomplete, and no-ready-account cases."
}
```

Apply it with:

```sh
python3 bin/review_queue.py edit-draft-review --key 'CLAIM_KEY' --finding U-01 --edit '/absolute/path/to/edit.json' --confirm EDIT
```

Treat “make the draft better,” “revise the comment,” and similar action requests as authorization to update the existing pending draft. If the user asks only to show or propose revised wording, keep it local. The edit command requires the same reviewed head, a recorded pending review, and an unsubmitted drafted finding; it updates GitHub and SQLite together. It edits wording and finding metadata, not the comment's file or line anchor. If the anchor must move, explain that the pending review must be replaced rather than bypassing the workflow.

Return the selected finding IDs, whether the result is local-only or pending on GitHub, and links to the PR and pending review when available. Never approve or submit the review from this skill.
