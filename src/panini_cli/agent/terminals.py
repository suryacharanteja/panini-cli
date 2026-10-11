"""Interactive terminal sessions: long-running processes the agent can talk to.

Used for dev servers, REPLs, watch builds and programs that ask for input. A session
is a process tree (in a kill-on-close job on Windows) with piped input and output; it
can optionally use ConPTY (Windows) or a PTY (Unix). Sessions retain their launch
policy and approval scope. At most four sessions
run at once; all end when the agent closes or their lifetime expires.
"""

import asyncio
import codecs
import os
import signal
import subprocess
import time
from pathlib import Path
from dataclasses import dataclass, field

from . import codex_sandbox, sandbox
from ..process import WindowsJob
from .shell import kill_windows_tree, safe_environment, shell_argv
import contextlib

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
    prepared_context: object = None
    watchdog: asyncio.Task | None = None
    authorization: dict = field(default_factory=dict)
    snapshot: object = None
    tty: bool = False
    on_output: object = None

    @property
    def running(self):
        return self.process.returncode is None


class Terminals:
    def __init__(self, workspace, settings, *, broker=None):
        self.workspace, self.settings = workspace, settings
        self.broker = broker
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
                "tty": t.tty,
            }
            for t in self.sessions.values()
        ]

    async def start(self, command, *, scope="workspace", tty=False, cols=100, rows=30,
                    process_argv=None, bridge=None, on_output=None):
        if not isinstance(tty, bool) or not isinstance(cols, int) or not isinstance(rows, int) or not 1 <= cols <= 500 or not 1 <= rows <= 200:
            raise ValueError("Terminal dimensions must be 1-500 columns and 1-200 rows; tty must be boolean")
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
        if tty and windows and process_argv is None:
            process_argv = [str(Path(os.environ["SystemRoot"]) / "System32" / "cmd.exe"), "/d", "/s", "/c", command]
        if process_argv and mode != "mxc":
            argv = process_argv
        prepared_context = None
        cwd = self.workspace
        if mode == "mxc":
            if self.broker is None:
                from .execution import ExecutionBroker

                self.broker = ExecutionBroker(self.workspace, self.settings)
            prepared_context = self.broker.prepare(command, scope, process_argv=process_argv, bridge=bridge)
            prepared = await prepared_context.__aenter__()
            argv, environ, cwd = prepared.argv, prepared.environ, prepared.cwd
        if mode == "docker":
            container = sandbox.new_name()
            argv = sandbox.argv(command, self.workspace, name=container)
            argv.insert(2, "-i")  # Keep the container's stdin open.
            environ = sandbox.environment()
        elif mode == "codex":
            argv = codex_sandbox.argv(command, self.workspace)
        flags = 0
        snapshot = None
        if self.settings.recoverable and (self.broker is None or not self.broker.read_only):
            from .recovery import Snapshot

            try:
                snapshot = await asyncio.to_thread(Snapshot, self.workspace)
            except BaseException:
                if prepared_context:
                    await prepared_context.__aexit__(None, None, None)
                raise
        if windows:
            flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000004
        try:
            if tty:
                from .pty_process import PtyProcess
                process = await PtyProcess.start(argv, cwd, safe_environment(environ), cols, rows)
            else:
                process = await asyncio.create_subprocess_exec(
                *argv, cwd=cwd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT, env=safe_environment(environ),
                creationflags=flags, start_new_session=not windows,
            )
        except BaseException:
            if prepared_context:
                await prepared_context.__aexit__(None, None, None)
            raise
        job = process.job if tty else None
        if windows and not tty:
            try:
                job = WindowsJob(process.pid, max_processes=64, max_job_memory=2 * 1024**3, max_cpu_percent=50)
            except BaseException:
                process.kill()
                await process.wait()
                if prepared_context:
                    await prepared_context.__aexit__(None, None, None)
                raise
        self.counter += 1
        terminal = Terminal(f"t{self.counter}", command, process, job, container, effective_scope)
        terminal.prepared_context = prepared_context
        terminal.snapshot = snapshot
        terminal.tty = tty
        terminal.on_output = on_output
        if self.broker:
            terminal.authorization = self.broker.authorization(scope)
        terminal.reader = asyncio.create_task(self._pump(terminal))
        self.sessions[terminal.id] = terminal
        terminal.watchdog = asyncio.create_task(self._expire(terminal))
        return terminal

    async def _expire(self, terminal):
        await asyncio.sleep(max(1, self.settings.terminal_timeout))
        terminal.watchdog = None
        await self.stop(terminal.id)  # End descendants even if their parent already exited.

    async def _pump(self, terminal):
        async def close_descendants_on_exit():
            # asyncio Process.wait can wait for inherited pipes after the parent
            # exits. Observe returncode to close the job before draining those pipes.
            while terminal.running:
                await asyncio.sleep(0.1)
            if terminal.job:
                terminal.job.close()
                terminal.job = None
            if terminal.tty:
                await terminal.process.close_console()

        monitor = asyncio.create_task(close_descendants_on_exit())
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        try:
            while chunk := await terminal.process.stdout.read(65536):
                self._append(terminal, decoder.decode(chunk))
            self._append(terminal, decoder.decode(b"", final=True))
            await terminal.process.wait()
            if terminal.snapshot:
                await asyncio.to_thread(terminal.snapshot.finish)
        finally:
            monitor.cancel()
            if terminal.job:
                terminal.job.close()
                terminal.job = None
            if terminal.prepared_context:
                context, terminal.prepared_context = terminal.prepared_context, None
                await context.__aexit__(None, None, None)
            if terminal.watchdog:
                terminal.watchdog.cancel()
            if terminal.tty:
                await terminal.process.release()
            terminal.changed.set()

    @staticmethod
    def _append(terminal, text):
        if terminal.on_output:
            text = terminal.on_output(text)
        terminal.buffer += text
        if len(terminal.buffer) > BUFFER:
            cut = len(terminal.buffer) - BUFFER
            terminal.buffer = terminal.buffer[cut:]
            terminal.dropped += cut
            terminal.read_to = max(0, terminal.read_to - cut)
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
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(terminal.changed.wait(), 0.3)
            if len(terminal.buffer) != seen:
                seen, quiet_since = len(terminal.buffer), time.monotonic()
            elif len(terminal.buffer) > terminal.read_to and time.monotonic() - quiet_since > 0.6:
                break  # Output arrived and has gone quiet.
        text = terminal.buffer[terminal.read_to :]
        terminal.read_to = len(terminal.buffer)
        if len(text) > limit:
            text = f"â€¦[{len(text) - limit:,} earlier characters omitted]â€¦\n" + text[-limit:]
        return text

    async def send(self, session_id, text, *, newline=True):
        terminal = self.get(session_id)
        if not terminal.running:
            raise OSError(f"Session {session_id} has ended (exit code {terminal.process.returncode}).")
        if len(text.encode("utf-8")) > 65536:
            raise ValueError("Terminal input exceeds 64 KiB")
        data = text + (("\r" if terminal.tty else "\n") if newline and not text.endswith(("\n", "\r")) else "")
        terminal.process.stdin.write(data.encode("utf-8"))
        await terminal.process.stdin.drain()

    def resize(self, session_id, cols, rows):
        terminal = self.get(session_id)
        if not terminal.tty or not terminal.running:
            raise ValueError("Resize requires a running PTY terminal")
        terminal.process.resize(cols, rows)

    async def stop(self, session_id):
        terminal = self.get(session_id)
        if terminal.watchdog and terminal.watchdog is not asyncio.current_task():
            terminal.watchdog.cancel()
        if terminal.running:
            if terminal.tty:
                terminal.process.kill()
            elif os.name == "nt":
                await asyncio.to_thread(kill_windows_tree, terminal.process.pid, terminal.job)
            else:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(terminal.process.pid, signal.SIGKILL)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(terminal.process.wait(), 10)
        if terminal.tty:
            await terminal.process.close_console()
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
        if terminal.prepared_context:
            context, terminal.prepared_context = terminal.prepared_context, None
            await context.__aexit__(None, None, None)
        return terminal.process.returncode

    async def close(self):
        for session_id in list(self.sessions):
            try:
                await self.stop(session_id)
            except Exception:  # noqa: BLE001 -- Best effort at shutdown.
                pass
        self.sessions.clear()
