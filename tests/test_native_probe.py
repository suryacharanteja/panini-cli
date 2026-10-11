import os
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
from panini_cli.agent import native_probe, native_sandbox


@pytest.mark.skipif(os.name != "nt", reason="Windows AppContainer API")
@pytest.mark.usefixtures("windows_sandbox_api")
def test_boundary_gate_checks_allowed_and_denied_filesystem_access():
    ready, reason = native_probe.verify_boundary()
    assert ready, reason


def test_native_spec_has_required_identifier_and_version(tmp_path):
    data = native_probe._spec(tmp_path)
    assert data[4:8] == b"SBOX"
    assert b"0.1.0" in data
    assert str(tmp_path).encode() in data


@pytest.mark.skipif(os.name != "nt", reason="Windows AppContainer API")
@pytest.mark.usefixtures("windows_sandbox_api")
def test_native_api_ignores_workspace_dll_name_collision(tmp_path, monkeypatch):
    (tmp_path / "processmodel.dll").write_bytes(b"not a Windows DLL")
    monkeypatch.chdir(tmp_path)
    ready, reason = native_sandbox.available()
    assert ready, reason


@pytest.mark.skipif(os.name != "nt", reason="Windows version check")
def test_native_api_rejects_windows_10_before_loading_dll(monkeypatch):
    monkeypatch.setattr(native_sandbox.sys, "getwindowsversion", lambda: SimpleNamespace(build=19045))
    ready, reason = native_sandbox.available()
    assert not ready and "Windows 11" in reason


@pytest.mark.skipif(os.name != "nt", reason="Windows AppContainer API")
def test_experimental_native_api_launches_and_cleans_up():
    result = native_probe.probe(suspended=True)
    if result.get("reason") == "Windows sandbox API unavailable":
        pytest.skip(result["reason"])
    assert result["available"], result
    assert result["profile_cleanup_hresult"] == 0


@pytest.mark.skipif(os.name != "nt", reason="Windows AppContainer API")
def test_native_probe_allows_write_inside_granted_root():
    result = native_probe.probe(inside_write=True)
    if result.get("reason") == "Windows sandbox API unavailable":
        pytest.skip(result["reason"])
    assert result["available"], result
    assert result["profile_cleanup_hresult"] == 0
    assert result["inside_write_succeeded"] is True, result


@pytest.mark.skipif(os.name != "nt", reason="Windows AppContainer API")
def test_native_probe_denies_write_outside_granted_root():
    with tempfile.TemporaryDirectory(prefix="panini-native-outside-") as outside:
        target = Path(outside) / "should-not-exist.txt"
        result = native_probe.probe(outside_write=target)
        if result.get("reason") == "Windows sandbox API unavailable":
            pytest.skip(result["reason"])
        assert result["available"], result
        assert result["profile_cleanup_hresult"] == 0
        assert result["outside_write_succeeded"] is False, result
        assert not target.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows AppContainer API")
def test_native_probe_denies_read_outside_granted_root():
    with tempfile.TemporaryDirectory(prefix="panini-native-outside-") as outside:
        source = Path(outside) / "private.txt"
        source.write_text("outside content", encoding="utf-8")
        result = native_probe.probe(outside_read=source)
        if result.get("reason") == "Windows sandbox API unavailable":
            pytest.skip(result["reason"])
        assert result["available"], result
        assert result["profile_cleanup_hresult"] == 0
        assert result["outside_read_succeeded"] is False, result


@pytest.mark.skipif(os.name != "nt", reason="Windows AppContainer API")
def test_native_probe_denies_new_junction_escape():
    with tempfile.TemporaryDirectory(prefix="panini-native-outside-") as outside:
        private = Path(outside) / "secret.txt"
        private.write_text("PANINI_BOUNDARY_SECRET", encoding="utf-8")
        result = native_probe.probe(junction_escape=outside, suspended=True)
        if result.get("reason") == "Windows sandbox API unavailable":
            pytest.skip(result["reason"])
        assert result["available"], result
        assert result["profile_cleanup_hresult"] == 0
        assert result["junction_read_succeeded"] is False, result
        assert result["junction_write_succeeded"] is False, result
        assert private.read_text(encoding="utf-8") == "PANINI_BOUNDARY_SECRET"
        assert not (Path(outside) / "escaped.txt").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows AppContainer API")
def test_native_probe_denies_loopback_network():
    curl = native_probe.probe(curl_version=True)
    if curl.get("reason") == "Windows sandbox API unavailable":
        pytest.skip(curl["reason"])
    assert curl["available"] and curl["exit_code"] == 0, curl
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
        result = native_probe.probe(network_url=f"http://127.0.0.1:{server.server_port}/probe")
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
    if result.get("reason") == "Windows sandbox API unavailable":
        pytest.skip(result["reason"])
    assert result["available"], result
    assert result["profile_cleanup_hresult"] == 0
    assert result["network_command_succeeded"] is False, result
    assert not seen
