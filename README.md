<p align="center">
  <img src="assets/panini-banner.svg" alt="PANINI CLI — local-first, routed, verified" width="100%">
</p>

<h1 align="center">PANINI&nbsp;CLI</h1>

<p align="center">
  <b>A terminal coding agent that spends tokens only when the task needs them.</b><br>
  Simple requests run on a local <b>Qwen 2.5</b> model through <b>Ollama</b> — free, private, offline.<br>
  Harder ones go to <b>OpenAI</b>. A <b>Jev</b>-backed router picks the cheapest model that can finish the job.
</p>

<p align="center">
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-d98a5a"></a>
  <img alt="Python 3.12+" src="https://img.shields.io/badge/python-3.12%2B-d98a5a">
  <img alt="Local model: Ollama" src="https://img.shields.io/badge/local-Ollama%20%C2%B7%20Qwen%202.5-d98a5a">
  <img alt="Cloud: OpenAI" src="https://img.shields.io/badge/cloud-OpenAI-d98a5a">
  <img alt="Router: Jev" src="https://img.shields.io/badge/router-Jev-d98a5a">
</p>

---

> Named for **Pāṇini**, the ancient grammarian who described a whole language in about 4,000 terse rules.
> PANINI CLI follows the same idea: a small set of sharp tools, strict rules about what they may touch, and a router
> that picks the right model for each request.

## Why PANINI CLI

Most coding agents send every request — `git status` and "migrate the auth layer" alike — to the same expensive model.
PANINI CLI routes **per task**:

| Task | Example | Runs on | Cost |
| --- | --- | --- | --- |
| **Local** | "what's in NOTES.md?", `git status`, a rename | `ollama/qwen2.5` on your machine | $0, private |
| **Cheap** | "fix the failing test", small edits | `gpt-4.1-nano` | cents |
| **Balanced** | bounded debugging, setup, test repair | `gpt-4.1-mini` | |
| **Strong** | multi-file changes, unfamiliar failures | `gpt-4.1` | |

If a local answer turns out unusable (unknown tool, malformed call, empty reply, Ollama down), the same request
**escalates once to the cloud**, and local routing stays off for the rest of that session. You never get stuck on a
model that cannot do the job.

## Quick start

```powershell
# 1. Install (Python 3.12+)
git clone https://github.com/<you>/panini-cli.git
cd panini-cli
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -e .

# 2. Pull the local model (Ollama: https://ollama.com)
ollama pull qwen2.5:3b-instruct

# 3. Optional: an OpenAI key for the harder tasks
.venv\Scripts\panini-cli auth login        # stored in Windows Credential Manager
# or: set OPENAI_API_KEY=sk-...

# 4. Check everything
.venv\Scripts\panini-cli doctor
.venv\Scripts\panini-cli models

# 5. Go
.venv\Scripts\panini-cli                              # interactive session in the current folder
.venv\Scripts\panini-cli exec "add a docstring to utils.py"
```

**No OpenAI key?** PANINI CLI still starts and serves everything from your local model. Hard tasks simply need a key.

## The router

```
        request ──► explicit override? ("use qwen", "use gpt-4.1")  ──► that model
                          │ no
                          ▼
                 Jev configured? ──yes──► Jev scores every model (boolean per model)
                          │ no                     │
                          ▼                        ▼
             offline complexity estimate     guards below veto or adjust the pick
                          │                        │
                          └────────────► chosen model ◄─────────────┘
```

**Guards** apply to every route, whichever way it was chosen:

- **Complexity veto** – Jev cannot send a hard task (refactor, auth, migration, wide-blast-radius work) to the small model.
- **Machine-task floor** – questions about the local PC (RAM, disks, processes, known folders) never use the local model.
- **No downgrade into local mid-session** – once a cloud model has built up history, the session does not drop to the 3B model.
- **Low-confidence no-downgrade / cap** – uncertain routing never moves to a weaker model, and never jumps above *balanced*.
- **Routed once per user message** – a task never bounces between backends between tool calls; the only mid-task move is the local → cloud escalation.

Inspect it without running anything:

```powershell
panini-cli models                                  # tiers, Ollama status, Jev status
panini-cli route "what is in the README?"          # -> ollama/qwen2.5:3b-instruct  (offline:local)
panini-cli route "refactor auth across all files"  # -> gpt-4.1                     (offline:frontier)
```

Every routed turn prints its decision, e.g. `Model selected: ollama/qwen2.5:3b-instruct (offline:local (jev off))`.

### Enabling Jev

Jev is optional. Without it the router uses a free, deterministic offline estimate ([`complexity.py`](src/panini_cli/agent/complexity.py)).
With Jev, each model gets an independent yes/no evaluation ("should we use *this* model for *this* request?") for a fraction of a cent.

```powershell
set AI_GATEWAY_API_KEY=your_vercel_ai_gateway_key   # Node.js 20+ required
panini-cli jev setup                                # installs the sidecar into ~\.panini\jev
panini-cli jev status
```

