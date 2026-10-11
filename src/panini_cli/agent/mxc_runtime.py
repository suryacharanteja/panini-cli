"""PANINI Sandbox built from the bundled Microsoft MXC source.

Consume the native executor rather than starting a Node harness for every command.
The standalone executable installs its bundled engine on first use; no downloads
or fallback to host access. Source and wheel installations also support explicit setup.
"""

import hashlib
import ctypes
import json
import os
import platform
import shutil
import sys
import tempfile
from pathlib import Path
from functools import lru_cache

VERSION = "1.0.0"
BUILD = "panini-source-1"


def bundled_root():
    architecture = "arm64" if platform.machine().lower() in {"arm64", "aarch64"} else "x64"
    return Path(__file__).resolve().parent.parent / "panini-sandbox" / "runtime" / f"win-{architecture}"


def identity():
    manifest = bundled_root() / "build.json"
    return hashlib.sha256(manifest.read_bytes()).hexdigest() if manifest.is_file() else "unbuilt"


def _manifest(directory):
    manifest = json.loads((directory / "build.json").read_text(encoding="utf-8-sig"))
    expected_target = "aarch64-pc-windows-msvc" if platform.machine().lower() in {"arm64", "aarch64"} else "x86_64-pc-windows-msvc"
    if manifest.get("format") != 1 or manifest.get("target") != expected_target or not manifest.get("sources"):
        raise OSError("Invalid PANINI Sandbox source-build manifest.")
    files = manifest.get("files", {})
    if set(files) != {"wxc-exec.exe", "mxc_ffi.dll", "LICENSE.md"}:
        raise OSError("PANINI Sandbox build must contain exactly its executor, DLL and license.")
    for name, expected in files.items():
        if hashlib.sha256((directory / name).read_bytes()).hexdigest() != expected:
            raise OSError(f"PANINI Sandbox integrity check failed: {name}")
    return manifest


def root():
    # Store Python virtualizes LOCALAPPDATA writes, while a frozen executable
    # does not. Use the shared user-owned home location for a stable installation.
    build_identity = identity()[:24]
    return Path.home() / ".panini" / "runtime" / "panini-sandbox" / VERSION / f"{BUILD}-{build_identity}"


def executable():
    return root() / "wxc-exec.exe"


def install():
    if os.name != "nt":
        raise OSError("The PANINI MXC adapter requires Windows.")
    source = bundled_root()
    if not (source / "build.json").is_file():
        raise OSError("PANINI Sandbox has not been built. Run scripts/build-sandbox.ps1, then panini-cli sandbox install.")
    manifest = _manifest(source)
    destination = root()
    if destination.exists():
        installed = _manifest(destination)
        if installed == manifest:
            verify()
            return destination
        raise OSError(f"A different sandbox build already exists at {destination}; remove it explicitly before reinstalling.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent, prefix="panini-sandbox-install-") as directory:
        staging = Path(directory)
        for name in [*manifest["files"], "build.json"]:
            shutil.copyfile(source / name, staging / name)
        _manifest(staging)
        staging.rename(destination)
    return destination


def verify(workspace=None):
    """Reject mutable project executors and modified runtime files before loading them."""
    candidate = root().resolve()
    if workspace is not None and candidate.is_relative_to(Path(workspace).resolve()):
        raise OSError("MXC runtime must be outside the writable workspace.")
    try:
        destination = root().resolve(strict=True)
    except FileNotFoundError:
        if not getattr(sys, "frozen", False):
            raise OSError("PANINI Sandbox is not installed. Run panini-cli sandbox install.") from None
        destination = install().resolve(strict=True)
    if workspace is not None and destination.is_relative_to(Path(workspace).resolve()):
        raise OSError("MXC runtime must be outside the writable workspace.")
    installed = _manifest(destination)
    # Anchor external runtime hashes to the trusted CLI bundle, not an editable
    # manifest sitting beside the executables. Refuse a stale installed build.
    if installed != _manifest(bundled_root()):
        raise OSError("Installed PANINI Sandbox does not match this CLI's source build. Run panini-cli sandbox install.")
    return destination / "wxc-exec.exe"


class _ErrorDetail(ctypes.Structure):
    _fields_ = [(name, ctypes.c_void_p) for name in ("message", "operation", "native_code", "remediation")]


@lru_cache(maxsize=1)
def _library(path):
    # DLL dependencies can only come from the verified runtime or System32.
    library = ctypes.CDLL(str(path), winmode=0x00000100 | 0x00000800)
    library.mxc_probe_request_json_with_error.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(_ErrorDetail)]
    library.mxc_probe_request_json_with_error.restype = ctypes.c_int32
    library.mxc_string_free.argtypes = [ctypes.c_void_p]
    library.mxc_string_free.restype = None
    library.mxc_error_detail_free.argtypes = [ctypes.POINTER(_ErrorDetail)]
    library.mxc_error_detail_free.restype = None
    return library


def probe(config, workspace):
    import json

    binary = verify(workspace)
    library = _library(binary.parent / "mxc_ffi.dll")
    output, detail = ctypes.c_void_p(), _ErrorDetail()
    status = library.mxc_probe_request_json_with_error(json.dumps(config).encode(), ctypes.byref(output), ctypes.byref(detail))
    try:
        if status:
            message = ctypes.string_at(detail.message).decode() if detail.message else f"native status {status}"
            raise OSError(f"MXC policy validation failed: {message}")
        if not output.value:
            raise OSError("MXC policy probe returned no result.")
        return json.loads(ctypes.string_at(output.value).decode())
    finally:
        if output.value:
            library.mxc_string_free(output)
        library.mxc_error_detail_free(ctypes.byref(detail))
