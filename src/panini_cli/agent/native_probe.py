"""Experimental first-party Windows AppContainer feasibility probe.

Not a command runner and not a production sandbox. It creates a short-lived
AppContainer profile, launches only ``cmd.exe /c exit 0``, then deletes the
profile. No PANINI command is routed through this module.

The FlatBuffer field order follows Microsoft's BaseContainerSpecification.fbs.
Microsoft documents Experimental_CreateProcessInSandbox as subject to change.
"""

import ctypes
import os
import subprocess
import tempfile
import threading
import uuid
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .shell import _WindowsJob

LOAD_LIBRARY_SEARCH_SYSTEM32 = 0x00000800


def _system_library(name, *, use_last_error=False):
    """Never resolve sandbox DLLs from a user-controlled workspace or PATH."""
    return ctypes.WinDLL(name, use_last_error=use_last_error, winmode=LOAD_LIBRARY_SEARCH_SYSTEM32)


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(wintypes.BYTE)),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


def _spec(writable_root, *extra_writable_roots, read_only_roots=()):
    import flatbuffers

    builder = flatbuffers.Builder(512)
    version = builder.CreateString("0.1.0")
    roots = [
        builder.CreateString(str(Path(root).resolve())) for root in (writable_root, *extra_writable_roots)
    ]
    builder.StartVector(4, len(roots), 4)
    for path in reversed(roots):
        builder.PrependUOffsetTRelative(path)
    writable = builder.EndVector()
    read_only_paths = [builder.CreateString(str(Path(path).resolve())) for path in read_only_roots]
    builder.StartVector(4, len(read_only_paths), 4)
    for path in reversed(read_only_paths):
        builder.PrependUOffsetTRelative(path)
    read_only = builder.EndVector()
    builder.StartObject(12)
    builder.PrependUOffsetTRelativeSlot(0, version, 0)
    builder.PrependBoolSlot(1, True, False)  # AppContainer is the enforcement boundary.
    builder.PrependUOffsetTRelativeSlot(7, writable, 0)
    if read_only_paths:
        builder.PrependUOffsetTRelativeSlot(8, read_only, 0)
    root = builder.EndObject()
    builder.Finish(root, file_identifier=b"SBOX")
    return bytes(builder.Output())


