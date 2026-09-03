---
name: request-pr-changes
description: Verify and submit this project's recorded pending GitHub PR review as REQUEST_CHANGES when the user explicitly asks to request changes.
---

# Request PR Changes

Use this only in the continuing Codex task for the PR being reviewed.

1. Resolve the exact repository, PR number, and completed `claim_key` from the current task, then inspect the recorded lifecycle:

   ```sh
   python3 bin/review_queue.py history --repository 'OWNER/REPO' --number NUMBER
   ```

2. Confirm that the chosen round has a recorded `PENDING` GitHub review and that its drafted findings match what the user accepted. If no pending review exists, stop and use the `draft-pr-review` workflow first. Do not create or reconstruct a review with direct `gh` calls.

3. If the current message itself is not an explicit request to submit/request changes on GitHub, show what is pending and ask for that authorization. Do not treat earlier approval of the findings as permission to publish them.

4. When explicitly authorized, submit exactly once:

   ```sh
   python3 bin/review_queue.py request-changes --key 'CLAIM_KEY' --confirm REQUEST_CHANGES
   ```

The command verifies the reviewed head is still current and that GitHub still has the recorded pending review. Do not bypass a stale-head, missing-review, or unexpected-state failure. Never substitute `APPROVE` or a general comment review.

Return the final GitHub review state and direct links to the submitted review and PR. The stored submitted findings become context for later review requests on the same PR.
