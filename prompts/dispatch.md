# Requested-review dispatcher

Run one lightweight dispatch cycle. Do not review pull-request code in this task.

## 1. Reserve available work

Run:

```sh
python3 bin/review_queue.py dispatch
```

The command atomically reserves as many pending PRs as there are free slots under `max_concurrent_reviews`. It returns `reserved`, `empty`, or `at_capacity`, along with the configured `codex_project_id` and resolved `project_root`.

## 2. Create one worker task per reservation

When `reserved` contains candidates, create one Codex task for every candidate immediately. Do not wait for one worker before creating the next.

Use the Codex app's `create_thread` action with:

- The exact `codex_project_id` returned by the dispatch command.
- Local environment, because this saved project is not itself a Git repository.
- No model or reasoning override; use the user's configured default.
- The candidate's exact `task_title`. It is already formatted as `ISSUE-ID · repository#PR · MMM DD HH:mm` using local 24-hour time, with the issue segment omitted when none was extracted.
- The worker prompt below, substituting the exact project root and claim key.

```text
You are a private PR-review worker assigned to exactly one reserved candidate.

Work only in PROJECT_ROOT. Read and follow PROJECT_ROOT/AGENTS.md and PROJECT_ROOT/prompts/review.md.

Your exact claim key is: CLAIM_KEY

Run `python3 bin/review_queue.py prepare --key 'CLAIM_KEY'`, then complete the review workflow for only that candidate. Never run `claim` or `dispatch`, never review a different PR, and never create another Codex task.

This is private and report-only. Never post comments, approvals, change requests, commits, or any other writes to GitHub. Never modify a reviewed repository, invoke a nested Codex CLI process, or use an API key. A clean review with no findings is valid; do not invent feedback.
```

Task creation is successful when the app returns either a ready task ID or a queued client task ID.

If creating a worker fails, immediately release only that reservation:

```sh
python3 bin/review_queue.py reset --key 'CLAIM_KEY'
```

Continue attempting the other reserved candidates, but keep this dispatcher task visible and report every failed dispatch. Never leave a failed-to-create reservation silently claimed.

## 3. Finish the dispatcher

- If every reserved worker was accepted, report the number and titles dispatched, archive this dispatcher task with `set_thread_archived` using `archived: true` and no `threadId`, and stop. Do not wait for workers.
- If the result was `empty` or `at_capacity`, report that concisely, archive this dispatcher task the same way, and stop.
- If discovery, reservation, or any worker creation failed, do not archive this task. Leave a concise actionable failure report.

The dispatcher never writes to GitHub and never prepares, reads, or reviews a pull-request checkout.
