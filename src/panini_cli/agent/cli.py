"""panini-cli: PANINI-CLI in the terminal, and the app-server that PANINI drives.

panini-cli                  interactive session in the current directory
panini-cli exec "task"      run one task and exit
panini-cli resume [ID]      continue the last (or a given) conversation in this workspace
panini-cli sessions         list saved conversations for this workspace
panini-cli doctor           check configuration and model access
panini-cli app-server --stdio
"""

import argparse
import asyncio
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import VERSION, approvals, codex_sandbox, userconfig, hooks, native_probe, native_sandbox, receipts, sandbox, sessions
from .config import DEFAULT_MODEL, EFFORTS, Settings, configured_key, load_panini_env, local_enabled, local_model
from .loop import Agent, text_of
from .model import ModelError, create_model
from .model_router import actual_model, enabled as model_router_enabled, fallback_model, model_options, routing_options
from .shell import shell_name

FOUNDER = os.getenv("PANINI_FOUNDER") or "Charan"
COMPANY = os.getenv("PANINI_COMPANY") or "PANINI"
MODES = {
    "read-only": "read-only: changes and commands are declined; the agent can only read and plan",
    "ask": "ask before every change and command",
    "edits": "file changes pre-approved, commands ask",
    "auto": "everything pre-approved",
}
HELP = "@file attaches a file · /image PATH · /new · /diff · /review · /commit · /compact · /plan shows the plan · /undo reverts the last changes · /power enables fast UAT auto-approval · /approvals MODE · /model NAME switches model · /exit quits · Ctrl+C stops the current task"
LOCAL_HELP = "Local commands: auth login · auth status · auth logout · doctor · sandbox status · sessions · receipts SESSION_ID"

WELCOME = r"""
        ____   ___    _   __  ____  _   __  __
       / __ \ /   |  / | / / /  _/ / | / / / /
      / /_/ // /| | /  |/ /  / /  /  |/ / / /
     / ____// ___ |/ /|  / _/ /  / /|  / /_/
    /_/    /_/  |_/_/ |_/ /___/ /_/ |_/ (_)
"""


def local_only_ready():
    """No OpenAI key is needed when every request can be served by a pulled Ollama model."""
    if not local_enabled():
        return False
    from . import ollama

    return ollama.available()[0]


def key_location_help():
    return (
        "API key: run `panini-cli auth login` to save it in Windows Credential Manager. "
        "Fallbacks: set OPENAI_API_KEY, or place it in PANINI's own runtime .env, never a project .env. "
        "Without a key, simple tasks still run on your local Ollama model."
    )


def default_approval_mode():
    mode = os.getenv("PANINI_CLI_APPROVAL_MODE", "").strip().lower()
    return mode if mode in MODES else None


def print_welcome(console, settings, workspace, approvals):
    from rich.panel import Panel
    from rich.text import Text

    title = Text("PANINI-CLI", style="bold cyan")
    title.append(f" {VERSION}", style="dim")
    title.append(" · terminal agency for PANINI", style="bold")
    body = Text()
    body.append(WELCOME.strip("\n"), style="bold cyan")
    body.append("\n\n")
    body.append("Built by ", style="dim")
    body.append(FOUNDER, style="bold")
    body.append(f" for {COMPANY}. ", style="dim")
    body.append("Named for Panini, the ancient architect of grammar: tiny rules, vast expression.\n", style="dim")
    body.append("This CLI is PANINI's command-line capability: it plans, acts, verifies, and leaves receipts.\n\n")
    body.append("Model ", style="dim")
    body.append(settings.model, style="cyan")
    if model_router_enabled(settings.model):
        body.append(f" -> {fallback_model()} fallback", style="dim")
        if local_enabled():
            body.append(f" + local {local_model()}", style="dim")
    body.append(f" · {shell_name()}\n", style="dim")
    body.append("Workspace ", style="dim")
    body.append(str(workspace), style="white")
    body.append("\nApprovals ", style="dim")
    body.append(approvals, style="white")
    body.append("\n")
    body.append(key_location_help(), style="yellow" if not configured_key() else "dim")
    body.append("\n")
    body.append(HELP, style="dim")
    body.append("\n")
    body.append(LOCAL_HELP, style="dim")
    console.print(Panel(body, title=title, border_style="cyan", padding=(1, 2)))


def can_ask():
    """Whether a person can answer prompts. Windows reports the NUL device as a TTY, so
    there only a real console counts."""
    if not sys.stdin or not sys.stdin.isatty():
        return False
    if os.name != "nt":
        return True
    import ctypes
    import msvcrt

    try:
        handle = msvcrt.get_osfhandle(sys.stdin.fileno())
    except (OSError, ValueError):
        return False
    mode = ctypes.c_uint32()
    return bool(ctypes.windll.kernel32.GetConsoleMode(ctypes.c_void_p(handle), ctypes.byref(mode)))


