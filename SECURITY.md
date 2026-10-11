# Security model

PANINI CLI runs an AI model that reads your code and proposes commands. This document says what it
protects against, and what it does not.

## Threats it is designed for

| Threat | Mitigation |
| --- | --- |
| **Prompt injection** from files, command output, web pages or MCP tools | Tool output is framed as data. Default Windows command execution uses native MXC containment; exceptions are scoped to task and policy. File changes, host operations, web and MCP calls follow the chosen approval mode. |
| **A repository running code when opened** | Project hooks require explicit configuration trust, rechecked per invocation, and run through the execution broker inside the task sandbox. User hooks remain trusted host programs. Only user-owned settings can change approvals or sandboxing. Git bookkeeping disables configured filters, signing, fsmonitor, hooks and external/textconv diff, and uses a minimal environment. |
| **Secrets reaching the model** | File tools and native MXC path exclusions protect recognized existing secret files. Command environments use an allowlist; unknown variables and credential-agent sockets are removed. Arbitrarily named or embedded secrets are not detected. |
| **Commands escaping the workspace** | Default on supported Windows: Microsoft MXC native BaseContainer, with workspace/scratch grants and toolchains read-only. Capability probes refuse unsupported guarantees and ACL-mutating fallbacks. Unsafe links are checked before launch. Exact host proxy grants retain direct-egress and host-port restrictions. |
| **Runaway processes** | Every command runs in a Job object (kill-on-close, process and memory caps) with a timeout. |
| **Tampering with the record** | Every tool call is written to a hash-chained receipt log; `panini-cli receipts SESSION` verifies it. |
| **Unbounded spend** | `budget_usd` / `PANINI_CLI_BUDGET_USD` stops a session at a priced cloud spend. |

## What is *not* protected

- **`scope=host` commands** run with your Windows user's access, including network and your files. They always need
  approval (unless you chose `auto` mode). Read-only commands auto-run only when they cannot read outside the workspace.
- **`auto` mode / `--approve-all`** approves everything. Use it only in disposable workspaces.
- **MCP servers** run as your user with the environment you give them; trust them as you would any program.
- **Trusted user hooks** run as your user. Project hooks require a sandbox even in an unrestricted session.
- **Browser previews** serve compiled assets or Node HTTP/WebSocket applications through a bounded, authenticated localhost broker. Node development servers remain inside MXC and use a keyed pipe bridge for HTTP, WebSocket and HMR traffic. Preview CSP restricts browser connections to the preview origin. Upstream cookies are stripped; this is not a general application authentication proxy. Arbitrarily named or embedded build secrets are not detected.
- **Disk exhaustion, concurrent filesystem races and kernel exploits** need stronger measures such as quotas or VM isolation. Read-only toolchain grants still expose the contents of those installations.
- **Recovery** is opt-in, bounded file restoration with conflict detection; it excludes secrets and does not undo external effects or recover automatically after a crash without a completion record.
- The **local model** can be less reliable. Routing and verification reduce unsupported completion claims, but do not guarantee correct answers. A pinned local model can attempt file changes; inspect its results and tests.
- `git` commands are auto-approved only inside the sandbox, because a repository's own git config can run programs.

## Reporting

See [docs/SANDBOX.md](docs/SANDBOX.md) for runtime installation, permissions, tested
behaviors and current limitations. This integration has not had an independent security audit.

Use [GitHub's private vulnerability reporting](https://github.com/suryacharanteja/panini-cli/security/advisories/new)
to contact the maintainers. Private reporting is enabled on this repository. Do not post credentials or exploit details in a public issue.

## Public beta and benchmark scope

The Windows executable is currently unsigned. Checksums identify the distributed bytes; they do not replace publisher signing.
Harbor trials run the Python CLI in disposable Linux task environments, with containment supplied by Harbor.
Their task scores measure agent behavior, not the Windows MXC boundary. Windows sandbox UAT is reported separately.
Pilot costs are estimates from the recorded price table, not an invoice or a billing guarantee.
