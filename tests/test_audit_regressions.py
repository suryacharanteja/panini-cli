import os
import sys
import asyncio
import json

import pytest

from panini_cli.agent import patch, sessions, tools
from panini_cli.agent.mcp import _Server, _tool_name
from panini_cli.engine import find_panini_cli
from panini_cli.verification import file_receipt


def test_hardlink_alias_cannot_expose_outside_file(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    secret = tmp_path / "outside.txt"
    secret.write_text("private sentinel")
    os.link(secret, root / "public.txt")
    ws = tools.Workspace(root)
    with pytest.raises(tools.ToolError):
        tools.read_file(ws, "public.txt")
    assert "private sentinel" not in tools.search_files(ws, "sentinel")
    with pytest.raises(ValueError):
        file_receipt(root, "public.txt")


def test_catastrophic_regex_has_a_deadline(tmp_path):
    (tmp_path / "large.txt").write_text("a" * 50000 + "!")
    with pytest.raises(tools.ToolError, match="time"):
        tools.search_files(tools.Workspace(tmp_path), "(a+)+$")


def test_patch_preflight_preserves_first_file_when_later_file_changed(tmp_path):
    (tmp_path / "a.txt").write_text("old\n")
    (tmp_path / "b.txt").write_text("old\n")
    changes = patch.prepare(tools.Workspace(tmp_path), "*** Begin Patch\n*** Delete File: a.txt\n*** Delete File: b.txt\n*** End Patch")
    (tmp_path / "b.txt").write_text("changed\n")
    with pytest.raises(tools.ToolError, match="changed"):
        patch.apply(changes)
    assert (tmp_path / "a.txt").read_text() == "old\n"


def test_session_identifiers_cannot_escape_storage(tmp_path, monkeypatch):
    monkeypatch.setenv("PANINI_DATA_DIR", str(tmp_path))
    with pytest.raises(ValueError):
        sessions.save("../outside", tmp_path, [], "test")
    sessions.root().mkdir()
    (sessions.root() / "invalid.json").write_text("[]")
    assert sessions.listing() == []


def test_mcp_sanitized_names_remain_distinct():
    assert _tool_name("server", "a-b") != _tool_name("server", "a_b")
    assert len(_tool_name("server", "x" * 200)) <= 64


def test_frozen_engine_launches_supported_entrypoint(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.delenv("PANINI_CLI_PATH", raising=False)
    assert find_panini_cli() == [sys.executable]


def test_mcp_failed_send_releases_pending_request():
    async def run():
        server = _Server("test", {})
        async def fail(message):
            raise OSError("closed pipe")
        server._send = fail
        with pytest.raises(OSError):
            await server.request("tools/list", {})
        assert not server.pending
    asyncio.run(run())


def test_mcp_cancelled_request_releases_pending_request():
    async def run():
        server = _Server("test", {})
        async def send(message):
            pass
        server._send = send
        task = asyncio.create_task(server.request("tools/list", {}))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not server.pending
    asyncio.run(run())


def test_search_keeps_top_results_and_complete_candidate_count(tmp_path):
    (tmp_path / "matches.txt").write_text("needle\n" * 20000)
    (tmp_path / "a.py").write_text("def needle():\n    pass\n")
    result = json.loads(tools.search_code(tools.Workspace(tmp_path), "needle", max_results=20))
    assert result["candidate_count"] == 20001
    assert len(result["results"]) == 20
    assert result["results"][0]["path"] == "a.py"
    assert result["incomplete"]