class TerminalHost:
    """Shows the agent's work and asks before every file change and command."""

    def __init__(self, console, *, approve_edits=False, approve_all=False, quiet=False):
        from rich.markup import escape
        from rich.text import Text

        self.console, self.escape, self.Text = console, escape, Text
        self.approve_edits, self.approve_all, self.quiet = approve_edits, approve_all, quiet
        self.read_only = False
        self.open_line = False
        self.rules = approvals.SessionRules()
        self.trust_read_only = os.getenv("PANINI_CLI_TRUST_READ_ONLY", "1").lower() not in ("0", "false", "no")

    def set_mode(self, mode):
        self.read_only = mode == "read-only"
        self.approve_all = mode == "auto"
        self.approve_edits = mode in ("edits", "auto")

    @property
    def mode(self):
        if self.read_only:
            return "read-only"
        return "auto" if self.approve_all else "edits" if self.approve_edits else "ask"

    def end_line(self):
        if self.open_line:
            self.console.print()
            self.open_line = False

    def _write(self, text, style=None):
        self.console.print(self.Text(text, style=style or ""), end="")
        self.open_line = not text.endswith("\n")

    def _diff(self, diff):
        from rich.syntax import Syntax

        lines = diff.splitlines()
        self.console.print(
            Syntax("\n".join(lines[:200]), "diff", theme="ansi_dark", background_color="default")
        )
        if len(lines) > 200:
            self.console.print(f"[dim]  … {len(lines) - 200} more diff lines[/]")

    def _scope_label(self, scope):
        if scope == "host":
            return "Windows host access"
        if scope == "workspace":
            return "Workspace sandbox"
        return str(scope)

    def _scope_help(self, scope):
        if scope == "host":
            return "Can see your Windows user folders, installed tools, processes and network."
        if scope == "workspace":
            return "Limited to the selected workspace sandbox."
        return ""

    def _command_card(self, item, *, approving=False):
        from rich.panel import Panel
        from rich.syntax import Syntax
        from rich.table import Table

        scope = item.get("executionScope", "workspace")
        table = Table.grid(padding=(0, 1))
        table.add_column(style="dim", no_wrap=True)
        table.add_column()
        table.add_row("Access", self._scope_label(scope))
        help_text = self._scope_help(scope)
        if help_text:
            table.add_row("Meaning", help_text)
        table.add_row("Folder", item.get("cwd", ""))
        table.add_row("Command", "")
        table.add_row("", Syntax(item.get("command", ""), "powershell", theme="ansi_dark", word_wrap=True))
        title = "Approval Needed" if approving else "PANINI Action"
        border = "yellow" if approving else ("magenta" if scope == "host" else "cyan")
        self.console.print(Panel(table, title=title, border_style=border, padding=(1, 2)))

    async def started(self, item):
        kind, esc = item["type"], self.escape
        self.end_line()
        if kind == "dynamicToolCall" and not self.quiet:
            self.console.print(f"  [dim]• {esc(item['summary'])}[/]")
        elif kind == "fileChange":
            change = item["changes"][0]
            verb = "Create" if change["kind"]["type"] == "add" else "Edit"
            self.console.print(f"[bold]✎ {verb} {esc(change['path'])}[/]")
            self._diff(change["diff"])
        elif kind == "commandExecution":
            if not self.quiet:
                self._command_card(item)

    async def delta(self, item_id, kind, text):
        if kind == "agentMessage":
            self._write(text)
        elif not self.quiet:
            self._write(text, "dim")

    async def completed(self, item):
        kind, status, esc = item["type"], item.get("status"), self.escape
        self.end_line()
        if kind == "dynamicToolCall" and status == "failed" and not self.quiet:
            self.console.print(f"    [red]✗ {esc(item.get('error', ''))}[/]")
        elif kind == "fileChange":
            outcome = {
                "completed": "[green]  ✓ applied[/]",
                "declined": "[yellow]  ✗ not applied[/]",
            }.get(status, f"[red]  ✗ failed: {esc(item.get('error', ''))}[/]")
            self.console.print(outcome)
        elif kind == "commandExecution":
            code = item.get("exitCode")
            if status == "declined":
                self.console.print("[yellow]  ✗ not run[/]")
            elif code is None:
                self.console.print(f"[red]  ✗ stopped: {esc(item.get('error') or 'timed out')}[/]")
            else:
                seconds = item.get("durationMs", 0) / 1000
                color = "green" if code == 0 else "red"
                self.console.print(f"[{color}]  exit {code}[/] [dim]· {seconds:.1f}s[/]")

    async def hook_note(self, text):
        self.end_line()
        self.console.print(f"[magenta]  ⚑ {self.escape(text[:2000])}[/]")

    async def compacted(self, reason, before, after, by):
        self.end_line()
        self.console.print(
            f"[dim]  ⇣ context compacted ({reason}): ~{before:,} → ~{after:,} tokens, summary by {self.escape(by)}[/]"
        )

    async def model_route(self, decision):
        if self.quiet:
            return
        self.end_line()
        detail = f"Model selected: {decision.model} ({decision.reason})"
        if decision.confidence:
            detail += f" · confidence {decision.confidence:.0%}"
        if decision.cost_usd is not None:
            detail += f" · JEV ${decision.cost_usd:.6f}"
        self.console.print(f"[dim]  {self.escape(detail)}[/]")

    async def plan(self, steps):
        if not self.quiet:
            self.plan_now(steps)

    def plan_now(self, steps):
        self.end_line()
        if not steps:
            self.console.print("[dim]  No plan yet.[/]")
        marks = {"completed": "[green]✓[/]", "in_progress": "[cyan]›[/]", "pending": "[dim]○[/]"}
        for step in steps:
            self.console.print(f"  {marks.get(step['status'], '○')} {self.escape(step['step'])}")

    async def approve(self, item):
        change = item["type"] == "fileChange"
        if self.read_only and not change and self.trust_read_only and approvals.is_trusted(item.get("command", "")) \
                and item.get("executionScope") == "workspace":
            return "accept"  # Read-only mode still allows sandboxed commands that only read.
        if self.read_only:
            self.console.print("[yellow]  Read-only mode: not applied. Switch with /approvals ask.[/]")
            return "decline"
        if self.approve_all or (change and self.approve_edits):
            return "accept"
        if not change and self.auto_approved(item):
            return "accept"
        if not can_ask():
            self.console.print(
                "[yellow]  Needs approval, but nobody can answer here. Run interactively, "
                "or pass --approve-edits or --approve-all.[/]"
            )
            return "decline"
        self.end_line()
        try:
            scope = item.get("executionScope", "workspace")
            if item["type"] == "commandExecution":
                self._command_card(item, approving=True)
            answer = self.console.input(
                "  Apply this change? y = yes, a = yes to all file changes this session, n = no [y/a/N] "
                if change else
                "  Run with Windows host access? y = yes, a = always this command this session, n = no [y/a/N] "
                if scope == "host" else
                "  Run in the workspace sandbox? y = yes, a = always this command this session, n = no [y/a/N] "
            )
        except EOFError:
            return "decline"
        answer = answer.strip().lower()
        if answer in ("a", "always"):
            if change:
                self.approve_edits = True
                self.console.print("[dim]  File changes are approved for the rest of this session (/approvals ask to undo).[/]")
            else:
                self.rules.allow(item.get("command", ""))
            return "accept"
        return "accept" if answer in ("y", "yes") else "decline"

    def auto_approved(self, item):
        """Read-only commands, and commands the user said "always" to, run without a prompt."""
        command = item.get("command", "")
        if self.rules.allowed(command):
            self.console.print("[dim]  ✓ approved earlier in this session[/]")
            return True
        if not self.trust_read_only or not approvals.is_trusted(command):
            return False
        # A repository's own git config can run programs (core.fsmonitor, diff.external), so git
        # is only trusted inside the sandbox; other read-only commands are trusted anywhere.
        sandboxed = item.get("executionScope") == "workspace"
        if not sandboxed and command.strip().lower().startswith("git"):
            return False
        if not self.quiet:
            self.console.print("[dim]  ✓ read-only command: runs without asking[/]")
        return True


