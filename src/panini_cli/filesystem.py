"""Bounded reads verified against the opened file, rather than a stale path check."""

import ctypes
import os
import stat
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def _final_path_function():
    from ctypes import wintypes

    function = ctypes.WinDLL("kernel32.dll", use_last_error=True).GetFinalPathNameByHandleW
    function.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    function.restype = wintypes.DWORD
    return function


def opened_path(stream, target):
    if os.name == "nt":
        import msvcrt

        buffer = ctypes.create_unicode_buffer(32768)
        length = _final_path_function()(msvcrt.get_osfhandle(stream.fileno()), buffer, len(buffer), 0)
        if not length or length >= len(buffer):
            raise OSError("Could not verify the opened file.")
        name = buffer.value
        name = "\\\\" + name[8:] if name.startswith("\\\\?\\UNC\\") else name.removeprefix("\\\\?\\")
        return Path(name)
    current = Path(target).resolve(strict=True)
    before, after = os.fstat(stream.fileno()), current.stat()
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        raise OSError("File changed while opening.")
    return current


def read_bounded(path, root, max_bytes, *, validate=None):
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError("Only regular, non-linked files may be read.")
        opened = opened_path(stream, path)
        if not opened.is_relative_to(Path(root)):
            raise OSError("Opened file is outside its permitted directory.")
        if validate:
            validate(opened)
        if info.st_size > max_bytes:
            raise OSError(f"File exceeds its {max_bytes:,}-byte read limit.")
        data = stream.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise OSError(f"File grew beyond its {max_bytes:,}-byte read limit.")
        return data
