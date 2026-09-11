# Set up Nicholas PR Review

Install the private, report-only review skill at user scope on macOS or Linux. Installation never enables the scheduled dispatcher, creates review state, or modifies a project repository.

## Before you start

You need Codex, Claude Code, or another Agent Skills-compatible client, plus curl, Git, and the [GitHub CLI](https://cli.github.com/). On macOS with Homebrew:

```sh
brew install git gh
```

You also need access to the GitHub repositories being reviewed and the Linear workspace containing their related issues. The installer can start both login flows, but you must approve access interactively.

## Level 1 — one-command installation

This is the recommended path for most peers. It detects Codex and Claude Code, downloads the latest `peer-v*` release, verifies its SHA-256 checksum, installs the skill, configures Linear's read-only MCP endpoint, starts required OAuth, and runs diagnostics.

```sh
/bin/bash <(curl -fsSL https://raw.githubusercontent.com/nickfarm27/codex-pr-reviewer/main/peer/install) --host auto
```

Use `--host codex` or `--host claude` to configure only one agent. The installer is safe to rerun and refuses to replace unrelated files, directories, skill links, or MCP configurations.

If the machine cannot open interactive OAuth during installation, add `--skip-oauth`, then run the printed login command later.

## Level 2 — ask your agent to install it

Open Codex or Claude Code and paste the instruction in [INSTALL_PROMPT.md](INSTALL_PROMPT.md). The agent first inspects the bootstrap script, runs the same one-command installation, pauses for your OAuth approvals, and reports the installed version and diagnostics.

This is the easiest option for someone who does not want to operate the terminal themselves. It performs the same installation as Level 1 and does not add files to their current project.

## Level 3 — manual installation and troubleshooting

Use this path when you want to inspect each step or need a custom agent location.

### 1. Authenticate GitHub

```sh
gh auth login
gh auth status
```

### 2. Download the peer package

Download the matching `.tar.gz` and `.sha256` files from the [latest GitHub Release](https://github.com/nickfarm27/codex-pr-reviewer/releases), then verify and extract them:

```sh
cd ~/Downloads
shasum -a 256 -c nickfarm27-pr-review-X.Y.Z.tar.gz.sha256
mkdir -p ~/.local/share
tar -xzf nickfarm27-pr-review-X.Y.Z.tar.gz -C ~/.local/share
```

Replace `X.Y.Z` with the release version. Keep the extracted directory in place because the installed skill links point to it.

Alternatively, use a repository checkout:

```sh
git clone --depth 1 https://github.com/nickfarm27/codex-pr-reviewer.git ~/.local/share/codex-pr-reviewer
```

### 3. Register the skill

For a peer-only release:

```sh
~/.local/share/nickfarm27-pr-review/setup --host auto --skip-doctor
```

For a repository checkout:

```sh
~/.local/share/codex-pr-reviewer/peer/setup --host auto --skip-doctor
```

Use `--host codex` or `--host claude` for one client. Codex is installed at user scope under `~/.agents/skills`; Claude Code uses `~/.claude/skills`.

For another Agent Skills-compatible client, provide its user-level skill directory:

```sh
~/.local/share/nickfarm27-pr-review/setup \
  --host custom \
  --skills-dir /path/to/agent/skills \
  --skip-doctor
```

### 4. Connect Linear

For Codex:

```sh
codex mcp add linear --url https://mcp.linear.app/mcp/readonly
codex mcp login linear
```

For Claude Code:

```sh
claude mcp add --scope user --transport http linear https://mcp.linear.app/mcp/readonly
claude mcp login linear
```

The native Claude plugin already declares this read-only server; approve its OAuth prompt when Claude first connects. For another client, add the same URL as a user-level streamable HTTP MCP server and complete OAuth.

### 5. Verify the installation

For a peer-only release:

```sh
~/.local/share/nickfarm27-pr-review/bin/doctor --host auto
```

For a repository checkout:

```sh
~/.local/share/codex-pr-reviewer/peer/bin/doctor --host auto
```

Use the corresponding single-host or custom-host arguments when appropriate. Restart or reload the agent if the skill does not immediately appear.

### Native Claude Code plugin alternative

Claude Code can install directly from the repository marketplace:

```sh
claude plugin marketplace add nickfarm27/codex-pr-reviewer
claude plugin install nickfarm27-pr-review@nickfarm27-tools --scope user
```

## Run the first review

Open the repository containing the pull-request branch and invoke the skill explicitly:

```text
$nickfarm27-pr-review Review https://github.com/owner/repository/pull/123
```

Before reviewing code, the skill checks its installed release, resolves the exact GitHub pull request, and reads a trustworthy related Linear issue. The result stays in the conversation; it does not modify or comment on the pull request.

## Update or remove it

For the one-command or peer-release installation:

```sh
~/.local/share/nickfarm27-pr-review/upgrade
~/.local/share/nickfarm27-pr-review/uninstall --host auto
```

For a repository checkout:

```sh
~/.local/share/codex-pr-reviewer/peer/upgrade
~/.local/share/codex-pr-reviewer/peer/uninstall --host auto
```

For the native Claude plugin:

```sh
claude plugin update nickfarm27-pr-review@nickfarm27-tools
claude plugin uninstall nickfarm27-pr-review@nickfarm27-tools
```

Reload the agent after an update.

## Common blockers

- **A prerequisite is missing:** install the command named by the bootstrapper, then rerun the same command.
- **GitHub cannot be read:** run `gh auth login` and confirm access with `gh auth status`.
- **Linear is unavailable:** run the appropriate `mcp get linear` and `mcp login linear` commands, then confirm that the URL is `https://mcp.linear.app/mcp/readonly`.
- **No Linear issue can be identified:** link the issue in the pull-request title or description, or provide it when invoking the skill.
- **An existing skill blocks setup:** installation never overwrites another entry. Inspect and remove or rename the conflicting `nickfarm27-pr-review` entry only when you know it is safe.
- **An existing MCP server named `linear` uses another URL:** the bootstrapper deliberately stops instead of replacing it. Resolve that configuration manually.
