"""Approved commands run non-interactively in their own killable process tree, without credentials.

On Windows each command starts suspended, joins a new Job object (kill-on-close) and
only then resumes, so no child can escape before the tree is owned. Stopping a
command, a timeout, or its completion ends every process it started.
"""

import asyncio
import codecs
import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass

from ..process import WindowsJob
import contextlib

_ENVIRONMENT_KEYS = frozenset({
    "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "OS", "PROCESSOR_ARCHITECTURE",
    "NUMBER_OF_PROCESSORS", "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
    "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432", "LANG", "LC_ALL", "LC_CTYPE", "TERM",
    "COLORTERM", "NO_COLOR", "PYTHONIOENCODING", "PYTHONUTF8", "PYTHONUNBUFFERED", "PAGER",
    "GIT_PAGER", "GIT_TERMINAL_PROMPT", "TOKENIZERS_PARALLELISM",
})
_OUTPUT_KEEP = 256 * 1024
_TAIL_WAIT = 2.0


def safe_environment(environ=None):
    """Construct a tool environment; unknown names and credential-agent sockets never pass."""
    source = os.environ if environ is None else environ
    env = {key: value for key, value in source.items() if key.upper() in _ENVIRONMENT_KEYS}
    env.update(
        PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PAGER="cat", GIT_PAGER="cat", GIT_TERMINAL_PROMPT="0"
    )
    return env


def shell_name():
    if os.name == "nt":
        return "PowerShell" if (shutil.which("pwsh") or shutil.which("powershell")) else "cmd"
    return "bash" if os.path.exists("/bin/bash") else "sh"


def powershell_script(command):
    return (
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
        "$OutputEncoding=[System.Text.Encoding]::UTF8;$ProgressPreference='SilentlyContinue';"
        "$global:LASTEXITCODE=$null\n"
        f"{command}\n"
        "$paniniOk=$?\n"
        "if ($null -ne $LASTEXITCODE -and $LASTEXITCODE -ne 0) { exit $LASTEXITCODE }"
        " elseif (-not $paniniOk) { exit 1 }"
    )


def shell_argv(command):
    if os.name == "nt":
        executable = shutil.which("pwsh") or shutil.which("powershell")
        if not executable:
            return ["cmd.exe", "/d", "/s", "/c", command]
        # UTF-8 output, no progress bars, and the real exit code of the last native command.
        script = powershell_script(command)
        return [executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script]
    return ["/bin/bash" if os.path.exists("/bin/bash") else "/bin/sh", "-c", command]


def kill_windows_tree(pid, job=None):
    """Stop PowerShell's native descendants before the Job closes its root."""
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            check=False,
            timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    if job:
        job.kill()


@dataclass
class CommandResult:
    exit_code: int | None
    output: str
    duration: float
    timed_out: bool
    omitted: int  # Characters dropped from the middle of a very long output.
    stderr: str = ""  # Only with separate_stderr (hooks).
    policy: dict | None = None
    recovery_id: str | None = None


async def run_command(
    command, cwd, timeout, on_output=None, environ=None, argv=None, *, stdin_text=None, separate_stderr=False,
    max_processes=None, max_job_memory=None, max_cpu_percent=None, max_output_bytes=None
):
    """Run one approved command; returns when its whole process tree has ended."""
    windows = os.name == "nt"
    flags = 0
    if windows:
        flags = (
            subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000004
        )  # CREATE_SUSPENDED
    process = await asyncio.create_subprocess_exec(
        *(argv or shell_argv(command)),
        cwd=cwd,
        stdin=asyncio.subprocess.PIPE if stdin_text is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE if separate_stderr else asyncio.subprocess.STDOUT,
        env=safe_environment(environ),
        creationflags=flags,
        start_new_session=not windows,
        limit=1024 * 1024,
    )
    job = None
    if windows:
        try:
            job = WindowsJob(process.pid, max_processes=max_processes, max_job_memory=max_job_memory, max_cpu_percent=max_cpu_percent)
        except BaseException:
            process.kill()
            await process.wait()
            raise

    def kill_tree():
        if windows:
            # A PowerShell-launched child can otherwise retain the output pipe.
            kill_windows_tree(process.pid, job)
        else:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)

    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    head, tail, total = [], "", 0

    def keep(text):
        nonlocal tail, total
        total += len(text)
        if sum(len(part) for part in head) < _OUTPUT_KEEP // 4:
            head.append(text)
        else:
            tail = (tail + text)[-(_OUTPUT_KEEP * 3 // 4) :]

    async def pump():
        output_bytes = 0
        while chunk := await process.stdout.read(65536):
            output_bytes += len(chunk)
            if max_output_bytes is not None and output_bytes > max_output_bytes:
                raise OSError("Command output exceeded the task's output limit; process tree was stopped.")
            text = decoder.decode(chunk)
            if text:
                keep(text)
                if on_output:
                    await on_output(text)

    errors = []

    async def pump_errors():
        while chunk := await process.stderr.read(65536):
            if sum(len(e) for e in errors) < _OUTPUT_KEEP:
                errors.append(chunk.decode("utf-8", errors="replace"))

    async def feed():
        try:
            process.stdin.write(stdin_text.encode("utf-8"))
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass  # The command did not read its input.
        finally:
            process.stdin.close()

    started = time.monotonic()
    timed_out = False
    reader = asyncio.create_task(pump())
    helpers = [asyncio.create_task(pump_errors())] if separate_stderr else []
    if stdin_text is not None:
        helpers.append(asyncio.create_task(feed()))
    process_wait = asyncio.create_task(process.wait())

    async def wait_for_process():
        done, _ = await asyncio.wait((process_wait, reader), return_when=asyncio.FIRST_COMPLETED)
        if reader in done:
            reader.result()  # An output/pump failure must stop the process immediately.
        await process_wait
    try:
        try:
            await asyncio.wait_for(wait_for_process(), timeout)
        except TimeoutError:
            timed_out = True
            kill_tree()
            await process.wait()
        # Background children may still hold the output pipe; they end with the tree.
        try:
            await asyncio.wait_for(asyncio.shield(reader), _TAIL_WAIT)
        except TimeoutError:
            kill_tree()
            try:
                await asyncio.wait_for(reader, 5)
            except TimeoutError:
                pass  # wait_for cancelled the reader; whatever was read is kept.
    except BaseException:
        kill_tree()
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
        if process.returncode is None:
            await process.wait()
        raise
    finally:
        if job:
            job.close()
        if helpers:
            done, pending = await asyncio.wait(helpers, timeout=2)
            for task in pending:
                task.cancel()
            await asyncio.gather(*helpers, return_exceptions=True)
    remainder = decoder.decode(b"", final=True)
    if remainder:
        keep(remainder)
    head_text = "".join(head)
    omitted = max(0, total - len(head_text) - len(tail))
    output = head_text + (f"\n…[{omitted:,} characters omitted]…\n" if omitted else "") + tail
    return CommandResult(
        None if timed_out else process.returncode,
        output,
        time.monotonic() - started,
        timed_out,
        omitted,
        "".join(errors),
    )
