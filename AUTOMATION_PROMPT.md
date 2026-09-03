Work only in the scheduled task's configured local project directory. Confirm that it contains `AGENTS.md`, `config.json`, and `prompts/dispatch.md`; this is PROJECT_ROOT. First read and follow `PROJECT_ROOT/AGENTS.md` and `PROJECT_ROOT/prompts/dispatch.md`, then run exactly one requested-review dispatch cycle.

This scheduled task is a dispatcher only. Atomically reserve pending review requests up to the configured global concurrency limit, then use `create_thread` to create one independent local Codex task per reserved PR in the `GitHub auto-review` project. Create all workers promptly without waiting for their results. Each worker must receive its exact claim key and follow `prompts/review.md`; it must never claim a different PR.

If all workers are accepted, or if the queue is empty or already at capacity, archive this dispatcher task with `set_thread_archived` using `archived: true` and no `threadId`. If discovery, reservation, or any worker creation fails, keep the dispatcher visible and report the failure. Release a reservation immediately when its worker could not be created.

Never review pull-request code in the dispatcher. Never post comments, approvals, change requests, commits, or any other writes to GitHub, never modify a reviewed repository, never invoke a nested Codex CLI process, and never use an API key.
