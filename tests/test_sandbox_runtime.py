import hashlib
import json
import os
import platform
import sys

import pytest

from panini_cli.agent import mxc_runtime


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    files = {}
    for name in ("wxc-exec.exe", "mxc_ffi.dll", "LICENSE.md"):
        payload = name.encode()
        (bundle / name).write_bytes(payload)
        files[name] = hashlib.sha256(payload).hexdigest()
    target = "aarch64-pc-windows-msvc" if platform.machine().lower() in {"arm64", "aarch64"} else "x86_64-pc-windows-msvc"
    (bundle / "build.json").write_text(json.dumps({"format": 1, "target": target, "sources": {"src/lib.rs": "source-hash"}, "files": files}))
    monkeypatch.setattr(mxc_runtime, "bundled_root", lambda: bundle)
    monkeypatch.setattr(mxc_runtime, "root", lambda: tmp_path / "installed")
    return bundle


@pytest.mark.skipif(os.name != "nt", reason="Windows native installation")
def test_install_local_build_is_idempotent_and_rejects_modified_payload(runtime):
    installed = mxc_runtime.install()
    assert mxc_runtime.install() == installed
    assert mxc_runtime.verify() == installed / "wxc-exec.exe"
    (installed / "wxc-exec.exe").write_bytes(b"tampered")
    with pytest.raises(OSError, match="integrity"):
        mxc_runtime.verify()


@pytest.mark.skipif(os.name != "nt", reason="Windows native installation")
def test_external_manifest_cannot_authorize_replaced_binary(runtime):
    installed = mxc_runtime.install()
    (installed / "wxc-exec.exe").write_bytes(b"tampered")
    manifest_path = installed / "build.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"]["wxc-exec.exe"] = hashlib.sha256(b"tampered").hexdigest()
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(OSError, match="does not match"):
        mxc_runtime.verify()


def test_unbuilt_and_invalid_source_artifacts_fail_closed(runtime):
    (runtime / "wxc-exec.exe").write_bytes(b"tampered")
    with pytest.raises(OSError, match="integrity"):
        mxc_runtime._manifest(runtime)
    (runtime / "build.json").unlink()
    if os.name == "nt":
        with pytest.raises(OSError, match="has not been built"):
            mxc_runtime.install()


@pytest.mark.skipif(os.name != "nt", reason="Windows native installation")
def test_runtime_cannot_be_loaded_from_writable_workspace(runtime):
    installed = mxc_runtime.install()
    with pytest.raises(OSError, match="outside the writable workspace"):
        mxc_runtime.verify(installed.parent)


@pytest.mark.skipif(os.name != "nt", reason="Windows native installation")
def test_frozen_release_bootstraps_verified_bundled_runtime(runtime, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert not mxc_runtime.root().exists()
    binary = mxc_runtime.verify(runtime.parent / "project")
    assert binary == mxc_runtime.root() / "wxc-exec.exe" and binary.read_bytes() == b"wxc-exec.exe"
    assert mxc_runtime.verify() == binary


@pytest.mark.skipif(os.name != "nt", reason="Windows native installation")
def test_frozen_bootstrap_refuses_workspace_runtime_before_install(runtime, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    with pytest.raises(OSError, match="outside the writable workspace"):
        mxc_runtime.verify(runtime.parent)
    assert not mxc_runtime.root().exists()
