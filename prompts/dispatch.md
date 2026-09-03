# Requested-review dispatcher

Run one lightweight dispatch cycle. Do not review pull-request code in this task.

## 1. Reserve available work

Run:

```sh
python3 bin/review_queue.py dispatch
```

The command atomically reserves pending PRs up to `max_concurrent_reviews`. Every reservation includes a `dispatch_action`:

- `continue_task`: send the new review round to the PR's existing Codex task.
- `create_task`: create the PR's first Codex task and bind it to that PR.

## 2. Dispatch every reservation promptly

Use this worker prompt, substituting the exact project root and claim key:

```text
Continue the private review of the pull request assigned to this task.

Work only in PROJECT_ROOT. Read and follow PROJECT_ROOT/AGENTS.md and PROJECT_ROOT/prompts/review.md.

Your exact new review-round claim key is: CLAIM_KEY

Run `python3 bin/review_queue.py prepare --key 'CLAIM_KEY'`, then complete the review workflow for only that candidate. Use the returned `previous_review` to reconcile earlier accepted findings and explain what changed since the prior review. Never run `claim` or `dispatch`, never review another PR, and never create another Codex task.

The automated review itself is private and report-only. Never post to GitHub unless the user later invokes one of this project's explicit review-action skills in this same task. Never modify a reviewed repository, invoke a nested Codex CLI process, or use an API key. A clean review with no findings is valid; do not invent feedback.
```

For `continue_task`, use the app's `send_message_to_thread` action with the exact `task_thread_id`, optional `task_host_id`, and worker prompt. Do not create a replacement merely to get a newer timestamp in the title. After the message is accepted, run:

```sh
python3 bin/review_queue.py bind-task --key 'CLAIM_KEY' --thread-id 'TASK_THREAD_ID' [--host-id 'TASK_HOST_ID']
```

For `create_task`, use the app's `create_thread` action with:

- The exact `codex_project_id` returned by dispatch.
- Local environment, because this saved project is not itself a Git repository.
- No model or reasoning override; use the user's configured default.
- The exact candidate `task_title`.
- The worker prompt above.

Bind the accepted task immediately:

```sh
python3 bin/review_queue.py bind-task --key 'CLAIM_KEY' --thread-id 'TASK_THREAD_ID' [--host-id 'TASK_HOST_ID']
```

If task setup returns only a queued client task ID, bind it with `--client-thread-id` and keep this dispatcher visible unless a ready task ID can be resolved and bound. A ready task ID is required before future rounds can continue the task.

If continuing an existing task fails because that task no longer exists or cannot be reached, create one replacement task using the same title and worker prompt, then bind the replacement. If both continuation and replacement fail, or initial task creation fails, immediately release only that reservation:

```sh
python3 bin/review_queue.py reset --key 'CLAIM_KEY'
```

Continue dispatching the other reservations. Never leave an undelivered reservation silently claimed.

## 3. Finish the dispatcher

- If every reservation was delivered and bound, report the number and titles dispatched, archive this dispatcher task with `set_thread_archived` using `archived: true` and no `threadId`, and stop. Do not wait for workers.
- If the result was `empty` or `at_capacity`, report that concisely, archive this dispatcher task the same way, and stop.
- If discovery, delivery, binding, or replacement failed, do not archive this task. Leave a concise actionable failure report.

The dispatcher never writes to GitHub and never prepares, reads, or reviews a pull-request checkout.
