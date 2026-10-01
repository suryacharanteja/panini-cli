<p align="center">
  <img src="assets/panini-banner.svg" alt="PANINI CLI — local-first, routed, verified" width="100%">
</p>

<h1 align="center">PANINI&nbsp;CLI</h1>

<p align="center">
  <b>A terminal coding agent that spends tokens only when the task needs them.</b><br>
  Each request is routed to the cheapest model that can finish it: a local <b>Qwen 2.5</b> on <b>Ollama</b> for questions,
  <b>OpenAI</b> GPT-5.6 tiers for real work. Chosen by a <b>Jev</b>-backed router, checked by tests, recorded in receipts.
</p>

<p align="center">
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-d98a5a"></a>
  <img alt="Python 3.12+" src="https://img.shields.io/badge/python-3.12%2B-d98a5a">
  <img alt="Local model: Ollama" src="https://img.shields.io/badge/local-Ollama%20%C2%B7%20Qwen%202.5-d98a5a">
  <img alt="Cloud: OpenAI" src="https://img.shields.io/badge/cloud-GPT--5.6-d98a5a">
  <img alt="MCP" src="https://img.shields.io/badge/MCP-client%20%2B%20server-d98a5a">
</p>

---

> Named for **Pāṇini**, the ancient grammarian who described a whole language in about 4,000 terse rules.
> PANINI CLI follows the same idea: a small set of sharp tools, strict rules about what they may touch, and a router
> that picks the right model for each request.

## Measured, not claimed

Same ten tasks, same test oracle, fresh git repo per task ([`evals/`](evals/)):

| Arm (v2.0.0, 2026-10-02) | Solved | Cloud cost | Cost per solve | Mean time |
| --- | ---: | ---: | ---: | ---: |
| **`auto`** (routed: local → luna → terra) | **10/10** | **$0.226** | **$0.023** | 24.2 s |
| pinned `gpt-5.6-terra` | 10/10 | $0.462 | $0.046 | 19.1 s |

Routing halved the cost per solve on this set, at about 5 s more per task. Ten small tasks is a small sample, and the
first run looked very different: `auto` 5/10 vs Terra 8/10, with the 3B local model solving 0/10 on its own and once
claiming an edit it never made. That run is why the local tier now only answers questions, and why local answers that
narrate, guess about unread files, or claim unmade changes are redone in the cloud. Both runs are kept in
[`evals/results/`](evals/results/).

Per-task results are in [`evals/results/`](evals/results/). Codex CLI was not installed on the test machine, so it has not
been compared yet; the harness supports it (`--arm codex`). Run it on your own tasks before trusting any number here.

## What it does

| | |
| --- | --- |
| **Per-task routing** | `local` → `gpt-5.6-luna` → `gpt-5.6-terra` → `gpt-5.6-sol`. Jev or a free offline estimate picks the tier; guards stop small models from taking edits or long sessions. A bad local answer is redone in the cloud. The router pauses the local tier if it keeps failing on your machine. [docs/ROUTER.md](docs/ROUTER.md) |
| **Local scouts, cloud surgeon** | Cloud models can call `explore`: a read-only sub-agent on your local model searches the code and returns `path:line` notes, so cloud tokens go to the edit. |
| **Cost ledger and budgets** | `$` per turn and session, `--budget`-style limits (`budget_usd`), `panini-cli usage` with spend by model and what local routing handled. |
| **Verification before "done"** | Detects `pytest`, `ruff`, `npm test`, `cargo test`, `go test`, `dotnet test`. If the agent changes files and tries to finish without running anything, it is sent back once to verify. |
| **Git checkpoints** | Every turn is bracketed by snapshots in `refs/panini/` (your index and branches are untouched). `/undo` reverts what the turn changed, including command side effects, and skips files you edited since. `/diff`, `/review`, `/commit`. |
| **Approvals that don't nag** | Read-only commands (`git status`, `ls`, `rg`, `Get-Content …`) inside the workspace run without asking. Answer `a` to allow a command for the session. Modes: `read-only`, `ask`, `edits`, `auto`. |
| **Sandbox** | Windows 11 AppContainer: workspace read/write, your toolchains read-only, no network, real exit codes. Docker and Codex-sandbox adapters too. [SECURITY.md](SECURITY.md) |
| **Context management** | Auto-compaction at 80 % of the window, with the summary written by the local model, plus `/compact`. |
| **MCP** | Client: tools from your MCP servers, behind approvals and receipts. Server: `panini-cli mcp-server` exposes `panini_route` and `panini_run`. |
| **Web and images** | `web_fetch` (approval-gated), OpenAI hosted web search (`web_search = true`), `--image` / `/image` for screenshots. |
| **Automation** | `exec --json` (JSONL events), `--output-schema`, prompt from stdin (`exec -`), distinct exit codes, `app-server --stdio`. |
| **Receipts** | Hash-chained log of every tool call; `panini-cli receipts SESSION --export report.md`. |

