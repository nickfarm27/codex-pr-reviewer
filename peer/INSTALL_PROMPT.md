# Agent-assisted installation

Paste the following message into Codex or Claude Code. The agent should inspect the installer before executing it and must leave project repositories unchanged.

```text
Install Nicholas PR Review globally for every supported agent detected on this machine.

First inspect the installer at:
https://raw.githubusercontent.com/nickfarm27/codex-pr-reviewer/main/peer/install

If it only downloads and verifies the latest peer-v* release, installs under my user account, configures the read-only Linear MCP endpoint, and runs diagnostics, execute:

/bin/bash <(curl -fsSL https://raw.githubusercontent.com/nickfarm27/codex-pr-reviewer/main/peer/install) --host auto

Allow me to complete any GitHub or Linear OAuth prompts. Do not modify the current project repository. When finished, tell me which agents were configured, the installed version, and whether all diagnostics passed.
```

For one agent, replace `--host auto` with `--host codex` or `--host claude`.