def probe(
    *,
    inside_write=False,
    outside_write=None,
    outside_read=None,
    curl_version=False,
    powershell=False,
    network_url=None,
    junction_escape=None,
    suspended=False,
):
    """Return diagnostic facts; never fall back to unsandboxed process creation."""
    if os.name != "nt":
        return {"available": False, "reason": "Windows only"}
    try:
        processmodel = _system_library("processmodel.dll", use_last_error=True)
        create = processmodel.Experimental_CreateProcessInSandbox
        userenv = _system_library("userenv.dll")
    except (OSError, AttributeError):
        return {"available": False, "reason": "Windows sandbox API unavailable"}
    create.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.BOOL,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.LPCWSTR,
        ctypes.POINTER(STARTUPINFOW),
        wintypes.LPCWSTR,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(PROCESS_INFORMATION),
    ]
    create.restype = wintypes.BOOL
    userenv.DeleteAppContainerProfile.argtypes = [wintypes.LPCWSTR]
    userenv.DeleteAppContainerProfile.restype = ctypes.c_long
    kernel = _system_library("kernel32.dll", use_last_error=True)
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetExitCodeProcess.restype = wintypes.BOOL
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    identity = "PaniniNativeProbe_" + uuid.uuid4().hex
    created = False
    job = None
    result = {"available": False, "reason": "sandbox process did not complete"}
    with tempfile.TemporaryDirectory(prefix="panini-native-probe-") as workspace:
        spec = ctypes.create_string_buffer(_spec(workspace))
        if (
            sum(
                [
                    bool(inside_write),
                    outside_write is not None,
                    outside_read is not None,
                    bool(curl_version),
                    bool(powershell),
                    network_url is not None,
                    junction_escape is not None,
                ]
            )
            > 1
        ):
            raise ValueError("Run one boundary probe at a time")
        executable = str(
            Path(os.environ["SystemRoot"])
            / "System32"
            / (
                "curl.exe"
                if network_url or curl_version
                else "WindowsPowerShell\\v1.0\\powershell.exe"
                if powershell
                else "cmd.exe"
            )
        )
        if inside_write:
            target = Path(workspace) / "allowed.txt"
            command_text = f'"{executable}" /d /c echo PANINI_NATIVE_PROBE > "{target}"'
        elif outside_read is not None:
            source = Path(outside_read).resolve()
            if not source.is_file():
                raise ValueError("Boundary probe needs an existing outside file")
            target = Path(workspace) / "read.txt"
            command_text = f'"{executable}" /d /c type "{source}" > "{target}"'
        elif curl_version:
            command_text = subprocess.list2cmdline([executable, "--version"])
        elif powershell:
            command_text = subprocess.list2cmdline(
                [executable, "-NoProfile", "-NonInteractive", "-Command", "exit 0"]
            )
        elif network_url is not None:
            command_text = subprocess.list2cmdline(
                [executable, "--noproxy", "*", "--silent", "--max-time", "2", network_url]
            )
        elif junction_escape is not None:
            outside = Path(junction_escape).resolve()
            if not (outside / "secret.txt").is_file():
                raise ValueError("Junction probe needs an outside secret.txt")
            junction = Path(workspace) / "junction"
            readback = Path(workspace) / "junction-read.txt"
            command_text = (
                f'"{executable}" /d /c mklink /J "{junction}" "{outside}" '
                f'& type "{junction / "secret.txt"}" > "{readback}" '
                f'& echo PANINI_JUNCTION_ESCAPE > "{junction / "escaped.txt"}"'
            )
        elif outside_write is None:
            command_text = subprocess.list2cmdline([executable, "/c", "exit", "0"])
        else:
            target = Path(outside_write).resolve()
            if target.exists() or not target.parent.is_dir():
                raise ValueError("Boundary probe needs a nonexistent file in an existing directory")
            command_text = f'"{executable}" /d /c echo PANINI_NATIVE_PROBE > "{target}"'
        command = ctypes.create_unicode_buffer(command_text)
        from .native_sandbox import native_environment

        variables = native_environment(workspace)
        environment = ctypes.create_unicode_buffer(
            "\0".join(f"{key}={value}" for key, value in sorted(variables.items())) + "\0\0"
        )
        startup = STARTUPINFOW()
        startup.cb = ctypes.sizeof(startup)
        info = PROCESS_INFORMATION()
        try:
            created = bool(
                create(
                    executable,
                    command,
                    None,
                    None,
                    False,
                    0x08000000 | 0x00000400 | (0x00000004 if suspended else 0),
                    environment,
                    workspace,
                    ctypes.byref(startup),
                    identity,
                    spec,
                    len(spec.raw),
                    ctypes.byref(info),
                )
            )
            if not created:
                result["reason"] = f"CreateProcessInSandbox failed: {ctypes.get_last_error()}"
            else:
                if suspended:
                    job = _WindowsJob(info.dwProcessId)
                waited = kernel.WaitForSingleObject(info.hProcess, 5000)
                if waited != 0:
                    kernel.TerminateProcess(info.hProcess, 1)
                    result["reason"] = f"Sandbox process wait failed: {waited}"
                else:
                    code = wintypes.DWORD()
                    kernel.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
                    result = {
                        "available": True,
                        "exit_code": code.value,
                        "reason": "AppContainer process launched",
                    }
                    if outside_write is not None:
                        result["outside_write_succeeded"] = target.exists()
                    if inside_write:
                        result["inside_write_succeeded"] = target.is_file()
                    if outside_read is not None:
                        result["outside_read_succeeded"] = target.is_file() and bool(target.read_bytes())
                    if network_url is not None:
                        result["network_command_succeeded"] = code.value == 0
                    if junction_escape is not None:
                        result["junction_read_succeeded"] = readback.is_file() and bool(readback.read_bytes())
                        result["junction_write_succeeded"] = (outside / "escaped.txt").exists()
        finally:
            if job:
                job.close()
            if info.hThread:
                kernel.CloseHandle(info.hThread)
            if info.hProcess:
                kernel.CloseHandle(info.hProcess)
            # The API may have created a profile even if process creation failed.
            deleted = userenv.DeleteAppContainerProfile(identity)
            result["profile_cleanup_hresult"] = deleted & 0xFFFFFFFF
    return result


def verify_boundary():
    """Exercise filesystem and network isolation before accepting native mode."""
    with tempfile.TemporaryDirectory(prefix="panini-native-denied-") as directory:
        outside = Path(directory)
        secret = outside / "secret.txt"
        secret.write_text("PANINI_BOUNDARY_SECRET", encoding="utf-8")
        checks = (
            (
                "allowed workspace write",
                probe(inside_write=True, suspended=True),
                "inside_write_succeeded",
                True,
            ),
            (
                "denied outside read",
                probe(outside_read=secret, suspended=True),
                "outside_read_succeeded",
                False,
            ),
            (
                "denied outside write",
                probe(outside_write=outside / "escaped.txt", suspended=True),
                "outside_write_succeeded",
                False,
            ),
        )
        for label, result, key, expected in checks:
            if (
                not result.get("available")
                or result.get(key) is not expected
                or result.get("profile_cleanup_hresult") != 0
            ):
                return False, f"{label} failed: {result}"

        junction = probe(junction_escape=outside, suspended=True)
        if (
            not junction.get("available")
            or junction.get("junction_read_succeeded") is not False
            or junction.get("junction_write_succeeded") is not False
            or junction.get("profile_cleanup_hresult") != 0
        ):
            return False, f"denied newly created junction escape failed: {junction}"

        curl = probe(curl_version=True, suspended=True)
        if not curl.get("available") or curl.get("exit_code") != 0:
            return False, f"Network test utility unavailable in sandbox: {curl}"
        seen = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.path)
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            network = probe(
                network_url=f"http://127.0.0.1:{server.server_port}/panini-boundary",
                suspended=True,
            )
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)
        if (
            not network.get("available")
            or network.get("network_command_succeeded") is not False
            or network.get("profile_cleanup_hresult") != 0
            or seen
        ):
            return False, f"denied network access failed: {network}"
        return True, "AppContainer filesystem and loopback network boundaries verified"
