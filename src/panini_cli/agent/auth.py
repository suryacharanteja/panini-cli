"""PANINI-CLI credentials in the current Windows user's Credential Manager."""

import ctypes
import os
from ctypes import wintypes

TARGET = "PANINI-CLI/OpenAI"
_GENERIC = 1
_LOCAL_MACHINE = 2
_NOT_FOUND = 1168


class _FILETIME(ctypes.Structure):
    _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]


class _CREDENTIAL(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", _FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


def _api():
    if os.name != "nt":
        raise OSError("Windows Credential Manager is available only on Windows")
    api = ctypes.WinDLL("Advapi32", use_last_error=True)
    api.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIAL), wintypes.DWORD]
    api.CredWriteW.restype = wintypes.BOOL
    api.CredReadW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.POINTER(_CREDENTIAL)),
    ]
    api.CredReadW.restype = wintypes.BOOL
    api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    api.CredDeleteW.restype = wintypes.BOOL
    api.CredFree.argtypes = [ctypes.c_void_p]
    api.CredFree.restype = None
    return api


def save_key(key: str, *, target: str = TARGET):
    value = key.strip()
    if not value or value.startswith("your_") or "\n" in value or "\r" in value:
        raise ValueError("Enter a non-empty API key, not a placeholder")
    raw = value.encode("utf-8")
    if len(raw) > 2560:
        raise ValueError("API key exceeds Windows Credential Manager's size limit")
    blob = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
    credential = _CREDENTIAL(
        Type=_GENERIC,
        TargetName=target,
        CredentialBlobSize=len(raw),
        CredentialBlob=blob,
        Persist=_LOCAL_MACHINE,
        UserName="PANINI-CLI",
    )
    if not _api().CredWriteW(ctypes.byref(credential), 0):
        raise ctypes.WinError(ctypes.get_last_error())


def read_key(*, target: str = TARGET) -> str | None:
    api = _api()
    pointer = ctypes.POINTER(_CREDENTIAL)()
    if not api.CredReadW(target, _GENERIC, 0, ctypes.byref(pointer)):
        error = ctypes.get_last_error()
        if error == _NOT_FOUND:
            return None
        raise ctypes.WinError(error)
    try:
        credential = pointer.contents
        return ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize).decode("utf-8")
    finally:
        api.CredFree(pointer)


def delete_key(*, target: str = TARGET) -> bool:
    if _api().CredDeleteW(target, _GENERIC, 0):
        return True
    error = ctypes.get_last_error()
    if error == _NOT_FOUND:
        return False
    raise ctypes.WinError(error)
