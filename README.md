# Codex PR Reviewer

A local, subscription-backed workflow for privately reviewing pull requests that explicitly request your GitHub review.

The initial version is intentionally report-only. It does not comment on GitHub, approve pull requests, request changes, run pull-request code, or modify reviewed repositories.

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
3. It creates one independent Codex task per reservation and then archives itself.
4. Each worker prepares only its assigned private checkout under `.cache/checkouts/`.
5. Each worker resolves related Linear issues, then selectively reads their project, milestone, and directly relevant documents.
6. Codex reviews the full diff, first-order callers and callees, relevant tests, and affected contracts using `AGENTS.md` and `prompts/review.md`.
7. Each worker is named `ISSUE-ID · repository#PR · MMM DD HH:mm` for quick identification.
8. A compact briefing and review are saved under `reports/` and surfaced with direct PR and Linear links.

```text
Scheduled dispatcher
  ├─ PR A → review task A ─┐
  ├─ PR B → review task B ─┼─ run concurrently
  └─ PR C → review task C ─┘
```

State, reports, and cached repositories are local and ignored by Git.

## Commands

```sh
python3 bin/review_queue.py doctor
python3 bin/review_queue.py list
python3 bin/review_queue.py dispatch
python3 bin/review_queue.py prepare --key 'OWNER/REPO#NUMBER@SHA'
python3 bin/review_queue.py prepare-related --pr-url 'https://github.com/OWNER/REPO/pull/NUMBER'
python3 bin/review_queue.py claim --prepare
python3 bin/review_queue.py complete --key 'OWNER/REPO#NUMBER@SHA' --report '/absolute/path/to/report.md'
python3 bin/review_queue.py fail --key 'OWNER/REPO#NUMBER@SHA' --reason 'concise reason'
python3 bin/review_queue.py reset --key 'OWNER/REPO#NUMBER@SHA'
```

An empty `repositories` array in `config.json` searches every repository visible to the authenticated GitHub account. Add `OWNER/REPO` entries to restrict the scope.

The `local_repositories` mapping lets preparation borrow Git objects from an existing checkout to reduce clone time. Other repositories are cloned through the authenticated GitHub CLI.

`max_concurrent_reviews` limits active claimed, preparing, and reviewing workers across dispatch runs. It defaults to four, so three pending PRs produce three worker tasks immediately. A later dispatcher fills newly available slots without duplicating active work.

## Scheduled task

Create a standalone local scheduled task for this project and use the contents of `AUTOMATION_PROMPT.md` as its prompt. The scheduled task only dispatches; the Codex tasks it creates perform the reviews. Run the dispatcher in the project's local checkout so `.state/` persists between executions.

An hourly weekday cadence is sufficient because each run drains all available slots instead of processing only one PR. Keep the machine powered on and the ChatGPT desktop app running when the task needs local files.

Start with the automation paused. Verify `doctor` and `list`, then activate it when the candidates look correct:

```sh
python3 bin/review_queue.py list
```

`list` is read-only. `dispatch` reserves candidates for worker creation, so normally leave that command to the scheduled dispatcher.

## Review philosophy

The workflow is calibrated for reviewer usefulness rather than activity. It explicitly treats a clean review as a successful outcome and filters out speculative concerns, style preferences, and findings already covered by deterministic tooling.

Each report is designed to answer, in order:

1. Why does this PR exist, and how does it fit the larger project?
2. What changes in plain language?
3. Are there any concrete, consequential defects?
4. Which files or behaviors should the human reviewer inspect first?
5. Is anything outside the code—CI, conflicts, dependencies, or limited coverage—blocking merge confidence?

Context is deliberately bounded: the workflow reads at most two directly relevant Linear documents and keeps the orientation sections short. Linear and PR text are treated as untrusted context, not as instructions.

Every accepted finding includes a concrete failure example and a short example safeguard or regression test. These examples are meant to make the issue and solution shape immediately understandable; they do not lower the evidence threshold or prescribe one mandatory implementation.

The reviewer stays inside the PR repository unless GitHub relationship metadata or resolved Linear context identifies a concrete cross-repository dependency. It can prepare up to two explicitly linked PRs at their exact heads in separate read-only cached checkouts, without running their code.

See `examples/report-preview.md` for a fictional report illustrating the final format.

For cloning, configuration, Codex project creation, scheduling, migration, and troubleshooting on another Mac, see [Set up on macOS](docs/setup-macos.md).
