"""PTY processes owned by the same Windows Job boundary as piped terminals."""

import asyncio
import contextlib
import ctypes
import os
import subprocess
from ctypes import wintypes as w

from ..process import WindowsJob


class PtyProcess:
    def __init__(self):
        self.stdin = self.stdout = self
        self.pending_input = bytearray()
        self.job = None
        self.console = None
        self.handle = None
        self.closed = False
        self.exit_code = None

    @classmethod
    async def start(cls, argv, cwd, env, cols=100, rows=30):
        process = cls()
        startup = asyncio.create_task(asyncio.to_thread(process._start, argv, cwd, env, cols, rows))
        try:
            await asyncio.shield(startup)
            return process
        except BaseException:
            # Cancellation cannot stop a native startup thread. Wait for ownership
            # to be established before closing it, otherwise a late child leaks.
            while not startup.done():
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await asyncio.shield(startup)
            with contextlib.suppress(Exception):
                startup.result()
            process.kill()
            await process.release()
            raise

    def _start(self, argv, cwd, env, cols, rows):
        if os.name != "nt":
            import pty
            self.output_handle, slave = pty.openpty()
            try:
                self.child = subprocess.Popen(argv, cwd=cwd, env=env, stdin=slave, stdout=slave,
                                              stderr=slave, start_new_session=True)
                self.pid = self.child.pid
                self.input_handle = self.output_handle
            finally:
                os.close(slave)
            self.resize(cols, rows)
            return
        kernel = self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = w.HANDLE
        kernel.CreatePipe.argtypes = [ctypes.POINTER(handle), ctypes.POINTER(handle), ctypes.c_void_p, w.DWORD]
        kernel.CreatePipe.restype = w.BOOL
        kernel.CloseHandle.argtypes = [handle]
        kernel.ReadFile.argtypes = [handle, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD), ctypes.c_void_p]
        kernel.WriteFile.argtypes = kernel.ReadFile.argtypes
        kernel.GetExitCodeProcess.argtypes = [handle, ctypes.POINTER(w.DWORD)]
        kernel.TerminateProcess.argtypes = [handle, w.UINT]

        class Coord(ctypes.Structure):
            _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]
        self.Coord = Coord
        kernel.CreatePseudoConsole.argtypes = [Coord, handle, handle, w.DWORD, ctypes.POINTER(handle)]
        kernel.CreatePseudoConsole.restype = ctypes.c_long
        kernel.ResizePseudoConsole.argtypes = [handle, Coord]
        kernel.ResizePseudoConsole.restype = ctypes.c_long
        kernel.ClosePseudoConsole.argtypes = [handle]
        kernel.InitializeProcThreadAttributeList.argtypes = [ctypes.c_void_p, w.DWORD, w.DWORD, ctypes.POINTER(ctypes.c_size_t)]
        kernel.UpdateProcThreadAttribute.argtypes = [ctypes.c_void_p, w.DWORD, ctypes.c_size_t, ctypes.c_void_p,
                                                     ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p]
        kernel.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
        class Startup(ctypes.Structure):
            _fields_ = [("cb", w.DWORD), ("reserved", w.LPWSTR), ("desktop", w.LPWSTR), ("title", w.LPWSTR),
                        ("x", w.DWORD), ("y", w.DWORD), ("sx", w.DWORD), ("sy", w.DWORD),
                        ("cx", w.DWORD), ("cy", w.DWORD), ("fill", w.DWORD), ("flags", w.DWORD),
                        ("show", w.WORD), ("reserved_size", w.WORD), ("reserved_ptr", ctypes.c_void_p),
                        ("stdin", handle), ("stdout", handle), ("stderr", handle)]
        class Extended(ctypes.Structure):
            _fields_ = [("startup", Startup), ("attributes", ctypes.c_void_p)]
        class Info(ctypes.Structure):
            _fields_ = [("process", handle), ("thread", handle), ("pid", w.DWORD), ("tid", w.DWORD)]
        kernel.CreateProcessW.argtypes = [w.LPCWSTR, w.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, w.BOOL,
                                         w.DWORD, ctypes.c_void_p, w.LPCWSTR, ctypes.POINTER(Startup), ctypes.POINTER(Info)]
        kernel.CreateProcessW.restype = w.BOOL
        pipe_in, pipe_out, self.input_handle, self.output_handle = handle(), handle(), handle(), handle()
        self.console = handle()
        try:
            if not kernel.CreatePipe(ctypes.byref(pipe_in), ctypes.byref(self.input_handle), None, 0):
                raise ctypes.WinError(ctypes.get_last_error())
            if not kernel.CreatePipe(ctypes.byref(self.output_handle), ctypes.byref(pipe_out), None, 0):
                raise ctypes.WinError(ctypes.get_last_error())
            result = kernel.CreatePseudoConsole(Coord(cols, rows), pipe_in, pipe_out, 0, ctypes.byref(self.console))
            if result:
                raise OSError(f"CreatePseudoConsole failed: HRESULT {result:#x}")
            size = ctypes.c_size_t()
            kernel.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
            buffer = ctypes.create_string_buffer(size.value)
            if not kernel.InitializeProcThreadAttributeList(buffer, 1, 0, ctypes.byref(size)):
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                if not kernel.UpdateProcThreadAttribute(buffer, 0, 0x00020016, self.console, ctypes.sizeof(handle), None, None):
                    raise ctypes.WinError(ctypes.get_last_error())
                startup = Extended()
                startup.startup.cb = ctypes.sizeof(Extended)
                # Without this flag Windows duplicates redirected parent stdio,
                # bypassing ConPTY. Null handles force new console connections.
                startup.startup.flags = 0x00000100
                startup.attributes = ctypes.cast(buffer, ctypes.c_void_p)
                info = Info()
                command = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))
                environment = ctypes.create_unicode_buffer("\0".join(f"{k}={v}" for k, v in sorted(env.items())) + "\0\0")
                if not kernel.CreateProcessW(None, command, None, None, False, 0x00080000 | 0x00000400 | 0x00000004,
                        environment, str(cwd), ctypes.byref(startup.startup), ctypes.byref(info)):
                    raise ctypes.WinError(ctypes.get_last_error())
                self.handle, self.pid = info.process, info.pid
                kernel.CloseHandle(info.thread)
                self.job = WindowsJob(self.pid, max_processes=64, max_job_memory=2 * 1024**3, max_cpu_percent=50)
            finally:
                kernel.DeleteProcThreadAttributeList(buffer)
        finally:
            if pipe_in:
                kernel.CloseHandle(pipe_in)
            if pipe_out:
                kernel.CloseHandle(pipe_out)

    @property
    def returncode(self):
        if os.name != "nt":
            return self.child.poll() if hasattr(self, "child") else None
        if not self.handle:
            return self.exit_code
        code = w.DWORD()
        if not self.kernel.GetExitCodeProcess(self.handle, ctypes.byref(code)):
            raise ctypes.WinError(ctypes.get_last_error())
        return None if code.value == 259 else code.value

    async def wait(self):
        while self.returncode is None:
            await asyncio.sleep(.05)
        return self.returncode

    def write(self, data):
        if len(self.pending_input) + len(data) > 65536:
            raise ValueError("PTY input exceeds 64 KiB")
        self.pending_input.extend(data)

    async def drain(self):
        data = bytes(self.pending_input)
        self.pending_input.clear()
        await asyncio.to_thread(self._write, data)

    def _write(self, data):
        while data:
            if os.name != "nt":
                count = os.write(self.input_handle, data)
            else:
                written = w.DWORD()
                if not self.kernel.WriteFile(self.input_handle, data, len(data), ctypes.byref(written), None):
                    raise ctypes.WinError(ctypes.get_last_error())
                count = written.value
            if not count:
                raise OSError("PTY input closed")
            data = data[count:]

    async def read(self, size):
        return await asyncio.to_thread(self._read, min(size, 65536))

    def _read(self, size):
        if os.name != "nt":
            try:
                return os.read(self.output_handle, size)
            except OSError:
                return b""
        buffer, received = ctypes.create_string_buffer(size), w.DWORD()
        if not self.kernel.ReadFile(self.output_handle, buffer, size, ctypes.byref(received), None):
            if ctypes.get_last_error() in {6, 109, 232}:
                return b""
            raise ctypes.WinError(ctypes.get_last_error())
        return buffer.raw[:received.value]

    def resize(self, cols, rows):
        if not isinstance(cols, int) or not isinstance(rows, int) or not 1 <= cols <= 500 or not 1 <= rows <= 200:
            raise ValueError("PTY size must be 1-500 columns and 1-200 rows")
        if os.name == "nt":
            result = self.kernel.ResizePseudoConsole(self.console, self.Coord(cols, rows))
            if result:
                raise OSError(f"ResizePseudoConsole failed: {result:#x}")
        else:
            import fcntl
            import struct
            import termios
            fcntl.ioctl(self.output_handle, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def kill(self):
        if self.job:
            self.job.close()
            self.job = None
        elif os.name == "nt" and self.handle:
            self.kernel.TerminateProcess(self.handle, 1)
        elif hasattr(self, "child"):
            import signal
            try:
                os.killpg(self.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    async def close_console(self):
        if os.name == "nt" and self.console:
            console, self.console = self.console, None
            await asyncio.to_thread(self.kernel.ClosePseudoConsole, console)

    async def release(self):
        if self.closed:
            return
        self.closed = True
        if os.name == "nt":
            await self.close_console()
            if self.handle:
                self.exit_code = self.returncode
                self.kernel.CloseHandle(self.handle)
                self.handle = None
            for handle in (getattr(self, "input_handle", None), getattr(self, "output_handle", None)):
                if handle:
                    self.kernel.CloseHandle(handle)
        elif hasattr(self, "output_handle"):
            os.close(self.output_handle)