Full details, tuning and the decision table: [docs/ROUTER.md](docs/ROUTER.md).

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `PANINI_CLI_MODEL` | `auto` | `auto` routes; `local` pins the Ollama model; or any OpenAI model id / `ollama/<name>` |
| `PANINI_CLI_LOCAL` | `auto` | `off` disables the local tier |
| `PANINI_CLI_LOCAL_MODEL` | `qwen2.5:3b-instruct` | Any tool-capable Ollama model, e.g. `qwen2.5:7b-instruct-q4_K_M` |
| `PANINI_CLI_LOCAL_CTX` | `16384` | Context window requested from Ollama |
| `OLLAMA_HOST` | `http://127.0.0.1:11434` | Ollama server |
| `PANINI_CLI_ROUTER_MODELS` | nano / mini / 4.1 | Cloud tiers: `id:tier:description,…` |
| `PANINI_CLI_ROUTER_FALLBACK` | `gpt-4.1` | Where a failed local turn escalates |
| `AI_GATEWAY_API_KEY` | – | Turns on Jev routing |
| `PANINI_CLI_APPROVAL_MODE` | `ask` | `read-only`, `ask`, `edits`, `auto` |
| `PANINI_CLI_SANDBOX` | `native` on Windows | `none`, `native`, `codex`, `docker` |

See [.env.example](.env.example) for the full list.

## Agent design

The router decides *which model* thinks. Everything below decides *what that model may do*.

- **Approval modes** — `read-only`, `ask`, `edits`, `auto`. Every file change and command is shown and gated by default.
- **Sandboxed commands** — a bundled Windows AppContainer sandbox, with optional Codex-sandbox or Docker adapters. Commands run in a
  killable process tree *without* your credentials; the host scope needs explicit approval.
- **Workspace-bound tools** — `list_files`, `read_file`, `search_files`, `search_code`, `write_file`, `edit_file`, `apply_patch`,
  `run_command`, `terminal_*`, `update_plan`. Reads never touch secret files.
- **Reviewable edits, reversible turns** — diffs before apply; `/undo` reverts the last turn's file changes, and refuses to clobber
  a file you edited since.
- **`AGENTS.md`** — project rules (and a global `~/.panini/AGENTS.md`) are loaded into the agent's instructions.
- **Hooks** — `PreToolUse`, `PostToolUse`, `TurnEnd`, `SessionStart`. Project hooks run only after `panini-cli hooks --trust`, so opening a
  repository can never run code on your machine.
- **Receipts** — a hash-chained log of every tool call; `panini-cli receipts <session>` verifies it.
- **Sessions** — `panini-cli sessions`, `panini-cli resume [ID]`.
- **Context budgeting** — old tool outputs are dropped when history exceeds the budget, keeping the recent ones.
- **App-server** — `panini-cli app-server --stdio` speaks the JSON-RPC protocol (`thread/start`, `turn/start`, `turn/interrupt`) so
  other tools can drive PANINI CLI as their coding runtime.

## Commands

```
panini-cli                       interactive session in the current directory
panini-cli exec "task"           run one task and exit         (-q quiet, -a auto|edits|ask|read-only)
panini-cli resume [ID]           continue the last conversation in this workspace
panini-cli sessions              list saved conversations
panini-cli models                show the router: local model, cloud tiers, Jev
panini-cli route "task"          dry-run the router on a prompt
panini-cli jev setup|status      manage the Jev sidecar
panini-cli doctor                check configuration and model access
panini-cli auth login|status|logout
panini-cli hooks [--trust]       list hooks, or trust the project's
panini-cli receipts SESSION_ID   verify a session's receipt chain
panini-cli app-server --stdio    serve the coding protocol on stdio
```

In a session: `/model NAME` (or `/model local`, `/model auto`), `/new`, `/plan`, `/undo`, `/approvals MODE`, `/exit`.

## Development

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m pytest
```

The suite runs offline: the Ollama backend is tested against a mocked HTTP transport, and models are scripted.

```
src/panini_cli/
  agent/
    model_router.py   tiers, Jev + offline routing, guards
    complexity.py     offline task-complexity estimate
    ollama.py         Ollama backend (Responses items <-> chat messages)
    model.py          RoutedModel: OpenAI / Ollama dispatch + escalation
    loop.py           the agent loop; routes once per user message
    tools.py sandbox.py native_sandbox.py hooks.py receipts.py sessions.py server.py cli.py
  jev/                Jev sidecar client (+ bundled Node sidecar)
tests/
docs/ROUTER.md
```

## Status

Version 1.1. The local tier is verified live against `qwen2.5:3b-instruct` on Ollama; a 3B model is deliberately kept to
small, self-contained requests. For heavier local work set `PANINI_CLI_LOCAL_MODEL=qwen2.5:7b-instruct-q4_K_M`.
Jev routing has not been benchmarked against the offline estimate on real solve rates yet — treat it as opt-in
(see [JEV-RETRIEVAL.md](JEV-RETRIEVAL.md) for the same cost-and-solve gate applied to code search).

## License

[MIT](LICENSE) © 2026 PANINI contributors.
