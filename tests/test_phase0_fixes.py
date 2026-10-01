"""Regression tests for the 2026-10-01 audit fixes (docs/AUDIT-2026-10-01.md §2)."""

import asyncio
import json
import os
from pathlib import Path

import pytest

from panini_cli.agent import native_sandbox, sessions, tools
from panini_cli.agent.config import Settings
from panini_cli.agent.loop import OMITTED_ARGUMENTS, Agent
from panini_cli.agent.model import ScriptedModel


@pytest.fixture
def anyio_backend():
    return "asyncio"


native = pytest.mark.skipif(
    not native_sandbox.available()[0] or os.getenv("PANINI_SKIP_NATIVE"),
    reason="needs the Windows 11 native sandbox",
)


@native
@pytest.mark.parametrize(
    "command, code",
    [("nosuchcmd123", 1), ("Get-Item nope.txt", 1), ("cmd /c exit 3", 3), ("Write-Output ok", 0)],
)
def test_native_sandbox_reports_the_real_exit_code(tmp_path, command, code):
    result = asyncio.run(native_sandbox.run_command(command, tmp_path, 60))
    assert result.exit_code == code, result.output


def test_toolchain_roots_never_grant_profiles_system_or_program_files(tmp_path):
    home = tmp_path / "home"
    tool = home / "AppData" / "Local" / "Programs" / "Python" / "Python311"
    scripts = tool / "Scripts"
    local_bin = home / ".local" / "bin"
    for folder in (scripts, local_bin):
        folder.mkdir(parents=True)
    (tool / "python.exe").write_bytes(b"")
    (scripts / "pip.exe").write_bytes(b"")
    (local_bin / "uv.exe").write_bytes(b"")
    environ = {
        "PATH": os.pathsep.join([str(tool), str(scripts), str(local_bin), os.environ.get("SystemRoot", r"C:\Windows")]),
        "PATHEXT": ".EXE",
        "USERPROFILE": str(home),
        "SystemRoot": os.environ.get("SystemRoot", r"C:\Windows"),
    }
    roots = native_sandbox.toolchain_roots(environ)
    assert tool.resolve() in roots and scripts.resolve() in roots and local_bin.resolve() in roots
    assert (home / ".local").resolve() not in roots  # the parent of ~/.local/bin holds user data
    assert home.resolve() not in roots
    assert not any(root.is_relative_to(Path(environ["SystemRoot"]).resolve()) for root in roots)


def test_link_scan_is_cached_and_invalidated(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(native_sandbox, "_reject_unsafe_workspace_links", lambda ws: calls.append(ws))
    native_sandbox.invalidate_link_scan()
    native_sandbox.checked_workspace_links(tmp_path)
    native_sandbox.checked_workspace_links(tmp_path)
    assert len(calls) == 1
    native_sandbox.invalidate_link_scan(tmp_path)
    native_sandbox.checked_workspace_links(tmp_path)
    assert len(calls) == 2


def test_session_pruning_is_per_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("PANINI_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(sessions, "KEEP", 2)
    a, b = tmp_path / "a", tmp_path / "b"
    sessions.save("b-only", b, [{"role": "user", "content": "x"}], "b")
    for index in range(4):
        sessions.save(f"a-{index}", a, [{"role": "user", "content": "x"}], "a")
    assert [row["id"] for row in sessions.listing(b)] == ["b-only"]
    assert len(sessions.listing(a)) == 2


def test_search_code_hashes_each_file_once(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("router = 1\nrouter = 2\nrouter = 3\n")
    calls = []
    real = tools.hashlib.sha256
    monkeypatch.setattr(tools.hashlib, "sha256", lambda data: calls.append(1) or real(data))
    result = json.loads(tools.search_code(tools.Workspace(tmp_path), "router"))
    assert len(result["results"]) == 3 and len(calls) == 1


class Host:
    def __init__(self):
        self.started_at = {}

    async def started(self, item):
        pass

    async def delta(self, *args):
        pass

    async def completed(self, item):
        pass

    async def approve(self, item):
        return "decline"


@pytest.mark.anyio
async def test_read_only_calls_in_one_step_run_concurrently(tmp_path, monkeypatch):
    for name in ("a.txt", "b.txt", "c.txt"):
        (tmp_path / name).write_text(name)
    running, peak = 0, 0
    original = tools.read_file

    def slow_read(ws, path, start=None, end=None):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        import time

        time.sleep(0.2)
        running -= 1
        return original(ws, path, start, end)

    monkeypatch.setattr(tools, "read_file", slow_read)
    reads = [{"name": "read_file", "arguments": {"path": p}} for p in ("a.txt", "b.txt", "c.txt")]
    model = ScriptedModel([{"calls": reads}, {"text": "Done."}])
    agent = Agent(tmp_path, model, Settings(sandbox="none"), Host())
    result = await agent.turn("read them")
    assert result.status == "completed"
    assert peak >= 2
    outputs = [i["output"] for i in model.requests[-1]["items"] if i.get("type") == "function_call_output"]
    assert ["a.txt" in outputs[0], "b.txt" in outputs[1], "c.txt" in outputs[2]] == [True, True, True]


def test_context_fit_drops_large_old_arguments(tmp_path):
    agent = Agent(tmp_path, ScriptedModel([]), Settings(sandbox="none", context_tokens=20_000), Host())
    big = json.dumps({"path": "x.py", "content": "y" * 30_000})
    for index in range(10):
        agent.items.append({"type": "function_call", "call_id": f"c{index}", "name": "write_file", "arguments": big})
        agent.items.append({"type": "function_call_output", "call_id": f"c{index}", "output": "ok"})
    agent._fit_context()
    assert agent.items[0]["arguments"] == OMITTED_ARGUMENTS
    assert agent.items[-2]["arguments"] == big  # recent calls are kept