## Quick start

```powershell
# Install (Python 3.12+)
git clone https://github.com/<you>/panini-cli.git
cd panini-cli
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -e .

# Local model (https://ollama.com)
ollama pull qwen2.5:3b-instruct

# OpenAI key for real work (stored in Windows Credential Manager)
.venv\Scripts\panini-cli auth login

# Check, configure, go
.venv\Scripts\panini-cli doctor
.venv\Scripts\panini-cli config init        # writes ~/.panini/config.toml with profiles
.venv\Scripts\panini-cli                    # interactive session in the current folder
.venv\Scripts\panini-cli exec "fix the failing test in tests/test_api.py"
```

A prebuilt Windows executable can be produced with PyInstaller (see *Development*).

## Everyday use

```text
› what does src/router.py do?              ← answered by the local model, $0
› fix the off-by-one in @src/paging.py     ← @file attaches the file; routed to a cloud tier
› /diff                                    ← everything this session changed
› /review                                  ← a strong model reviews the diff, read-only
› /undo                                    ← put back what the last turn changed
› /commit                                  ← message drafted locally, you confirm
```

Keys: Enter sends · Alt+Enter new line · ↑/↓ and Ctrl+R history · Tab completes `/commands` and `@files`.
The bottom bar shows model, approval mode, session cost and context use.

Other commands: `/model NAME|local|auto`, `/approvals MODE`, `/image PATH`, `/compact`, `/plan`, `/new`, `/exit`.

## Commands

```
panini-cli                          interactive session
panini-cli exec "task"              one task  (--json, --output-schema F, --image F, -q, -a MODE, "-" = stdin)
panini-cli resume [ID] · sessions   continue / list conversations
panini-cli models · route "task"    router status / dry-run a routing decision
panini-cli usage [--days N]         spend, local savings, escalations
panini-cli config [show|init|path]  ~/.panini/config.toml and profiles (-p PROFILE on any command)
panini-cli mcp-server               PANINI as an MCP server
panini-cli receipts ID [--export F] verify (and export) a session's receipt chain
panini-cli doctor · sandbox status · auth login|status|logout · hooks [--trust] · jev setup|status
panini-cli app-server --stdio       JSON-RPC coding protocol for other hosts
```

Exit codes for `exec`: 0 done, 1 failed, 3 step limit, 4 budget reached, 130 interrupted.

## Configuration

`~/.panini/config.toml` (create with `panini-cli config init`). Environment variables and flags override it.

```toml
model = "auto"                  # auto | local | <OpenAI model> | ollama/<name>
approval_mode = "ask"           # read-only | ask | edits | auto
local_model = "qwen2.5:3b-instruct"
local_strong_model = "qwen2.5:7b-instruct-q4_K_M"   # tried before the cloud when the small one fails
budget_usd = 2.0
web_search = false

[profiles.offline]
model = "local"

[prices]                        # USD per million tokens [input, output], for the ledger
"gpt-5.6-terra" = [2.0, 12.0]

[mcp_servers.filesystem]
command = "npx"
args = ["-y", "@modelcontextprotocol/server-filesystem", "."]
trusted_tools = ["read_file"]
```

Every key has an environment variable (`PANINI_CLI_MODEL`, `PANINI_CLI_BUDGET_USD`, `PANINI_CLI_LOCAL_MODEL`, …);
see [`userconfig.py`](src/panini_cli/agent/userconfig.py) and [.env.example](.env.example). Keys and secrets never go
in a project folder: use `panini-cli auth login`, environment variables, or PANINI's own `.env`.

Project rules go in `AGENTS.md`: `~/.panini/AGENTS.md`, then every `AGENTS.md` from the repository root down to the
workspace. Deeper ones are listed for the agent to read before working in their directory.

## Development

```powershell
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m ruff check src tests evals
.venv\Scripts\python -m pytest -q                      # offline: scripted models, mocked HTTP, fake MCP server
.venv\Scripts\python evals/run.py --arm auto --arm model:gpt-5.6-terra   # live, costs a little
```

Windows executable:

```powershell
.venv\Scripts\python -m pip install pyinstaller
.venv\Scripts\python -m PyInstaller --noconfirm --clean --onedir --name panini-cli --icon assets/panini-cli.ico `
  --paths src --collect-data panini_cli.jev --collect-submodules panini_cli --collect-submodules openai `
  --collect-submodules prompt_toolkit --hidden-import flatbuffers packaging_entry.py
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the rule on routing and prompt changes (they ship only with eval results),
and [docs/AUDIT-2026-10-01.md](docs/AUDIT-2026-10-01.md) for the audit this release implements.

## License

[MIT](LICENSE) © 2026 PANINI contributors.
