# Nicholas PR Review

Install Nicholas's private, report-only pull-request review workflow into Codex or Claude Code. The skill requires authenticated GitHub access and Linear's read-only MCP server, checks that its release is current before every review, and never writes to the reviewed repository or publishes a GitHub review.

## Install

Most peers need one command:

```sh
/bin/bash <(curl -fsSL https://raw.githubusercontent.com/nickfarm27/codex-pr-reviewer/main/peer/install) --host auto
```

The bootstrapper downloads and verifies the latest peer release, installs it at user scope, configures the read-only Linear MCP server, starts OAuth, and runs diagnostics. Use `--host codex` or `--host claude` for one client.

There are three supported installation levels:

1. **One command:** use the bootstrapper above.
2. **Agent-assisted:** paste [INSTALL_PROMPT.md](INSTALL_PROMPT.md) into Codex or Claude Code.
3. **Manual:** follow [SETUP.md](SETUP.md) to inspect and run every step yourself or configure another harness.

## Requirements

- macOS or Linux with Git, Bash, and curl
- [GitHub CLI](https://cli.github.com/) authenticated with `gh auth login`
- Codex, Claude Code, or both
- Access to the Linear workspace containing the PR's related issue

## Manual alternatives

Install from the repository:

```sh
git clone --depth 1 https://github.com/nickfarm27/codex-pr-reviewer.git ~/.local/share/codex-pr-reviewer
~/.local/share/codex-pr-reviewer/peer/setup --host auto --skip-doctor
```

Use `--host codex` or `--host claude` to install for one harness. The setup script creates safe user-level skill links and refuses to replace an existing file, directory, or foreign link with the same name.

Claude Code can alternatively install the native plugin directly from this GitHub repository:

```sh
claude plugin marketplace add nickfarm27/codex-pr-reviewer
claude plugin install nickfarm27-pr-review@nickfarm27-tools --scope user
```

For another harness that supports the Agent Skills `SKILL.md` format, pass its user-level skills directory explicitly:

```sh
~/.local/share/codex-pr-reviewer/peer/setup --host custom --skills-dir /path/to/agent/skills
```

This installs the portable core only. The harness must be able to run local shell commands, inspect Git/GitHub, and connect to remote MCP servers; otherwise the required preflight stops the review.

Connect Linear if setup reports that it is missing:

```sh
codex mcp add linear --url https://mcp.linear.app/mcp/readonly
claude mcp add --scope user --transport http linear https://mcp.linear.app/mcp/readonly
```

Complete the OAuth flow in the relevant client. The skill proves access by reading the related Linear issue before it begins a review.

Or install from a GitHub Release:

Each `peer-vX.Y.Z` release contains only this peer package and an SHA-256 checksum. Download both assets from the repository's Releases page, verify the checksum, extract the archive under `~/.local/share/`, and run `setup` from the extracted directory.

```sh
shasum -a 256 -c nickfarm27-pr-review-X.Y.Z.tar.gz.sha256
tar -xzf nickfarm27-pr-review-X.Y.Z.tar.gz -C ~/.local/share
~/.local/share/nickfarm27-pr-review/setup --host auto
```

Keep the extracted directory in place because the installed skills point to it. `upgrade` replaces a release installation safely after verifying the next release checksum.

## Use

In Codex:

```text
$nickfarm27-pr-review Review the PR for my current branch.
```

In Claude Code, invoke the installed skill by name and provide a PR URL or use a branch with an open PR.

The skill stops without reviewing when it cannot verify the latest release, read the GitHub PR, connect to Linear, or resolve a trustworthy related issue.

## Diagnose, update, and remove

```sh
./peer/bin/doctor --host auto
./peer/upgrade
./peer/uninstall --host auto
```

`doctor` and `uninstall` cover installations created by `setup`. For a native Claude marketplace installation, use `claude plugin list`, `claude plugin update nickfarm27-pr-review@nickfarm27-tools`, and `claude plugin uninstall nickfarm27-pr-review@nickfarm27-tools` instead.

When running from a release archive, omit the `peer/` prefix. A clone updates with a clean fast-forward of the repository. A release installation downloads and verifies the latest peer release in place, so Codex, Claude, and custom-harness links remain valid. After an update, reload the agent before invoking the skill again.

## Maintainers

`prompts/review-core.md` is the canonical, harness-neutral review policy. The dispatcher wrapper references it directly. The portable skill contains an exact release snapshot, and tests reject the package if that copy drifts. Peer-specific instructions in `SKILL.md` provide the required GitHub and Linear preflight, chat-only output, and report-only permission boundary.

Set the same semantic version in:

- `plugins/nickfarm27-pr-review/VERSION`
- both plugin manifests
- the skill's `metadata.version`
- the Claude marketplace entry

Run the full suite before tagging:

```sh
python3 -m unittest discover -s tests -v
```

Pull-request CI requires a version bump whenever the shared review policy or release payload changes. Documentation and test-only changes outside `peer/` do not require one.

Push a tag matching the current version, such as `peer-v0.2.0`. GitHub Actions validates the tag and package, creates the archive and checksum, and publishes them as GitHub Release assets.
