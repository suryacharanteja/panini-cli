"""Own the complete child tree. A Windows Job is lifetime control, not a sandbox."""

import asyncio
import ctypes
import os
import signal
import subprocess
from ctypes import wintypes
import contextlib


class WindowsJob:
    """A kill-on-close Job object with optional limits; Windows only."""

    def __init__(self, pid, *, max_processes=None, max_job_memory=None, max_cpu_percent=None, resume=True):
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        ntdll = ctypes.WinDLL("ntdll")
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
            if max_cpu_percent is not None:
                if not 1 <= max_cpu_percent <= 100:
                    raise ValueError("max_cpu_percent must be 1-100")
                cpu = (wintypes.DWORD * 2)(0x1 | 0x4, max_cpu_percent * 100)
                if not kernel.SetInformationJobObject(self.job, 15, ctypes.byref(cpu), ctypes.sizeof(cpu)):
                    raise ctypes.WinError(ctypes.get_last_error())
            # PROCESS_SET_QUOTA | PROCESS_TERMINATE | PROCESS_SUSPEND_RESUME
            process = kernel.OpenProcess(0x0100 | 0x0001 | 0x0800, False, pid)
            if not process:
                raise ctypes.WinError(ctypes.get_last_error())
            if not kernel.AssignProcessToJobObject(self.job, process):
                raise ctypes.WinError(ctypes.get_last_error())
            if resume and ntdll.NtResumeProcess(process) != 0:
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


class ProcessTree:
    def __init__(self):
        self.process = None
        self.job = None
        self.lock = asyncio.Lock()
        self.closed = False

    async def start(self, argv, cwd, env=None):
        try:
            async with self.lock:
                if self.process or self.closed:
                    raise RuntimeError("Process already started or stopped")
                creation = asyncio.create_task(
                    asyncio.create_subprocess_exec(
                        *argv,
                        cwd=cwd,
                        env=env,
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL,
                        limit=8 * 1024 * 1024,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                        start_new_session=os.name != "nt",
                    )
                )
                try:
                    self.process = await asyncio.shield(creation)
                except asyncio.CancelledError:
                    # Resolve spawn before propagating cancellation, so ownership cannot be lost.
                    self.process = await creation
                    if os.name == "nt":
                        self.job = WindowsJob(self.process.pid, resume=False)
                    raise
                if os.name == "nt":
                    self.job = WindowsJob(self.process.pid, resume=False)
        except BaseException:
            await self.close()
            raise
        # No protocol input/prompt is sent until ownership is established.
        return self.process

    async def close(self):
        async with self.lock:
            self.closed = True
            if self.job:
                self.job.close()
                self.job = None
            if self.process:
                if os.name != "nt":
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(self.process.pid, signal.SIGKILL)
                elif self.process.returncode is None:
                    with contextlib.suppress(ProcessLookupError):
                        self.process.kill()
                await asyncio.wait_for(self.process.wait(), 5)
