# Set up Nicholas PR Review

This guide installs the review skill for one user on macOS or Linux. It does not install or start the scheduled dispatcher, create local review state, or give the skill permission to post on GitHub.

## 1. Install the prerequisites

Install the agent you want to use, Git, and the [GitHub CLI](https://cli.github.com/). On macOS with Homebrew:

```sh
brew install git gh
```

Authenticate GitHub and confirm that you can read the repositories you review:

```sh
gh auth login
gh auth status
```

You also need access to the Linear workspace containing the issues linked to those pull requests. The skill deliberately stops if either GitHub or Linear context is unavailable.

## 2. Install the skill

### Codex, Claude Code, or both

The repository checkout is the recommended installation because the same command can support both agents and makes updates simple:

```sh
git clone --depth 1 https://github.com/nickfarm27/codex-pr-reviewer.git ~/.local/share/codex-pr-reviewer
~/.local/share/codex-pr-reviewer/peer/setup --host auto
```

Use `--host codex` or `--host claude` instead of `auto` when you only want one agent.

### Peer-only GitHub Release

To avoid cloning the dispatcher, download the matching `.tar.gz` and `.sha256` assets from the [latest GitHub Release](https://github.com/nickfarm27/codex-pr-reviewer/releases). Then verify and install them:

```sh
cd ~/Downloads
shasum -a 256 -c nickfarm27-pr-review-X.Y.Z.tar.gz.sha256
mkdir -p ~/.local/share
tar -xzf nickfarm27-pr-review-X.Y.Z.tar.gz -C ~/.local/share
~/.local/share/nickfarm27-pr-review/setup --host auto
```

Replace `X.Y.Z` with the release version. Keep the extracted directory in place because the installed skill links point to it.

### Native Claude Code plugin

Claude Code can install directly from the repository marketplace instead of using the checkout-based setup:

```sh
claude plugin marketplace add nickfarm27/codex-pr-reviewer
claude plugin install nickfarm27-pr-review@nickfarm27-tools --scope user
```

### Another Agent Skills-compatible harness

Tell setup where that agent keeps user-level skills:

```sh
~/.local/share/codex-pr-reviewer/peer/setup \
  --host custom \
  --skills-dir /path/to/agent/skills
```

The other harness must support the Agent Skills `SKILL.md` format, local shell commands, GitHub access, and remote MCP servers.

## 3. Connect Linear

For a checkout-based Codex installation:

```sh
codex mcp add linear --url https://mcp.linear.app/mcp/readonly
codex mcp login linear
```

For a checkout-based Claude Code installation:

```sh
claude mcp add --scope user --transport http linear https://mcp.linear.app/mcp/readonly
claude mcp login linear
```

The native Claude plugin already declares the read-only Linear server; complete its OAuth prompt when Claude first connects. For another harness, add `https://mcp.linear.app/mcp/readonly` as a user-level streamable HTTP MCP server and complete its OAuth flow.

## 4. Verify the installation

For an installation created by `setup`, run:

```sh
~/.local/share/codex-pr-reviewer/peer/bin/doctor --host auto
```

For a peer-only release installation, use `~/.local/share/nickfarm27-pr-review/bin/doctor --host auto` instead.

Use `--host codex`, `--host claude`, or `--host custom --skills-dir /path/to/agent/skills` when appropriate. For the native Claude plugin, verify it with:

```sh
claude plugin list
```

Restart or reload the agent so it discovers the new skill. The plugin will prompt for Linear OAuth when it first connects.

## 5. Run a first review

Open the repository containing the pull-request branch and invoke the skill explicitly:

```text
$nickfarm27-pr-review Review https://github.com/owner/repository/pull/123
```

Before reviewing code, the skill checks its installed release, resolves the exact GitHub pull request, and reads a trustworthy related Linear issue. A successful result is returned only in the conversation; it does not comment on or modify the pull request.

## Update or remove it

For a checkout or release installation:

```sh
~/.local/share/codex-pr-reviewer/peer/upgrade
~/.local/share/codex-pr-reviewer/peer/uninstall --host auto
```

For a peer-only release, replace `~/.local/share/codex-pr-reviewer/peer` with `~/.local/share/nickfarm27-pr-review`.

For the native Claude plugin:

```sh
claude plugin update nickfarm27-pr-review@nickfarm27-tools
claude plugin uninstall nickfarm27-pr-review@nickfarm27-tools
```

Reload the agent after an update.

## Common blockers

- **The skill is out of date:** run `upgrade` or the Claude plugin update command, reload the agent, and invoke it again.
- **GitHub cannot be read:** run `gh auth status`, then `gh auth login` if needed. Confirm that the account can open the target repository and pull request.
- **Linear is unavailable:** confirm the `linear` MCP server is present, finish its OAuth flow, and make sure the linked issue belongs to a workspace you can access.
- **No Linear issue can be identified:** add the issue link or identifier to the pull-request title or description, or provide the issue explicitly when invoking the skill.
- **An existing skill blocks setup:** setup never overwrites another file or link. Remove or rename the conflicting `nickfarm27-pr-review` entry yourself only after confirming that it is safe.
