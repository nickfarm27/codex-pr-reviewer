# Set up Codex PR Reviewer on macOS

This guide installs the workflow on a Mac and creates a private, report-only dispatcher in the Codex desktop app. Optional user-triggered skills can later draft and submit change requests.

## 1. Install the prerequisites

Install the [ChatGPT desktop app](https://chatgpt.com/download/) and sign in to the account whose Codex subscription should run the reviews.

Install GitHub CLI and Python 3.10 or newer. With [Homebrew](https://brew.sh/):

```sh
brew install gh python
gh auth login
gh auth status
```

The GitHub account must be able to read every repository you want reviewed. Connect Linear in Codex as well if reports should include issue, project, milestone, and document context.

## 2. Clone and configure the reviewer

```sh
mkdir -p ~/code
git clone https://github.com/nickfarm27/codex-pr-reviewer.git ~/code/codex-pr-reviewer
cd ~/code/codex-pr-reviewer
cp config.example.json config.json
```

Edit `config.json`:

- `reviewer`: keep `@me` to use the active GitHub CLI account.
- `repositories`: keep `[]` for all accessible repositories, or list values such as `owner/repository` to restrict discovery.
- `exclude_authors`: `@me` prevents self-authored PRs from entering the queue.
- `max_concurrent_reviews`: maximum active review tasks across dispatcher runs.
- `codex_project_id`: the ID of the local Codex project created in the next step.
- `local_repositories`: optional `owner/repository` to absolute checkout-path mappings. These reuse local Git objects for faster preparation; they are not the review working copies.

`config.json`, cached checkouts, the SQLite state database, and generated reports stay local and are ignored by Git.

## 3. Create the local Codex project

In the Codex desktop app, create a local project and make the cloned `codex-pr-reviewer` folder its primary folder. A dedicated project keeps the dispatcher, worker tasks, reports, and repository instructions together.

Open a task in that project and ask:

```text
Set up this Codex PR Reviewer checkout. Put this local project's ID into
config.json, run the test suite and doctor command, then create a paused local
scheduled task using AUTOMATION_PROMPT.md. Do not dispatch any reviews yet.
```

Codex can resolve the project ID and create the scheduled task without placing it in committed files.

## 4. Verify before activation

Run:

```sh
python3 -m unittest discover -s tests -v
python3 bin/review_queue.py doctor
python3 bin/review_queue.py list
```

Expected behavior:

- Tests pass.
- `doctor` reports authenticated GitHub and available Git, Python, and Codex tooling.
- `list` returns only open, non-draft PRs that still request your review and pass the configured repository/author filters.

Do not use `dispatch` as a dry run: it reserves work for task creation. After checking the candidate list, activate the paused scheduled task in Codex.

## 5. What each scheduled run does

The scheduled task is only a dispatcher. It atomically reserves available candidates up to the concurrency limit, creates the first task for each PR or continues that PR's already-bound task, then archives itself. Worker tasks remain visible and each one:

1. Prepares an isolated detached checkout at the reserved PR head.
2. Retrieves bounded Linear context when available.
3. Reviews the full diff, nearby behavior, tests, and affected contracts without executing PR code.
4. Writes a private Markdown report and structured findings under `reports/`.
5. Ends with direct links to the GitHub PR and primary Linear issue.

When no reviews are waiting, the dispatcher archives itself without creating worker tasks.

The task binding is keyed by repository and PR number, so later commits and later review requests return to the same task. The SQLite database also carries accepted and submitted finding context into the next round.

## 6. Optional GitHub review actions

Automated runs never write to GitHub. In a completed PR task, ask Codex to draft review comments for selected findings. The repo-scoped `draft-pr-review` skill previews the exact payload before it can create a pending GitHub review. Then ask Codex to request changes; the separate `request-pr-changes` skill verifies and submits that pending review.

These actions require the existing `gh` authentication and explicit user requests. They abort if the PR head moved, if the expected pending review is missing, or if GitHub already has an unrelated pending review. The workflow does not approve PRs.

## 7. Moving to another Mac

Clone the repository and repeat the configuration steps. Normally do not copy `.cache/`, `.state/`, or `reports/`; they are machine-local. Recreate `config.json` with the new Codex project ID and local checkout paths, then create a new paused scheduled task. Repo-scoped skills under `.agents/skills/` arrive with the clone.

If the old Mac might still run the dispatcher, pause or delete its scheduled task before activating the new one. Queue state is local, so two active Macs cannot coordinate reservations with each other.

### Migrating an older installation

Run:

```sh
python3 bin/review_queue.py migrate-state
```

The command imports legacy `.state/reviews.json` entries into `.state/reviews.db` once. It leaves the JSON file in place as a backup. Existing historical reviews have no task binding or structured findings, so the next requested review for those PRs creates and binds a continuing task unless one is manually seeded.

## Troubleshooting

### `config.codex_project_id` needs replacement

The example configuration is still in use. Ask Codex to fill in the current local project's ID, or replace the placeholder manually.

### GitHub authentication fails

Run `gh auth status`, then `gh auth login` if necessary. This workflow relies on GitHub CLI's keychain-backed authentication and does not store a token in the repository.

### Linear context is missing

Confirm Linear is connected in Codex and that the PR is linked to an issue. The worker falls back to issue IDs found in the PR title, body, or branch and otherwise continues with code review only.

### A review remains claimed after task creation failed

Reset only that exact claim key:

```sh
python3 bin/review_queue.py reset --key 'OWNER/REPO#NUMBER@SHA'
```

### Review code must be executed to gain confidence

The unattended workflow deliberately does not execute untrusted PR code. Use existing GitHub CI as evidence, or run the code manually in an environment whose security boundary you control.
