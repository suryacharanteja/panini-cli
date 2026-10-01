"""Approved commands run non-interactively in their own killable process tree, without credentials.

On Windows each command starts suspended, joins a new Job object (kill-on-close) and
only then resumes, so no child can escape before the tree is owned. Stopping a
command, a timeout, or its completion ends every process it started.
"""

import asyncio
import codecs
import ctypes
import os
import re
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass

_CREDENTIAL = re.compile(
    r"(?i)(^|_)(api_?key|access_?key|key|secret|token|password|passwd|credential|credentials)($|_)"
)
_OUTPUT_KEEP = 256 * 1024
_TAIL_WAIT = 2.0


def safe_environment(environ=None):
    """The command gets the user's environment without API keys, tokens or passwords."""
    env = {key: value for key, value in (environ or os.environ).items() if not _CREDENTIAL.search(key)}
    env.update(
        PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PAGER="cat", GIT_PAGER="cat", GIT_TERMINAL_PROMPT="0"
    )
    return env


def shell_name():
    if os.name == "nt":
        return "PowerShell" if (shutil.which("pwsh") or shutil.which("powershell")) else "cmd"
    return "bash" if os.path.exists("/bin/bash") else "sh"


def shell_argv(command):
    if os.name == "nt":
        executable = shutil.which("pwsh") or shutil.which("powershell")
        if not executable:
            return ["cmd.exe", "/d", "/s", "/c", command]
        # UTF-8 output, no progress bars, and the real exit code of the last native command.
        script = (
            "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
            "$OutputEncoding=[System.Text.Encoding]::UTF8;$ProgressPreference='SilentlyContinue';"
            "$global:LASTEXITCODE=$null\n"
            f"{command}\n"
            "$paniniOk=$?\n"
            "if ($null -ne $LASTEXITCODE -and $LASTEXITCODE -ne 0) { exit $LASTEXITCODE }"
            " elseif (-not $paniniOk) { exit 1 }"
        )
        return [executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script]
    return ["/bin/bash" if os.path.exists("/bin/bash") else "/bin/sh", "-c", command]


class _WindowsJob:
    def __init__(self, pid, *, max_processes=None, max_job_memory=None):
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        ntdll = ctypes.WinDLL("ntdll")
        from ctypes import wintypes

        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
        self.kernel = kernel

        class Limits(ctypes.Structure):
            _fields_ = [
                ("process_time", ctypes.c_int64),
                ("job_time", ctypes.c_int64),
                ("flags", wintypes.DWORD),
                ("min_ws", ctypes.c_size_t),
                ("max_ws", ctypes.c_size_t),
                ("active", wintypes.DWORD),
                ("affinity", ctypes.c_size_t),
                ("priority", wintypes.DWORD),
                ("scheduling", wintypes.DWORD),
            ]

        class Extended(ctypes.Structure):
            _fields_ = [
                ("basic", Limits),
                ("io", ctypes.c_uint64 * 6),
                ("process_memory", ctypes.c_size_t),
                ("job_memory", ctypes.c_size_t),
                ("peak_process", ctypes.c_size_t),
                ("peak_job", ctypes.c_size_t),
            ]

        self.job = kernel.CreateJobObjectW(None, None)
        if not self.job:
            raise ctypes.WinError(ctypes.get_last_error())
        process = None
        try:
            info = Extended()
            info.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if max_processes is not None:
                if max_processes < 1:
                    raise ValueError("max_processes must be positive")
                info.basic.flags |= 0x00000008  # JOB_OBJECT_LIMIT_ACTIVE_PROCESS
                info.basic.active = max_processes
            if max_job_memory is not None:
                if max_job_memory < 1:
                    raise ValueError("max_job_memory must be positive")
                info.basic.flags |= 0x00000200  # JOB_OBJECT_LIMIT_JOB_MEMORY
                info.job_memory = max_job_memory
            if not kernel.SetInformationJobObject(self.job, 9, ctypes.byref(info), ctypes.sizeof(info)):
                raise ctypes.WinError(ctypes.get_last_error())
            # PROCESS_SET_QUOTA | PROCESS_TERMINATE | PROCESS_SUSPEND_RESUME
            process = kernel.OpenProcess(0x0100 | 0x0001 | 0x0800, False, pid)
            if not process:
                raise ctypes.WinError(ctypes.get_last_error())
            if not kernel.AssignProcessToJobObject(self.job, process):
                raise ctypes.WinError(ctypes.get_last_error())
            if ntdll.NtResumeProcess(process) != 0:
                raise OSError("The command could not be resumed")
        except BaseException:
            if process:
                kernel.TerminateProcess(process, 1)
            self.close()
            raise
        finally:
            if process:
                kernel.CloseHandle(process)

    def kill(self):
        if self.job:
            self.kernel.TerminateJobObject(self.job, 1)

    def close(self):
        if self.job:
            self.kernel.CloseHandle(self.job)  # Kill-on-close ends anything still running.
            self.job = None


def kill_windows_tree(pid, job=None):
    """Stop PowerShell's native descendants before the Job closes its root."""
    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            check=False,
            timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass
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


async def run_command(
    command, cwd, timeout, on_output=None, environ=None, argv=None, *, stdin_text=None, separate_stderr=False
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
            job = _WindowsJob(process.pid)
        except BaseException:
            process.kill()
            await process.wait()
            raise

    def kill_tree():
        if windows:
            # A PowerShell-launched child can otherwise retain the output pipe.
            kill_windows_tree(process.pid, job)
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

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
        while chunk := await process.stdout.read(65536):
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
    try:
        try:
            await asyncio.wait_for(process.wait(), timeout)
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
