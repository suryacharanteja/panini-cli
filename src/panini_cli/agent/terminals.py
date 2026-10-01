"""Interactive terminal sessions: long-running processes the agent can talk to.

Used for dev servers, REPLs, watch builds and programs that ask for input. A session
is a process tree (in a kill-on-close job on Windows) with piped input and output; it
is not a pseudo-terminal, so full-screen programs and hidden password prompts do not
work. Starting a session and sending input each need the person's approval, like
commands. At most four sessions run at once; all end when the agent closes.
"""

import asyncio
import codecs
import os
import signal
import subprocess
import time
from dataclasses import dataclass, field

from . import codex_sandbox, sandbox
from .shell import _WindowsJob, kill_windows_tree, safe_environment, shell_argv

MAX_SESSIONS = 4
BUFFER = 200_000


@dataclass
class Terminal:
    id: str
    command: str
    process: asyncio.subprocess.Process
    job: object = None
    container: str | None = None
    scope: str = "workspace"
    started: float = field(default_factory=time.monotonic)
    buffer: str = ""
    read_to: int = 0  # Characters of buffer already returned to the agent.
    dropped: int = 0
    reader: asyncio.Task | None = None
    changed: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def running(self):
        return self.process.returncode is None


class Terminals:
    def __init__(self, workspace, settings):
        self.workspace, self.settings = workspace, settings
        self.sessions = {}
        self.counter = 0

    def list(self):
        return [
            {
                "id": t.id,
                "command": t.command[:200],
                "running": t.running,
                "exit_code": t.process.returncode,
                "seconds": round(time.monotonic() - t.started),
            }
            for t in self.sessions.values()
        ]

    async def start(self, command, *, scope="workspace"):
        if scope not in ("workspace", "host"):
            raise ValueError("scope must be workspace or host")
        mode = "none" if scope == "host" else self.settings.sandbox
        effective_scope = "host" if mode == "none" else "workspace"
        if mode == "native":
            raise OSError(
                "Interactive terminals need scope=host with the native sandbox."
            )
        live = [t for t in self.sessions.values() if t.running]
        if len(live) >= MAX_SESSIONS:
            raise OSError(f"At most {MAX_SESSIONS} sessions can run at once; stop one first.")
        windows = os.name == "nt"
        container = None
        argv, environ = shell_argv(command), None
        if mode == "docker":
            container = sandbox.new_name()
            argv = sandbox.argv(command, self.workspace, name=container)
            argv.insert(2, "-i")  # Keep the container's stdin open.
            environ = sandbox.environment()
        elif mode == "codex":
            argv = codex_sandbox.argv(command, self.workspace)
        flags = 0
        if windows:
            flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000004
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=self.workspace,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=safe_environment(environ),
            creationflags=flags,
            start_new_session=not windows,
        )
        job = None
        if windows:
            try:
                job = _WindowsJob(process.pid)
            except BaseException:
                process.kill()
                await process.wait()
                raise
        self.counter += 1
        terminal = Terminal(f"t{self.counter}", command, process, job, container, effective_scope)
        terminal.reader = asyncio.create_task(self._pump(terminal))
        self.sessions[terminal.id] = terminal
        return terminal

    async def _pump(self, terminal):
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while chunk := await terminal.process.stdout.read(65536):
            terminal.buffer += decoder.decode(chunk)
            if len(terminal.buffer) > BUFFER:
                cut = len(terminal.buffer) - BUFFER
                terminal.buffer = terminal.buffer[cut:]
                terminal.dropped += cut
                terminal.read_to = max(0, terminal.read_to - cut)
            terminal.changed.set()
        await terminal.process.wait()
        terminal.changed.set()

    def get(self, session_id):
        terminal = self.sessions.get(session_id)
        if not terminal:
            raise KeyError(f"No terminal session {session_id}.")
        return terminal

    async def read(self, session_id, wait_seconds=2.0, limit=16_000):
        """New output since the last read, after waiting briefly for it to settle."""
        terminal = self.get(session_id)
        deadline = time.monotonic() + max(0.0, min(float(wait_seconds), 30.0))
        quiet_since = time.monotonic()
        seen = len(terminal.buffer)
        while time.monotonic() < deadline and terminal.running:
            terminal.changed.clear()
            try:
                await asyncio.wait_for(terminal.changed.wait(), 0.3)
            except TimeoutError:
                pass
            if len(terminal.buffer) != seen:
                seen, quiet_since = len(terminal.buffer), time.monotonic()
            elif len(terminal.buffer) > terminal.read_to and time.monotonic() - quiet_since > 0.6:
                break  # Output arrived and has gone quiet.
        text = terminal.buffer[terminal.read_to :]
        terminal.read_to = len(terminal.buffer)
        if len(text) > limit:
            text = f"…[{len(text) - limit:,} earlier characters omitted]…\n" + text[-limit:]
        return text

    async def send(self, session_id, text, *, newline=True):
        terminal = self.get(session_id)
        if not terminal.running:
            raise OSError(f"Session {session_id} has ended (exit code {terminal.process.returncode}).")
        data = text + ("\n" if newline and not text.endswith("\n") else "")
        terminal.process.stdin.write(data.encode("utf-8"))
        await terminal.process.stdin.drain()

    async def stop(self, session_id):
        terminal = self.get(session_id)
        if terminal.running:
            if os.name == "nt":
                await asyncio.to_thread(kill_windows_tree, terminal.process.pid, terminal.job)
            else:
                try:
                    os.killpg(terminal.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            try:
                await asyncio.wait_for(terminal.process.wait(), 10)
            except TimeoutError:
                pass
        if terminal.job:
            terminal.job.close()
            terminal.job = None
        if terminal.reader:
            try:
                await asyncio.wait_for(terminal.reader, 2)
            except (TimeoutError, asyncio.CancelledError):
                terminal.reader.cancel()
        if terminal.container:
            await asyncio.to_thread(sandbox.remove, terminal.container)
        return terminal.process.returncode

    async def close(self):
        for session_id in list(self.sessions):
            try:
                await self.stop(session_id)
            except Exception:  # noqa: BLE001 -- Best effort at shutdown.
                pass
        self.sessions.clear()
