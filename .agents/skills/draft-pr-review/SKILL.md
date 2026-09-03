---
name: draft-pr-review
description: Select accepted findings from a completed Codex PR review, preview the exact comments, and create a pending GitHub review when the user explicitly asks to draft or place review comments on the PR.
---

# Draft PR Review

Work in the Codex PR Reviewer project and keep the action tied to the current PR task.

1. Identify the exact repository, PR number, and completed `claim_key` from the current task. If unclear, inspect local history with:

   ```sh
   python3 bin/review_queue.py history --repository 'OWNER/REPO' --number NUMBER
   ```

2. Show the user the proposed finding IDs and titles. Accept only findings the user explicitly selected or clearly approved; never infer acceptance from the mere existence of a finding. Record the decision:

   ```sh
   python3 bin/review_queue.py decide --key 'CLAIM_KEY' --accept F-01 F-02 --note 'Concise user decision'
   ```

   Record explicitly rejected findings with `--reject`. Do not silently convert suggestions, limitations, CI failures, or review notes into findings.

3. Generate and inspect the exact pending-review payload:

   ```sh
   python3 bin/review_queue.py preview-review --key 'CLAIM_KEY'
   ```

   Summarize the review body and each inline comment for the user. Preserve the review rule: only concrete, reachable, consequential, evidenced, PR-introduced problems belong in the draft. Each comment should explain the failure and include the prepared example safeguard or regression test.

4. If the user asked only to draft wording, stop after the local preview. If the user explicitly asked to create or place the pending review on GitHub, run exactly once:

   ```sh
   python3 bin/review_queue.py draft-review --key 'CLAIM_KEY' --confirm DRAFT
   ```

The command rechecks the PR head, prevents duplicate local drafts, and refuses to collide with another pending review. Do not bypass those safeguards or use `gh` directly. A pending review is not visible to the PR author until submitted.

Return the selected finding IDs, whether the result is local-only or pending on GitHub, and links to the PR and pending review when available. Never approve or submit the review from this skill.
