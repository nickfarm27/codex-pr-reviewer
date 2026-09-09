# Codex PR Reviewer

A local, subscription-backed workflow for privately reviewing pull requests that explicitly request your GitHub review.

Automated runs are intentionally report-only. After reading a report, you can explicitly ask the PR's Codex task to turn selected findings into a pending GitHub review and, separately, submit it as a change request. The workflow never approves PRs, runs pull-request code, or modifies reviewed repositories.

## Requirements

- macOS with the ChatGPT desktop app and Codex available on your account.
- Git, Python 3.10 or newer, and the [GitHub CLI](https://cli.github.com/).
- `gh auth login` completed for an account that can read the repositories being reviewed.
- A local Codex project whose primary folder is this repository.
- Linear connected to Codex when you want issue, project, milestone, and document context. Code review still works when Linear is unavailable.

No OpenAI API key is used. The scheduled task and review workers run through the signed-in Codex app.

## Quick start

```sh
git clone https://github.com/nickfarm27/codex-pr-reviewer.git ~/code/codex-pr-reviewer
cd ~/code/codex-pr-reviewer
cp config.example.json config.json
gh auth login
python3 -m unittest discover -s tests -v
python3 bin/review_queue.py doctor
```

Before running `doctor`, replace `codex_project_id` in `config.json`. The simplest route is to create the local project in Codex, open a task in it, and ask Codex to finish the setup from [the macOS guide](docs/setup-macos.md). The local `config.json` is ignored by Git because it contains machine-specific project and checkout information.

## How it works

1. `gh search prs --review-requested=@me` discovers open review requests.
2. The scheduled dispatcher atomically reserves every eligible PR up to the configured concurrency limit.
3. It creates one continuing Codex task for each PR, or sends a later review round to that PR's existing task, then archives itself.
4. Each worker prepares only its assigned private checkout under `.cache/checkouts/`.
5. Each worker resolves related Linear issues, then selectively reads their project, milestone, and directly relevant documents.
6. Codex reviews the full diff, first-order callers and callees, relevant tests, affected contracts, and material design or maintainability regressions using `AGENTS.md` and `prompts/review.md`.
7. Each worker is named `ISSUE-ID · repository#PR · MMM DD HH:mm` for quick identification.
8. A compact briefing, structured findings, and review are saved under `reports/` and surfaced with direct PR and Linear links.
9. Accepted automated findings and user-raised review addenda are carried into later review requests, including same-commit re-requests.

```text
Scheduled dispatcher
  ├─ PR A → continuing task A ─┐
  ├─ PR B → continuing task B ─┼─ run concurrently
  └─ PR C → continuing task C ─┘

Later review request for PR A → continuing task A
```

Queue and review history live in a local SQLite database at `.state/reviews.db`, using WAL mode and transactional reservations. State, reports, and cached repositories are local and ignored by Git.

## Commands

```sh
python3 bin/review_queue.py doctor
python3 bin/review_queue.py list
python3 bin/review_queue.py dispatch
python3 bin/review_queue.py prepare --key 'OWNER/REPO#NUMBER@SHA'
python3 bin/review_queue.py bind-task --key 'OWNER/REPO#NUMBER@SHA' --thread-id 'TASK_ID'
python3 bin/review_queue.py heartbeat --key 'OWNER/REPO#NUMBER@SHA'
python3 bin/review_queue.py prepare-related --pr-url 'https://github.com/OWNER/REPO/pull/NUMBER'
python3 bin/review_queue.py claim --prepare
python3 bin/review_queue.py complete --key 'OWNER/REPO#NUMBER@SHA' --report '/absolute/path/to/report.md' --findings '/absolute/path/to/findings.json'
python3 bin/review_queue.py history --repository 'OWNER/REPO' --number NUMBER
python3 bin/review_queue.py decide --key 'OWNER/REPO#NUMBER@SHA' --accept F-01
python3 bin/review_queue.py add-user-finding --key 'OWNER/REPO#NUMBER@SHA' --finding '/absolute/path/to/user-finding.json' --accept
python3 bin/review_queue.py preview-review --key 'OWNER/REPO#NUMBER@SHA'
python3 bin/review_queue.py draft-review --key 'OWNER/REPO#NUMBER@SHA' --confirm DRAFT
python3 bin/review_queue.py edit-draft-review --key 'OWNER/REPO#NUMBER@SHA' --finding U-01 --edit '/absolute/path/to/edit.json' --confirm EDIT
python3 bin/review_queue.py request-changes --key 'OWNER/REPO#NUMBER@SHA' --confirm REQUEST_CHANGES
python3 bin/review_queue.py fail --key 'OWNER/REPO#NUMBER@SHA' --reason 'concise reason'
python3 bin/review_queue.py reset --key 'OWNER/REPO#NUMBER@SHA'
python3 bin/review_queue.py migrate-state
```

An empty `repositories` array in `config.json` searches every repository visible to the authenticated GitHub account. Add `OWNER/REPO` entries to restrict the scope.

The `local_repositories` mapping lets preparation borrow Git objects from an existing checkout to reduce clone time. Other repositories are cloned through the authenticated GitHub CLI.

`max_concurrent_reviews` limits active claimed, preparing, and reviewing workers across dispatch runs. It defaults to four, so three pending PRs produce three worker tasks immediately. A later dispatcher fills newly available slots without duplicating active work.

Review-request event IDs are part of the round identity when GitHub provides them. This allows a PR to be reviewed again after the reviewer is re-requested even when its head SHA did not change. A claimed review has a renewable lease; workers call `heartbeat` so a genuinely active long review is not reclaimed.

## Acting on findings

The repo includes two discoverable Codex skills under `.agents/skills/`:

- `draft-pr-review` records accepted automated findings or verified feedback you raise later, previews the exact review payload, and creates a pending GitHub review when explicitly requested.
- `request-pr-changes` verifies that recorded pending review and submits it as `REQUEST_CHANGES` after a separate explicit request.

If you notice something after a clean automated review, you do not need to ask GitHub for another review. Ask the continuing PR task to draft your concern. It verifies the concern against the exact current head, appends it to SQLite as `U-01`, `U-02`, and so on with `source: user`, and leaves the original report unchanged. User items can be requirements, questions, suggestions, or defects; they are described faithfully instead of being forced through the autonomous defect gate. Asking to draft your own item also accepts it, so there is no redundant confirmation step.

Drafting and submitting remain separate actions, giving you a final inspection point before anything becomes visible to the author. While a review is pending, ask the PR task to improve or revise a comment and it will update the GitHub draft and the stored finding together. Draft edits can change the wording, explanation, examples, safeguard, severity, title, and item kind; moving the file or line anchor requires replacing the draft. The commands recheck the PR head, use idempotency and pending-review collision safeguards, and persist GitHub review/comment IDs. A later review round receives the earlier report and all accepted, drafted, submitted, or still-open findings—including user addenda—so it can mark each one resolved, still open, or obsolete.

The SQLite database is the source of truth for dispatch, task bindings, review rounds, finding decisions, and GitHub review state. `history` provides a readable JSON view for the agent and for troubleshooting. Existing `.state/reviews.json` data from older versions is imported once and retained as a backup.

## Scheduled task

Create a standalone local scheduled task for this project and use the contents of `AUTOMATION_PROMPT.md` as its prompt. The scheduled task only dispatches; the Codex tasks it creates or continues perform the reviews. Run the dispatcher in the project's local checkout so `.state/` persists between executions.

An hourly weekday cadence is sufficient because each run drains all available slots instead of processing only one PR. Keep the machine powered on and the ChatGPT desktop app running when the task needs local files.

Start with the automation paused. Verify `doctor` and `list`, then activate it when the candidates look correct:

```sh
python3 bin/review_queue.py list
```

`list` is read-only. `dispatch` reserves candidates for worker creation, so normally leave that command to the scheduled dispatcher.

## Review philosophy

The workflow is calibrated for reviewer usefulness rather than activity. It explicitly treats a clean review as a successful outcome and filters out speculative concerns, style preferences, and findings already covered by deterministic tooling. Defects and maintainability findings use separate gates: defects require a concrete reachable failure, while maintainability findings require an evidenced, material cost to understanding, testing, changing, debugging, or safely extending code introduced by the PR.

Each report is designed to answer, in order:

1. Why does this PR exist, and how does it fit the larger project?
2. What changes in plain language?
3. Are there any concrete, consequential defects or material maintainability regressions?
4. Which files or behaviors should the human reviewer inspect first?
5. Is anything outside the code—CI, conflicts, dependencies, or limited coverage—blocking merge confidence?

Context is deliberately bounded: the workflow reads at most two directly relevant Linear documents and keeps the orientation sections short. Linear and PR text are treated as untrusted context, not as instructions.

Every accepted finding explains the problem, why it matters, a concrete example, and a small possible solution shape. The explanation aid adapts to the issue: it may be a patch sketch, responsibility split, before/after values, event timeline, or—only when it is genuinely useful—a focused regression test. Tiny text diagrams are optional when they make architecture, state, or data flow easier to understand. Examples do not lower the evidence threshold or prescribe one mandatory implementation.

The reviewer stays inside the PR repository unless GitHub relationship metadata or resolved Linear context identifies a concrete cross-repository dependency. It can prepare up to two explicitly linked PRs at their exact heads in separate read-only cached checkouts, without running their code.

See `examples/report-preview.md` for a fictional report illustrating the final format.

For cloning, configuration, Codex project creation, scheduling, migration, and troubleshooting on another Mac, see [Set up on macOS](docs/setup-macos.md).