class JsonHost(TerminalHost):
    """`exec --json`: one JSON object per line on stdout; human-readable notes go to stderr."""

    def __init__(self, console, **options):
        super().__init__(console, quiet=True, **options)

    def emit(self, event, **fields):
        import json as _json

        sys.stdout.write(_json.dumps({"type": event, **fields}, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    async def started(self, item):
        self.emit("item.started", item=item)

    async def delta(self, item_id, kind, text):
        self.emit("item.delta", item_id=item_id, kind=kind, delta=text)

    async def completed(self, item):
        self.emit("item.completed", item=item)

    async def model_route(self, decision):
        self.emit("model.routed", model=decision.model, reason=decision.reason, confidence=decision.confidence)

    async def plan(self, steps):
        self.emit("plan.updated", steps=steps)

    async def hook_note(self, text):
        self.emit("note", text=text[:2000])

    async def compacted(self, reason, before, after, by):
        self.emit("context.compacted", reason=reason, tokens_before=before, tokens_after=after, summarizer=by)

    async def approve(self, item):
        if self.approve_all or (item["type"] == "fileChange" and self.approve_edits):
            decision = "accept"
        elif item["type"] != "fileChange" and self.auto_approved(item):
            decision = "accept"
        else:
            decision = "decline"  # Nobody can answer a prompt in JSON mode; use -a to pre-approve.
        self.emit("approval", item_id=item.get("id"), decision=decision)
        return decision


def _utf8_streams():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure") and (stream.encoding or "").lower().replace("-", "") != "utf8":
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except ValueError:
                pass


def _options(subcommand):
    parser = argparse.ArgumentParser(add_help=False)
    # On a subcommand, an option left out must not erase one given before it.
    unset = {"default": argparse.SUPPRESS} if subcommand else {}
    parser.add_argument(
        "-C", "--cd", metavar="DIR", help="workspace directory (default: current directory)", **unset
    )
    parser.add_argument(
        "-m",
        "--model",
        help=(
            "model id; default reads PANINI_CLI_MODEL, JEV_REV_CODE_MODEL, PANINI_AGENT_MODEL, "
            f"planner envs, then {DEFAULT_MODEL}; use auto for JEV cost routing"
        ),
        **unset,
    )
    parser.add_argument(
        "--reasoning-effort", choices=EFFORTS, help="reasoning effort for reasoning models", **unset
    )
    parser.add_argument(
        "-a",
        "--approval-mode",
        choices=list(MODES),
        help="read-only, ask (default), edits (apply file changes without asking) or auto",
        **unset,
    )
    parser.add_argument(
        "--sandbox",
        choices=["none", "native", "codex", "docker"],
        help="none is local; native is PANINI's experimental Windows sandbox; codex/docker are optional adapters",
        **unset,
    )
    parser.add_argument(
        "--approve-edits",
        action="store_true",
        help="apply file changes without asking (still asks for commands)",
        **unset,
    )
    parser.add_argument(
        "--approve-all",
        action="store_true",
        help="approve every change and command without asking; only for disposable workspaces",
        **unset,
    )
    parser.add_argument("-p", "--profile", help="settings profile from ~/.panini/config.toml", **unset)
    return parser


def build_parser():
    parser = argparse.ArgumentParser(
        prog="panini-cli",
        parents=[_options(False)],
        description="PANINI-CLI: a terminal/system agent that works in your workspace, with your approval.",
    )
    parser.add_argument("--version", action="version", version=f"panini-cli {VERSION}")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")
    run = commands.add_parser("exec", parents=[_options(True)], help="run one task and exit")
    run.add_argument("prompt", nargs="+", help="what to do; - reads the task from stdin")
    run.add_argument("-q", "--quiet", action="store_true", help="show only changes, commands and the answer")
    run.add_argument("--json", action="store_true", help="print JSON Lines events on stdout (logs go to stderr)")
    run.add_argument("--output-schema", metavar="FILE", help="JSON Schema the final answer must match")
    run.add_argument("--image", action="append", metavar="FILE", help="attach an image (repeatable)")
    usage_command = commands.add_parser("usage", help="cloud spend, local savings and routing outcomes")
    usage_command.add_argument("--days", type=int, default=7, help="look back this many days (default 7)")
    config_command = commands.add_parser("config", help="show or create ~/.panini/config.toml")
    config_command.add_argument("action", nargs="?", choices=["show", "init", "path"], default="show")
    resume = commands.add_parser(
        "resume", parents=[_options(True)], help="continue the last (or a given) conversation"
    )
    resume.add_argument("session", nargs="?", help="session id (default: the latest in this workspace)")
    commands.add_parser("sessions", parents=[_options(True)], help="list saved conversations")
    receipt_command = commands.add_parser("receipts", help="verify a session's durable tool receipts")
    receipt_command.add_argument("session", help="session id shown by panini-cli sessions")
    receipt_command.add_argument("--export", metavar="FILE", help="also write a readable Markdown report")
    hook = commands.add_parser("hooks", parents=[_options(True)], help="list hooks, or trust the project's")
    hook.add_argument(
        "--trust", action="store_true", help="trust this project's .panini/hooks.json as it is now"
    )
    server = commands.add_parser("app-server", help="serve PANINI's coding protocol on stdio")
    server.add_argument("--stdio", action="store_true", required=True)
    commands.add_parser("doctor", parents=[_options(True)], help="check configuration and model access")
    commands.add_parser("models", help="show the model router: local model, cloud tiers and Jev")
    route_command = commands.add_parser("route", help="dry-run the router: which model would this task get?")
    route_command.add_argument("prompt", nargs="+", help="the task to route")
    commands.add_parser("mcp-server", help="serve PANINI's router and agent as MCP tools on stdio")
    jev_command = commands.add_parser("jev", help="manage the Jev routing sidecar")
    jev_commands = jev_command.add_subparsers(dest="jev_command", required=True)
    jev_commands.add_parser("setup", help="install the sidecar into ~/.panini/jev (needs Node.js and npm)")
    jev_commands.add_parser("status", help="show whether Jev routing is ready")
    sandbox_command = commands.add_parser("sandbox", help="inspect PANINI's bundled command sandbox")
    sandbox_commands = sandbox_command.add_subparsers(dest="sandbox_command", required=True)
    sandbox_commands.add_parser("status", help="verify the native Windows boundary without an API key")
    auth = commands.add_parser("auth", help="manage the OpenAI API key securely")
    auth_commands = auth.add_subparsers(dest="auth_command", required=True)
    login = auth_commands.add_parser("login", help="save a key in Windows Credential Manager")
    login.add_argument("--key-stdin", action="store_true", help="read a key from stdin without echo")
    auth_commands.add_parser("status", help="show whether a key is configured (never prints it)")
    auth_commands.add_parser("logout", help="remove the saved key")
    return parser


def usage_command(args):
    from rich.console import Console
    from rich.table import Table

    from . import ledger

    console = Console(highlight=False)
    rows = ledger.read(time.time() - args.days * 86400)
    if not rows:
        console.print(f"[dim]No usage recorded in the last {args.days} days ({ledger.ledger_path()}).[/]")
        return 0
    data = ledger.summary(rows, reference_model=fallback_model())
    table = Table(title=f"PANINI CLI usage · last {args.days} days")
    for column in ("Model", "Calls", "Input tokens", "Output tokens", "Cost"):
        table.add_column(column, justify="left" if column == "Model" else "right")
    for model, entry in sorted(data["models"].items(), key=lambda kv: -kv[1]["cost"]):
        cost = "local · $0" if model.startswith("ollama/") else (
            f"${entry['cost']:.4f}" + (f" + {entry['unpriced']} unpriced" if entry["unpriced"] else "")
        )
        table.add_row(model, str(entry["calls"]), f"{entry['input']:,}", f"{entry['output']:,}", cost)
    console.print(table)
    console.print(f"Priced cloud spend: [bold]${data['cost_usd']:.4f}[/]")
    if data["local_tokens"]:
        saved = data["saved_usd"]
        estimate = f"≈ ${saved:.4f} at {fallback_model()} prices" if saved is not None else f"({fallback_model()} is unpriced)"
        console.print(f"Handled locally: {data['local_tokens']:,} tokens {estimate}")
    if data["turns"]:
        console.print(f"Turns: {data['turns']} · local→cloud escalations: {data['escalations']}")
    return 0


def config_command(args, data):
    from rich.console import Console

    console = Console(highlight=False)
    target = userconfig.path()
    if args.action == "path":
        console.print(str(target))
        return 0
    if args.action == "init":
        try:
            console.print(f"Created {userconfig.init()}")
        except FileExistsError as exc:
            console.print(f"[yellow]{exc}[/]")
            return 1
        return 0
    if not data:
        console.print(f"[dim]No config file at {target}. Create one with `panini-cli config init`.[/]")
    else:
        console.print(f"[dim]{target}[/]")
        for key, value in sorted(userconfig.effective(data, getattr(args, "profile", None)).items()):
            console.print(f"  {key} = {value!r}")
        profiles = ", ".join(data.get("profiles") or {}) or "none"
        console.print(f"[dim]Profiles: {profiles} · MCP servers: {', '.join(data.get('mcp_servers') or {}) or 'none'}[/]")
    return 0


def router_command(args, console):
    from rich.markup import escape

    from ..jev import client as jev_client
    from . import model_router, ollama

    if args.command == "jev":
        if args.jev_command == "setup":
            try:
                console.print(f"Jev sidecar installed in {jev_client.setup()}")
            except (RuntimeError, OSError, subprocess.CalledProcessError) as exc:
                console.print(f"[red]Jev setup failed:[/] {escape(str(exc))}")
                return 1
        ready, detail = jev_client.available()
        console.print(f"Jev: {'ready — ' if ready else 'not ready — '}{escape(detail)}")
        return 0 if ready else 1
    if args.command == "route":
        prompt = " ".join(args.prompt)
        options = routing_options()
        current = model_router.fallback_model()
        decision = model_router.route(prompt, current) if options else None
        if decision is None:
            console.print("[red]No models available.[/] Start Ollama or set OPENAI_API_KEY.")
            return 1
        console.print(f"{decision.model}  [dim]({escape(decision.reason)})[/]")
        return 0
    ready, detail = ollama.available()
    console.print(f"Local   {local_model()}  " + ("[green]ready[/]" if ready else f"[yellow]{escape(detail)}[/]"))
    console.print(f"        {'enabled' if local_enabled() else 'disabled (PANINI_CLI_LOCAL=off)'} · Ollama {ollama.config.ollama_host()}")
    turns, failures = model_router.local_track_record()
    if turns:
        state = "in use" if model_router.local_is_earning_its_keep() else "paused (too many failures; it comes back as they age out)"
        console.print(f"        last {model_router.LEARN_WINDOW_DAYS} days: {turns} local turns, {failures} escalated or failed — {state}")
    for option in model_options():
        console.print(f"{option.tier.capitalize():<7} {option.id}  [dim]{escape(option.description)}[/]")
    console.print("Cloud   " + ("OPENAI_API_KEY configured" if configured_key() else "[yellow]no OPENAI_API_KEY[/]"))
    ok, detail = jev_client.available()
    console.print("Jev     " + ("ready" if ok else f"off ({escape(detail)}) — offline estimate routes instead"))
    return 0


def money(value):
    return "unpriced" if value is None else f"${value:.4f}"


def report(console, result, agent=None):
    tokens = result.usage.get("total_tokens", 0)
    spent = f" · {money(result.cost_usd)}" if result.usage.get("total_tokens") else ""
    session = ""
    if agent is not None:
        session = f" · session {money(agent.cost_usd)}" + (" + unpriced" if agent.unpriced_calls else "")
        window = agent.settings.context_tokens
        used = min(100, agent.last_input_tokens * 100 // window) if window else 0
        session += f" · context {used}%"
    if result.status == "completed":
        console.print(
            f"[dim]✓ done · {result.steps} step{'s' if result.steps != 1 else ''} · {tokens:,} tokens{spent}{session}[/]"
        )
    else:
        from rich.markup import escape

        console.print(f"[red]{escape(result.error or result.status)}[/]")


async def start_mcp(agent, console):
    if agent.mcp is None:
        return
    await agent.mcp.start()
    agent.extra_tools = agent.mcp.tool_defs()
    for name, error in agent.mcp.errors.items():
        console.print(f"[yellow]MCP server {name} did not start:[/] {error}")
    if agent.extra_tools:
        console.print(f"[dim]MCP: {len(agent.extra_tools)} tools from {', '.join(s for s in agent.mcp.servers if s not in agent.mcp.errors)}[/]")


def interactive(agent, settings, console, host, approvals):
    print_welcome(console, settings, agent.workspace.root, approvals)
    with asyncio.Runner() as runner:
        try:
            runner.run(start_mcp(agent, console))
            return _session(agent, settings, console, host, runner)
        finally:
            runner.run(agent.close())


def _status(agent, host):
    window = agent.settings.context_tokens
    used = min(100, agent.last_input_tokens * 100 // window) if window else 0
    model = getattr(agent.model, "model", "?")
    return f"{model} · {host.mode} · ${agent.cost_usd:.4f} · context {used}%"


def _session(agent, settings, console, host, runner):
    from . import ledger, ui

    prompt = ui.make_input(console, agent.workspace.root, ledger.data_dir() / "cli-history.txt", can_ask())
    pending_images = []
    while True:
        try:
            text = prompt.ask(_status(agent, host))
        except (EOFError, KeyboardInterrupt):
            console.print()
            return 0
        command = text.strip()
        if command.startswith("/image"):
            path = command[len("/image"):].strip().strip('"')
            if path:
                pending_images.append(path)
                console.print(f"[dim]Attached {host.escape(path)}; it goes with your next message.[/]")
            else:
                console.print("[dim]Usage: /image PATH[/]")
            continue
        if not command:
            continue
        if command in ("/exit", "/quit"):
            return 0
        if command == "/help":
            console.print(f"[dim]{HELP}\n{LOCAL_HELP}[/]")
            continue
        if _local_session_command(command, agent, settings, console, host):
            continue
        if command == "/undo":
            restored, skipped = agent.undo()
            if not restored and not skipped:
                console.print("[dim]Nothing to undo.[/]")
            for label in restored:
                console.print(f"[green]  ↶ restored {host.escape(label)}[/]")
            for label in skipped:
                console.print(f"[yellow]  ✗ {host.escape(label)} changed since; left as is[/]")
            if restored:
                console.print("[dim]  Effects of commands that already ran are not undone.[/]")
            continue
        if command.startswith("/approvals"):
            mode = command[len("/approvals") :].strip()
            if mode in MODES:
                host.set_mode(mode)
            elif mode:
                console.print(f"[yellow]Modes: {', '.join(MODES)}[/]")
            console.print(f"[dim]Approvals: {MODES[host.mode]}[/]")
            continue
        if command == "/power":
            host.set_mode("auto")
            console.print("[cyan]Power mode enabled:[/] PANINI will auto-approve file changes and commands in this session.")
            continue
        if command == "/plan":
            host.plan_now(agent.plan)
            continue
        if command == "/diff":
            text = _workspace_diff(agent)
            if not text.strip():
                console.print("[dim]No changes in this session (or not a git repository).[/]")
            else:
                host._diff(text)
            continue
        if command == "/review":
            text = _workspace_diff(agent)
            if not text.strip():
                console.print("[dim]Nothing to review.[/]")
                continue
            previous = host.mode
            host.set_mode("read-only")
            try:
                lead = "Use strong. " if model_router_enabled(settings.model) else ""
                result = runner.run(agent.turn(lead + REVIEW_PROMPT + "\n\n```diff\n" + text[:60_000] + "\n```"))
                report(console, result, agent)
            except KeyboardInterrupt:
                console.print("[yellow]Review stopped.[/]")
            finally:
                host.set_mode(previous)
            continue
        if command == "/commit":
            _commit(agent, console, runner)
            continue
        if command == "/compact":
            try:
                if not runner.run(agent.compact("manual")):
                    console.print("[dim]Nothing to compact yet.[/]")
            except ModelError as exc:
                console.print(f"[red]Compaction failed:[/] {host.escape(str(exc))}")
            continue
        if command == "/new":
            agent.reset()
            agent.session_id = sessions.new_id()
            console.print("[dim]Started a new conversation.[/]")
            continue
        if command.startswith("/model"):
            name = command[len("/model") :].strip()
            if name:
                settings.model = name
                agent.model = create_model(settings)
                agent.new_model_state()
            console.print(f"[dim]Model: {settings.model}[/]")
            continue
        try:
            images, pending_images = pending_images, []
            message = ui.expand_mentions(text, agent.workspace.root)
            result = runner.run(agent.turn(message, images=images))
        except KeyboardInterrupt:
            host.end_line()
            console.print("[yellow]Stopped. Changes already applied and commands already run remain.[/]")
            continue
        except (OSError, ValueError) as exc:
            console.print(f"[red]{host.escape(str(exc))}[/]")
            continue
        report(console, result, agent)
        remember(agent, text)


REVIEW_PROMPT = (
    "Review the following changes as a careful senior engineer. Read surrounding code where needed. "
    "Report only real problems: bugs, missed edge cases, security issues, broken tests, and unclear code, "
    "each with path:line, severity (high/medium/low) and a concrete fix. Do not modify files. "
    "If the changes look correct, say so briefly."
)


def _workspace_diff(agent):
    from . import gitops

    try:
        if agent.session_base:
            return agent.session_diff()
        root = gitops.repo_root(agent.workspace.root)
        if root is None:
            return ""
        return gitops._git(root, "diff", "--no-ext-diff", "HEAD", "--", ".")
    except (gitops.GitError, OSError) as exc:
        return f"(git diff failed: {exc})"


def _commit(agent, console, runner):
    import subprocess

    from . import compaction, gitops

    root = gitops.repo_root(agent.workspace.root)
    if root is None:
        console.print("[yellow]Not a git repository.[/]")
        return
    status = gitops._git(root, "status", "--porcelain")
    if not status.strip():
        console.print("[dim]Nothing to commit.[/]")
        return
    patch_text = gitops._git(root, "diff", "--no-ext-diff", "HEAD") + "\n" + status
    try:
        message, by = runner.run(compaction.oneshot(compaction.COMMIT_INSTRUCTIONS, patch_text[:40_000], agent.model))
    except ModelError as exc:
        console.print(f"[red]Could not draft a message:[/] {exc}")
        return
    message = message.strip().strip("`").strip()
    console.print(f"[dim]Drafted by {by}:[/]\n{message}\n")
    console.print("[dim]Files:[/]\n" + status)
    try:
        answer = console.input("Stage everything above and commit with this message? [y/N] ").strip().lower()
    except EOFError:
        answer = ""
    if answer not in ("y", "yes"):
        console.print("[dim]Not committed.[/]")
        return
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=False)
    done = subprocess.run(["git", "-C", str(root), "commit", "-F", "-"], input=message, text=True, capture_output=True)
    console.print(done.stdout.strip() or done.stderr.strip())


def _local_session_command(command, agent, settings, console, host):
    normalized = command[1:].strip() if command.startswith("/") else command
    lowered = normalized.lower()
    if lowered == "doctor":
        args = argparse.Namespace(cd=str(agent.workspace.root), model=settings.model, sandbox=settings.sandbox)
        doctor(args, console)
        return True
    if lowered == "sandbox status":
        ready, reason = native_sandbox.available()
        if ready:
            ready, reason = native_probe.verify_boundary()
        console.print("Engine: PANINI-owned native Windows AppContainer; bundled in PANINI-CLI.exe")
        console.print(f"Boundary: {'verified' if ready else 'unavailable'} — {reason}")
        console.print("Docker Desktop and Codex CLI are not required for this engine.")
        return True
    if lowered == "sessions":
        rows = sessions.listing(agent.workspace.root)
        for row in rows:
            stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(row["updated"]))
            console.print(f"[cyan]{row['id']}[/]  [dim]{stamp}[/]  {host.escape(row['title'] or '')}")
        if not rows:
            console.print("[dim]No saved conversations for this workspace.[/]")
        return True
    if lowered.startswith("receipts "):
        session_id = normalized.split(maxsplit=1)[1].strip()
        _verify_receipts(console, session_id)
        return True
    if lowered in {"auth status", "auth login", "auth logout"}:
        _interactive_auth(console, lowered)
        return True
    if lowered.startswith("auth "):
        console.print("[yellow]Auth commands: auth login · auth status · auth logout[/]")
        return True
    return False


def _interactive_auth(console, command):
    from .auth import delete_key, read_key, save_key

    try:
        if command == "auth status":
            source = (
                "configured"
                if configured_key()
                else ("Windows Credential Manager" if read_key() else "missing")
            )
            console.print(f"OpenAI API key: {source}")
            return
        if command == "auth logout":
            removed = delete_key()
            os.environ.pop("OPENAI_API_KEY", None)
            console.print("Saved API key removed." if removed else "No saved API key found.")
            return
        if not can_ask():
            console.print("[red]Use an interactive terminal for auth login, or run auth login --key-stdin.[/]")
            return
        from getpass import getpass

        key = getpass("OpenAI API key (hidden): ")
        ok, message = _validate_login_key(key)
        if not ok:
            console.print(f"[red]{message}[/]")
            return
        save_key(key)
        os.environ["OPENAI_API_KEY"] = key.strip()
        console.print(message)
    except (OSError, UnicodeError, ValueError) as exc:
        console.print(f"[red]Credential error:[/] {exc}")


def _validate_login_key(key):
    async def check():
        import openai
        from openai import AsyncOpenAI

        try:
            await AsyncOpenAI(api_key=key.strip(), timeout=20, max_retries=1).models.list()
        except openai.AuthenticationError:
            return False, "OpenAI rejected this API key. The saved credential was not changed."
        except openai.PermissionDeniedError:
            return False, "OpenAI says this key is not allowed to list models. The saved credential was not changed."
        except openai.RateLimitError:
            return True, "API key saved, but quota/rate limit prevented live verification."
        except (openai.APIConnectionError, openai.APITimeoutError):
            return True, "API key saved, but PANINI-CLI could not reach OpenAI to verify it right now."
        except openai.APIStatusError as exc:
            return True, f"API key saved, but OpenAI verification returned HTTP {exc.status_code}."
        return True, "API key verified and saved in Windows Credential Manager."

    return asyncio.run(check())


def _verify_receipts(console, session_id):
    try:
        path = receipts.path_for(session_id)
        if not path.is_file():
            console.print(f"[red]No receipts for session {session_id}.[/]")
            return
        count, head = receipts.verify(path)
    except (ValueError, OSError) as exc:
        console.print(f"[red]Receipt verification failed:[/] {exc}")
        return
    console.print(f"Verified {count} receipt events for {session_id}. Chain head: {head}")


def doctor(args, console):
    try:
        settings = Settings.from_env(model=getattr(args, "model", None))
    except ValueError as exc:
        console.print(f"[red]Configuration error:[/] {exc}")
        return 2
    if getattr(args, "sandbox", None):
        settings.sandbox = args.sandbox
    shell = shell_name()
    executable = shutil.which("pwsh") or shutil.which("powershell") or shutil.which("bash") or ""
    console.print(f"panini-cli {VERSION} · Python {sys.version.split()[0]}")
    console.print(f"Workspace: {Path(getattr(args, 'cd', None) or '.').resolve()}")
    console.print(f"Shell: {shell} {executable}")
    console.print(f"Model: {settings.model}")
    if model_router_enabled(settings.model):
        options = ", ".join(option.id for option in model_options())
        console.print(f"Auto router: enabled; fallback {fallback_model()}; candidates {options}")
    if settings.sandbox == "docker":
        ready, reason = sandbox.available()
        console.print(f"Command mode: Docker sandbox — {'available' if ready else reason}")
    elif settings.sandbox == "codex":
        ready, reason = codex_sandbox.available(Path(getattr(args, "cd", None) or ".").resolve())
        console.print(f"Command mode: Codex native Windows sandbox — {'available' if ready else reason}")
    elif settings.sandbox == "native":
        ready, reason = native_sandbox.available()
        if ready:
            ready, reason = native_probe.verify_boundary()
        console.print(f"Command mode: PANINI native Windows sandbox (experimental) — {reason}")
        if not ready:
            return 2
    else:
        console.print(
            "Command mode: local approved commands (no filesystem/network isolation; Docker not required)"
        )
    local_ok = False
    if local_enabled():
        from . import ollama

        local_ok, detail = ollama.available()
        console.print(f"Local model: {local_model()} — " + ("ready" if local_ok else f"[yellow]{detail}[/]"))
    from ..jev import client as jev_client

    jev_ok, detail = jev_client.available()
    console.print("Jev router: " + ("ready" if jev_ok else f"off ({detail}); routing uses the offline estimate"))
    if not configured_key():
        if local_ok:
            console.print("OPENAI_API_KEY: missing — local-only mode; hard tasks need a key")
            return 0
        console.print(
            "[red]OPENAI_API_KEY: missing[/] — run [bold]panini-cli auth login[/bold] or set OPENAI_API_KEY."
        )
        return 1
    console.print("OPENAI_API_KEY: configured")

    async def check_key():
        from openai import AsyncOpenAI

        await AsyncOpenAI(timeout=20, max_retries=1).models.list()

    async def check_model():
        from openai import AsyncOpenAI

        await AsyncOpenAI(timeout=20, max_retries=1).models.retrieve(actual_model(settings.model))

    try:
        asyncio.run(check_key())
    except Exception as exc:  # noqa: BLE001 -- Report any provider failure plainly.
        detail = str(exc)[:200]
        if "incorrect api key" in detail.lower() or "401" in detail:
            detail = "OpenAI rejected the saved key. Replace it with `panini-cli auth login`."
        console.print(f"[red]API key validation: failed[/] — {detail}")
        return 1
    console.print("API key validation: ok")

    try:
        asyncio.run(check_model())
    except Exception as exc:  # noqa: BLE001 -- Report provider/model access separately from auth.
        detail = str(exc)[:200]
        if "404" in detail or "not found" in detail.lower():
            detail = (
                f"Model {actual_model(settings.model)} is not available to this API key. "
                "Use /model NAME or PANINI_CLI_MODEL."
            )
        elif "403" in detail or "permission" in detail.lower():
            detail = (
                f"This API key is not allowed to use {actual_model(settings.model)}. "
                "Use /model NAME or another key."
            )
        console.print(f"[red]Model access: failed[/] — {detail}")
        return 1
    console.print("Model access: ok")
    return 0


def remember(agent, text):
    """Save the conversation after each turn; a failure to save never loses the answer."""
    if not agent.items:
        return
    title = next((text_of(i["content"]) for i in agent.items if i.get("role") == "user"), text)
    try:
        sessions.save(agent.session_id, agent.workspace.root, agent.items, str(title))
    except OSError:
        pass


def main(argv=None):
    _utf8_streams()
    args = build_parser().parse_args(argv)
    if args.command not in ("sandbox", "auth", "receipts") or (args.command == "auth" and args.auth_command == "status"):
        try:
            load_panini_env()
        except (OSError, UnicodeError) as exc:
            from rich.console import Console

            Console(highlight=False).print(f"[red]API key setup failed:[/] {exc}")
            return 2
    try:
        user_settings = userconfig.apply(getattr(args, "profile", None))
    except ValueError as exc:
        from rich.console import Console

        Console(highlight=False).print(f"[red]Config error:[/] {exc}")
        return 2
    if args.command == "config":
        return config_command(args, user_settings)
    if args.command == "usage":
        return usage_command(args)
    if args.command == "mcp-server":
        from .mcp import serve_stdio

        asyncio.run(serve_stdio())
        return 0
    if args.command == "app-server":
        from .server import serve

        serve()
        return 0
    from rich.console import Console

    console = Console(highlight=False)
    if args.command == "receipts":
        path = receipts.path_for(args.session)
        if not path.is_file():
            console.print(f"[red]No receipts for session {args.session}.[/]")
            return 1
        try:
            count, head = receipts.verify(path)
        except (ValueError, OSError) as exc:
            console.print(f"[red]Receipt verification failed:[/] {exc}")
            return 2
        console.print(f"Verified {count} receipt events for {args.session}. Chain head: {head}")
        if getattr(args, "export", None):
            Path(args.export).write_text(receipts.report(path, args.session, head), encoding="utf-8")
            console.print(f"Report written to {args.export}")
        return 0
    if args.command == "sandbox":
        ready, reason = native_sandbox.available()
        if ready:
            ready, reason = native_probe.verify_boundary()
        console.print("Engine: PANINI-owned native Windows AppContainer; bundled in PANINI-CLI.exe")
        console.print(f"Boundary: {'verified' if ready else 'unavailable'} — {reason}")
        console.print("Docker Desktop and Codex CLI are not required for this engine.")
        return 0 if ready else 2
    if args.command == "auth":
        from .auth import delete_key, read_key, save_key

        try:
            if args.auth_command == "login":
                if args.key_stdin:
                    key = sys.stdin.readline().strip()
                elif sys.stdin.isatty():
                    from getpass import getpass

                    key = getpass("OpenAI API key (hidden): ")
                else:
                    console.print("[red]Use an interactive terminal or auth login --key-stdin.[/]")
                    return 2
                ok, message = _validate_login_key(key)
                if not ok:
                    console.print(f"[red]{message}[/]")
                    return 1
                save_key(key)
                os.environ["OPENAI_API_KEY"] = key.strip()
                console.print(message)
                return 0
            if args.auth_command == "logout":
                removed = delete_key()
                console.print("Saved API key removed." if removed else "No saved API key found.")
                return 0
            source = (
                "configured"
                if configured_key()
                else ("Windows Credential Manager" if read_key() else "missing")
            )
            console.print(f"OpenAI API key: {source}")
            return 0 if source != "missing" else 1
        except (OSError, UnicodeError, ValueError) as exc:
            console.print(f"[red]Credential error:[/] {exc}")
            return 2
    if args.command == "doctor":
        return doctor(args, console)
    if args.command in ("models", "route", "jev"):
        return router_command(args, console)
    workspace = Path(getattr(args, "cd", None) or ".").resolve()
    if not workspace.is_dir():
        console.print(f"[red]{workspace} is not a directory.[/]")
        return 2
    if not configured_key() and not os.getenv("PANINI_CLI_SCRIPTED_MODEL") and not local_only_ready():
        if os.name == "nt" and sys.stdin.isatty():
            try:
                answer = console.input("OpenAI API key is missing. Set it up now? [Y/n] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                answer = "n"
            if answer in ("", "y", "yes"):
                from getpass import getpass

                from .auth import save_key

                try:
                    key = getpass("OpenAI API key (hidden): ")
                    ok, message = _validate_login_key(key)
                    if not ok:
                        console.print(f"[red]{message}[/]")
                        return 2
                    save_key(key)
                    os.environ["OPENAI_API_KEY"] = key.strip()
                    console.print(message)
                except (OSError, ValueError) as exc:
                    console.print(f"[red]Credential setup failed:[/] {exc}")
                    return 2
        if not configured_key():
            console.print(
                "[red]OpenAI API key is not configured.[/] Run [bold]panini-cli auth login[/bold] (or set OPENAI_API_KEY)."
            )
            return 2
    try:
        settings = Settings.from_env(
            model=getattr(args, "model", None), reasoning_effort=getattr(args, "reasoning_effort", None)
        )
    except ValueError as exc:
        console.print(f"[red]Configuration error:[/] {exc}")
        return 2
    if getattr(args, "sandbox", None):
        settings.sandbox = args.sandbox
    if settings.sandbox == "docker":
        ready, reason = sandbox.available()
        if ready:
            console.print(f"[dim]Preparing the sandbox image {sandbox.image()}…[/]")
            ready, reason = sandbox.prepare_image()
        if not ready:
            console.print(f"[red]Sandbox unavailable:[/] {reason}")
            return 2
    elif settings.sandbox == "codex":
        ready, reason = codex_sandbox.available(workspace)
        if not ready:
            console.print(f"[red]Native sandbox unavailable:[/] {reason}")
            return 2
    elif settings.sandbox == "native":
        ready, reason = native_sandbox.available()
        if ready:
            ready, reason = native_probe.verify_boundary()
        if not ready:
            console.print(f"[red]PANINI native sandbox unavailable:[/] {reason}")
            return 2
    approve_all, approve_edits = getattr(args, "approve_all", False), getattr(args, "approve_edits", False)
    if getattr(args, "json", False):
        from rich.console import Console

        console = Console(highlight=False, stderr=True)
        host = JsonHost(console, approve_edits=approve_edits, approve_all=approve_all)
    else:
        host = TerminalHost(
            console, approve_edits=approve_edits, approve_all=approve_all, quiet=getattr(args, "quiet", False)
        )
    mode = getattr(args, "approval_mode", None) or default_approval_mode()
    if mode:
        host.set_mode(mode)
    if args.command == "hooks":
        configured = hooks.Hooks(workspace)
        if getattr(args, "trust", False):
            if not configured.project_file.is_file():
                console.print("[dim]This project has no .panini/hooks.json.[/]")
                return 0
            console.print(host.escape(configured.project_file.read_text(encoding="utf-8")))
            hooks.trust(configured.project_file)
            console.print("[green]Trusted this exact hooks file. Any change to it needs trusting again.[/]")
            return 0
        for entry in configured.describe():
            console.print(
                f"[cyan]{entry['event']}[/] [dim]{entry['origin']}[/] "
                f"{host.escape(entry['matcher'] or '*')} → {host.escape(entry['command'])}"
            )
        if configured.untrusted:
            console.print(
                "[yellow]The project's .panini/hooks.json is not trusted, so it does not run. "
                "Read it, then run `panini-cli hooks --trust`.[/]"
            )
        elif not configured.describe():
            console.print("[dim]No hooks configured.[/]")
        return 0
    if args.command == "sessions":
        rows = sessions.listing(workspace)
        for row in rows:
            stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(row["updated"]))
            console.print(f"[cyan]{row['id']}[/]  [dim]{stamp}[/]  {host.escape(row['title'] or '')}")
        if not rows:
            console.print("[dim]No saved conversations for this workspace.[/]")
        return 0
    configured = hooks.Hooks(workspace)
    if configured.untrusted:
        console.print(
            "[yellow]This project has hooks in .panini/hooks.json that you have not trusted; "
            "they will not run. Review them with `panini-cli hooks`.[/]"
        )
    agent = Agent(workspace, create_model(settings), settings, host, hooks=configured)
    agent.session_id = sessions.new_id()
    servers = userconfig.section("mcp_servers")
    if servers:
        from .mcp import McpManager

        agent.mcp = McpManager(servers)
    if args.command == "resume":
        try:
            if args.session:
                record = sessions.load(args.session)
            else:
                latest = sessions.listing(workspace)
                if not latest:
                    console.print("[yellow]No saved conversation for this workspace.[/]")
                    return 1
                record = sessions.load(latest[0]["id"])
        except KeyError as exc:
            console.print(f"[red]{host.escape(str(exc).strip(chr(39)))}[/]")
            return 1
        if Path(record["workspace"]) != workspace:
            console.print(
                f"[yellow]That conversation belongs to {host.escape(record['workspace'])}; use -C.[/]"
            )
            return 1
        agent.items, agent.session_id = record["items"], record["id"]
        console.print(f"[dim]Resumed: {host.escape(record['title'])}[/]")
    if args.command == "exec":
        prompt = " ".join(args.prompt)
        if prompt.strip() == "-":
            prompt = sys.stdin.read()
        if not prompt.strip():
            console.print("[red]The task is empty.[/]")
            return 2
        schema = None
        if getattr(args, "output_schema", None):
            import json as _json

            try:
                schema = _json.loads(Path(args.output_schema).read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                console.print(f"[red]Cannot read the output schema:[/] {exc}")
                return 2
        try:

            async def once():
                try:
                    await start_mcp(agent, console)
                    return await agent.turn(prompt, schema, images=getattr(args, "image", None) or ())
                finally:
                    await agent.close()

            result = asyncio.run(once())
        except KeyboardInterrupt:
            host.end_line()
            console.print("[yellow]Stopped.[/]")
            return 130
        except (OSError, ValueError) as exc:
            console.print(f"[red]{host.escape(str(exc))}[/]")
            return 2
        if isinstance(host, JsonHost):
            host.emit(
                "turn.completed",
                status=result.status,
                text=result.text,
                error=result.error,
                usage=result.usage,
                steps=result.steps,
                cost_usd=getattr(result, "cost_usd", None),
                session_id=agent.session_id,
            )
        else:
            report(console, result, agent)
        remember(agent, prompt)
        # 0 done, 1 failed, 3 stopped at the step limit, 4 stopped by the budget, 130 interrupted.
        return {"completed": 0, "incomplete": 3, "budget": 4, "interrupted": 130}.get(result.status, 1)
    approvals = MODES[host.mode] + (
        " · commands in a Docker sandbox"
        if settings.sandbox == "docker"
        else " · commands in the Codex native Windows sandbox"
        if settings.sandbox == "codex"
        else " · PANINI native Windows sandbox (experimental; no interactive terminals)"
        if settings.sandbox == "native"
        else " · local commands are not filesystem/network-isolated"
    )
    return interactive(agent, settings, console, host, approvals)
