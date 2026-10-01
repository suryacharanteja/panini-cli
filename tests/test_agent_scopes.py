import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from panini_cli.agent import receipts
from panini_cli.agent.config import Settings
from panini_cli.agent.loop import Agent
from panini_cli.agent.model import ScriptedModel
from panini_cli.agent.terminals import Terminals


@pytest.fixture
def anyio_backend():
    return "asyncio"


class Host:
    def __init__(self):
        self.approvals = []

    async def started(self, item):
        pass

    async def delta(self, item_id, kind, text):
        pass

    async def completed(self, item):
        pass

    async def approve(self, item):
        self.approvals.append(item)
        return "accept"


@pytest.mark.anyio
@pytest.mark.skipif(os.name != "nt", reason="Windows host access")
async def test_native_agent_can_request_host_access_for_one_command(tmp_path, monkeypatch):
    monkeypatch.setenv("PANINI_DATA_DIR", str(tmp_path / "data"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("HOST_ACCESS_WORKED", encoding="utf-8")
    command = f"Get-Content -LiteralPath '{outside}'"
    host = Host()
    model = ScriptedModel([
        {"calls": [{"name": "run_command", "arguments": {
            "command": command, "timeout_seconds": 20, "scope": "host",
        }}]},
        {"text": "Command finished."},
    ])
    agent = Agent(workspace, model, Settings(sandbox="native"), host)
    agent.session_id = "host-scope-test"
    result = await agent.turn("Read the test file with host access.")
    assert result.status == "completed"
    assert host.approvals[0]["executionScope"] == "host"
    outputs = [item["output"] for item in model.requests[-1]["items"]
               if item.get("type") == "function_call_output"]
    assert "HOST_ACCESS_WORKED" in outputs[0]
    rows = [json.loads(line) for line in receipts.path_for(agent.session_id).read_text().splitlines()]
    assert all(row["sandbox"] == "host" for row in rows)


@pytest.mark.anyio
@pytest.mark.skipif(os.name != "nt", reason="Windows host network")
async def test_host_scope_can_reach_local_network(tmp_path, monkeypatch):
    monkeypatch.setenv("PANINI_DATA_DIR", str(tmp_path / "data"))
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"PANINI_NETWORK_OK")

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    command = f'curl.exe --noproxy "*" --silent http://127.0.0.1:{server.server_port}/scope-test'
    model = ScriptedModel([
        {"calls": [{"name": "run_command", "arguments": {
            "command": command, "timeout_seconds": 20, "scope": "host",
        }}]},
        {"text": "Network check finished."},
    ])
    agent = Agent(tmp_path, model, Settings(sandbox="native"), Host())
    try:
        result = await agent.turn("Check local network access.")
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
    assert result.status == "completed"
    outputs = [item["output"] for item in model.requests[-1]["items"]
               if item.get("type") == "function_call_output"]
    assert "PANINI_NETWORK_OK" in outputs[0]
    assert seen == ["/scope-test"]


@pytest.mark.skipif(os.name != "nt", reason="Windows host access")
def test_native_mode_host_terminal_accepts_input(tmp_path):
    script = tmp_path / "repl.py"
    script.write_text(
        "import sys\nprint('READY', flush=True)\n"
        "for line in sys.stdin:\n print('ECHO:' + line.strip(), flush=True)\n",
        encoding="utf-8",
    )
    command = f'& "{sys.executable}" -u "{script}"'

    async def go():
        sessions = Terminals(tmp_path, Settings(sandbox="native"))
        try:
            terminal = await sessions.start(command, scope="host")
            assert terminal.scope == "host"
            assert "READY" in await sessions.read(terminal.id, 5)
            await sessions.send(terminal.id, "hello")
            assert "ECHO:hello" in await sessions.read(terminal.id, 5)
        finally:
            await sessions.close()

    asyncio.run(go())


@pytest.mark.anyio
@pytest.mark.skipif(os.name != "nt", reason="Windows host access")
async def test_agent_routes_host_terminal_with_native_default(tmp_path, monkeypatch):
    monkeypatch.setenv("PANINI_DATA_DIR", str(tmp_path / "data"))
    script = tmp_path / "repl.py"
    script.write_text(
        "import sys\nprint('READY', flush=True)\n"
        "for line in sys.stdin:\n print('ECHO:' + line.strip(), flush=True)\n",
        encoding="utf-8",
    )
    command = f'& "{sys.executable}" -u "{script}"'
    model = ScriptedModel([
        {"calls": [{"name": "terminal_start", "arguments": {"command": command, "scope": "host"}}]},
        {"calls": [{"name": "terminal_send", "arguments": {"session_id": "t1", "input": "hello", "wait_seconds": 3}}]},
        {"calls": [{"name": "terminal_stop", "arguments": {"session_id": "t1"}}]},
        {"text": "Terminal checked."},
    ])
    host = Host()
    agent = Agent(tmp_path, model, Settings(sandbox="native"), host)
    agent.session_id = "host-terminal-test"
    try:
        result = await agent.turn("Check the REPL with host access.")
    finally:
        await agent.close()
    assert result.status == "completed"
    assert [item["executionScope"] for item in host.approvals] == ["host", "host"]
    outputs = [item["output"] for item in model.requests[-1]["items"]
               if item.get("type") == "function_call_output"]
    assert "READY" in outputs[0] and "ECHO:hello" in outputs[1]
    rows = [json.loads(line) for line in receipts.path_for(agent.session_id).read_text().splitlines()]
    assert all(row["sandbox"] == "host" for row in rows)


def test_invalid_execution_scope_is_rejected():
    from panini_cli.agent.loop import command_scope
    from panini_cli.agent.tools import ToolError

    with pytest.raises(ToolError, match="scope"):
        command_scope({"scope": "unrestricted-maybe"})


@pytest.mark.anyio
async def test_local_mode_is_labeled_as_host_access(tmp_path, monkeypatch):
    monkeypatch.setenv("PANINI_DATA_DIR", str(tmp_path / "data"))
    command = "Write-Output PANINI_LOCAL" if os.name == "nt" else "echo PANINI_LOCAL"
    host = Host()
    model = ScriptedModel([
        {"calls": [{"name": "run_command", "arguments": {"command": command}}]},
        {"text": "Done."},
    ])
    agent = Agent(tmp_path, model, Settings(sandbox="none"), host)
    agent.session_id = "local-host-label-test"
    assert (await agent.turn("Run the local command.")).status == "completed"
    assert host.approvals[0]["executionScope"] == "host"
    rows = [json.loads(line) for line in receipts.path_for(agent.session_id).read_text().splitlines()]
    assert all(row["sandbox"] == "host" for row in rows)
