"""Own the complete child tree. A Windows Job is lifetime control, not a sandbox."""

import asyncio
import ctypes
import os
import signal
import subprocess
from ctypes import wintypes


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
                        self._attach_windows_job()
                    raise
                if os.name == "nt":
                    self._attach_windows_job()
        except BaseException:
            await self.close()
            raise
        # No protocol input/prompt is sent until ownership is established.
        return self.process

    def _attach_windows_job(self):
        class Basic(ctypes.Structure):
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

        class IO(ctypes.Structure):
            _fields_ = [
                (name, ctypes.c_uint64)
                for name in (
                    "read_ops",
                    "write_ops",
                    "other_ops",
                    "read_bytes",
                    "write_bytes",
                    "other_bytes",
                )
            ]

        class Extended(ctypes.Structure):
            _fields_ = [
                ("basic", Basic),
                ("io", IO),
                ("process_memory", ctypes.c_size_t),
                ("job_memory", ctypes.c_size_t),
                ("peak_process", ctypes.c_size_t),
                ("peak_job", ctypes.c_size_t),
            ]

        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        declarations = {
            "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            "SetInformationJobObject": (
                [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD],
                wintypes.BOOL,
            ),
            "OpenProcess": (
                [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD],
                wintypes.HANDLE,
            ),
            "AssignProcessToJobObject": (
                [wintypes.HANDLE, wintypes.HANDLE],
                wintypes.BOOL,
            ),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
        }
        for name, (args, result) in declarations.items():
            fn = getattr(self.kernel, name)
            fn.argtypes, fn.restype = args, result
        job = self.kernel.CreateJobObjectW(None, None)
        if not job:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            info = Extended()
            info.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not self.kernel.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
                raise ctypes.WinError(ctypes.get_last_error())
            child = self.kernel.OpenProcess(0x0100 | 0x0001, False, self.process.pid)
            if not child:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                if not self.kernel.AssignProcessToJobObject(job, child):
                    raise ctypes.WinError(ctypes.get_last_error())
            finally:
                self.kernel.CloseHandle(child)
            self.job = job
        except BaseException:
            self.kernel.CloseHandle(job)
            raise

    async def close(self):
        async with self.lock:
            self.closed = True
            if self.job:
                self.kernel.CloseHandle(self.job)
                self.job = None
            if self.process:
                if os.name != "nt":
                    try:
                        os.killpg(self.process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                elif self.process.returncode is None:
                    try:
                        self.process.kill()
                    except ProcessLookupError:
                        pass
                await asyncio.wait_for(self.process.wait(), 5)
