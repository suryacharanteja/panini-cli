# Security model

PANINI CLI runs an AI model that reads your code and proposes commands. This document says what it
protects against, and what it does not.

## Threats it is designed for

| Threat | Mitigation |
| --- | --- |
| **Prompt injection** from files, command output, web pages or MCP tools | All tool output is framed as data; every change and command still needs approval (or a mode you chose); web and MCP output is marked "data only". |
| **A repository running code when opened** | Project hooks (`.panini/hooks.json`) run only after `panini-cli hooks --trust`, pinned to the file's exact content. A project cannot ship config that changes approvals or sandboxing — only `~/.panini/config.toml` is read. Git bookkeeping runs with `core.fsmonitor`, hooks and external diff disabled. |
| **Secrets reaching the model** | File tools refuse `.env`, keys, keystores and credential files (`tools.is_secret`); `@file` mentions use the same rule. API keys and token-like variables are removed from command environments. |
| **Commands escaping the workspace** | Default on Windows 11: an AppContainer sandbox (`native`) — workspace read/write, toolchains read-only, no network. The boundary is self-tested before use (re-checked every 10 minutes). Hard links and links pointing outside the workspace are refused. |
| **Runaway processes** | Every command runs in a Job object (kill-on-close, process and memory caps) with a timeout. |
| **Tampering with the record** | Every tool call is written to a hash-chained receipt log; `panini-cli receipts SESSION` verifies it. |
| **Unbounded spend** | `budget_usd` / `PANINI_CLI_BUDGET_USD` stops a session at a priced cloud spend. |

## What is *not* protected

- **`scope=host` commands** run with your Windows user's access, including network and your files. They always need
  approval (unless you chose `auto` mode). Read-only commands auto-run only when they cannot read outside the workspace.
- **`auto` mode / `--approve-all`** approves everything. Use it only in disposable workspaces.
- **MCP servers** run as your user with the environment you give them; trust them as you would any program.
- The **local model** is weaker and easier to mislead; it is offered only read-only questions by the router, and its
  answers that claim unmade changes are discarded.
- `git` commands are auto-approved only inside the sandbox, because a repository's own git config can run programs.

## Reporting

Please report vulnerabilities privately to the maintainers rather than in a public issue.
