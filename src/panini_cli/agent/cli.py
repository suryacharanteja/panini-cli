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

from . import VERSION, codex_sandbox, hooks, native_probe, native_sandbox, receipts, sandbox, sessions
from .config import DEFAULT_MODEL, EFFORTS, Settings, configured_key, load_panini_env, local_enabled, local_model
from .loop import Agent
from .model import create_model
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
HELP = "/new starts a new conversation · /plan shows the plan · /undo reverts the last changes · /power enables fast UAT auto-approval · /approvals MODE · /model NAME switches model · /exit quits · Ctrl+C stops the current task"
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
        if self.read_only:
            self.console.print("[yellow]  Read-only mode: not applied. Switch with /approvals ask.[/]")
            return "decline"
        if self.approve_all or (change and self.approve_edits):
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
                "  Type y + Enter to apply this file change, or n to decline [y/N] " if change else
                "  Run with Windows host access? Type y + Enter, or n to decline [y/N] " if scope == "host" else
                "  Run in the workspace sandbox? Type y + Enter, or n to decline [y/N] "
            )
        except EOFError:
            return "decline"
        return "accept" if answer.strip().lower() in ("y", "yes") else "decline"


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
    run.add_argument("prompt", nargs="+", help="what to do")
    run.add_argument("-q", "--quiet", action="store_true", help="show only changes, commands and the answer")
    resume = commands.add_parser(
        "resume", parents=[_options(True)], help="continue the last (or a given) conversation"
    )
    resume.add_argument("session", nargs="?", help="session id (default: the latest in this workspace)")
    commands.add_parser("sessions", parents=[_options(True)], help="list saved conversations")
    receipt_command = commands.add_parser("receipts", help="verify a session's durable tool receipts")
    receipt_command.add_argument("session", help="session id shown by panini-cli sessions")
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
    for option in model_options():
        console.print(f"{option.tier.capitalize():<7} {option.id}  [dim]{escape(option.description)}[/]")
    console.print("Cloud   " + ("OPENAI_API_KEY configured" if configured_key() else "[yellow]no OPENAI_API_KEY[/]"))
    ok, detail = jev_client.available()
    console.print("Jev     " + ("ready" if ok else f"off ({escape(detail)}) — offline estimate routes instead"))
    return 0


def report(console, result):
    tokens = result.usage.get("total_tokens", 0)
    if result.status == "completed":
        console.print(
            f"[dim]✓ done · {result.steps} step{'s' if result.steps != 1 else ''} · {tokens:,} tokens[/]"
        )
    else:
        from rich.markup import escape

        console.print(f"[red]{escape(result.error or result.status)}[/]")


def interactive(agent, settings, console, host, approvals):
    print_welcome(console, settings, agent.workspace.root, approvals)
    with asyncio.Runner() as runner:
        try:
            return _session(agent, settings, console, host, runner)
        finally:
            runner.run(agent.close())


def _session(agent, settings, console, host, runner):
    while True:
        try:
            text = console.input("\n[bold cyan]›[/] ")
        except (EOFError, KeyboardInterrupt):
            console.print()
            return 0
        command = text.strip()
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
            result = runner.run(agent.turn(text))
        except KeyboardInterrupt:
            host.end_line()
            console.print("[yellow]Stopped. Changes already applied and commands already run remain.[/]")
            continue
        report(console, result)
        remember(agent, text)


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
    title = next((i["content"] for i in agent.items if i.get("role") == "user"), text)
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
        try:

            async def once():
                try:
                    return await agent.turn(" ".join(args.prompt))
                finally:
                    await agent.close()

            result = asyncio.run(once())
        except KeyboardInterrupt:
            host.end_line()
            console.print("[yellow]Stopped.[/]")
            return 130
        report(console, result)
        remember(agent, " ".join(args.prompt))
        return 0 if result.status == "completed" else 1
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
