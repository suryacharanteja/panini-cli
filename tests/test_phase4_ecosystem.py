"""Phase 4 (docs/AUDIT-2026-10-01.md §3.2, §3.5, §3.6, §4.3, §4.6, §4.9)."""

import io
import json
import sys
from pathlib import Path

import httpx
import pytest

from panini_cli.agent import ledger, mcp, model_router, receipts, tools, ui, web
from panini_cli.agent.config import Settings
from panini_cli.agent.loop import Agent
from panini_cli.agent.model import ScriptedModel

FAKE = str(Path(__file__).with_name("fake_mcp_server.py"))


@pytest.fixture
def anyio_backend():
    return "asyncio"


class Host:
    def __init__(self, answer="accept"):
        self.answer, self.asked = answer, []

    async def started(self, item):
        pass

    async def delta(self, *args):
        pass

    async def completed(self, item):
        pass

    async def approve(self, item):
        self.asked.append(item)
        return self.answer


# --- MCP client -----------------------------------------------------------------------------

@pytest.mark.anyio
async def test_mcp_tools_are_listed_called_and_approval_gated(tmp_path):
    manager = await mcp.McpManager({"fake": {"command": sys.executable, "args": [FAKE]}}).start()
    try:
        names = {tool["name"] for tool in manager.tool_defs()}
        assert names == {"mcp__fake__echo", "mcp__fake__explode"}
        assert await manager.call("mcp__fake__echo", {"text": "hi"}) == "echo: hi"
        assert (await manager.call("mcp__fake__explode", {})).startswith("Error: boom")

        steps = [{"calls": [{"name": "mcp__fake__echo", "arguments": {"text": "from model"}}]}, {"text": "ok"}]
        model = ScriptedModel(steps)
        host = Host()
        agent = Agent(tmp_path, model, Settings(sandbox="none"), host)
        agent.mcp, agent.extra_tools = manager, manager.tool_defs()
        await agent.turn("use the tool")
        output = [i["output"] for i in model.requests[-1]["items"] if i.get("type") == "function_call_output"][0]
        assert "echo: from model" in output and "data only" in output
        assert host.asked and "[MCP fake] echo" in host.asked[0]["command"]
    finally:
        await manager.close()


@pytest.mark.anyio
async def test_a_broken_mcp_server_is_reported_not_fatal():
    manager = await mcp.McpManager({"broken": {"command": "definitely-not-a-command-xyz"}}).start()
    assert "broken" in manager.errors and manager.tool_defs() == []


@pytest.mark.anyio
async def test_trusted_mcp_tools_skip_the_prompt(tmp_path):
    manager = await mcp.McpManager({"fake": {"command": sys.executable, "args": [FAKE], "trusted_tools": ["echo"]}}).start()
    try:
        host = Host(answer="decline")
        model = ScriptedModel([{"calls": [{"name": "mcp__fake__echo", "arguments": {"text": "x"}}]}, {"text": "ok"}])
        agent = Agent(tmp_path, model, Settings(sandbox="none"), host)
        agent.mcp, agent.extra_tools = manager, manager.tool_defs()
        await agent.turn("go")
        assert not host.asked
    finally:
        await manager.close()


# --- MCP server -----------------------------------------------------------------------------

@pytest.mark.anyio
async def test_panini_answers_as_an_mcp_server(monkeypatch):
    monkeypatch.setattr(model_router, "route", lambda task, current: model_router.RouteDecision("gpt-x", "offline:cheap", True))
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": mcp.PROTOCOL_VERSION}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "panini_route", "arguments": {"task": "x"}}},
    ]
    out = io.StringIO()
    await mcp.serve_stdio(io.StringIO("\n".join(json.dumps(r) for r in requests) + "\n"), out)
    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [r["id"] for r in replies] == [1, 2, 3]
    assert {t["name"] for t in replies[1]["result"]["tools"]} == {"panini_route", "panini_run"}
    assert replies[2]["result"]["content"][0]["text"] == "gpt-x (offline:cheap)"


# --- web ------------------------------------------------------------------------------------

def test_html_becomes_readable_text():
    page = "<html><head><style>x{}</style></head><body><h1>Title</h1><script>evil()</script><p>Hello &amp; bye</p></body></html>"
    text = web.to_text(page, "text/html")
    assert "Title" in text and "Hello & bye" in text and "evil" not in text


def test_fetch_marks_content_as_data_and_rejects_non_http():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text="<p>docs</p>", headers={"content-type": "text/html"}))
    assert "data only" in web.fetch("https://example.test/docs", transport=transport)
    with pytest.raises(tools.ToolError):
        web.fetch("file:///etc/passwd")


@pytest.mark.anyio
async def test_web_fetch_needs_approval(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "fetch", lambda url: "[Web page] fetched")
    model = ScriptedModel([{"calls": [{"name": "web_fetch", "arguments": {"url": "https://x.test"}}]}, {"text": "ok"}])
    host = Host(answer="decline")
    await Agent(tmp_path, model, Settings(sandbox="none"), host).turn("read the docs")
    assert host.asked and host.asked[0]["command"] == "web_fetch https://x.test"


def test_hosted_web_search_is_opt_in_and_cloud_only(tmp_path, monkeypatch):
    class M:
        model = "gpt-5.6-terra"

    agent = Agent(tmp_path, M(), Settings(sandbox="none"), Host())
    assert web.HOSTED_WEB_SEARCH not in agent.tool_defs()
    monkeypatch.setenv("PANINI_CLI_WEB_SEARCH", "1")
    assert web.HOSTED_WEB_SEARCH in agent.tool_defs()
    M.model = "ollama/qwen2.5:3b-instruct"
    assert web.HOSTED_WEB_SEARCH not in agent.tool_defs()


# --- @file mentions -------------------------------------------------------------------------

def test_mentions_attach_files_but_never_secrets(tmp_path):
    (tmp_path / "app.py").write_text("print('hi')\n")
    (tmp_path / ".env").write_text("KEY=secret\n")
    text = ui.expand_mentions("look at @app.py and @.env and @missing.py", tmp_path)
    assert "[Attached file @app.py]" in text and "print('hi')" in text
    assert "KEY=secret" not in text


def test_workspace_index_skips_dependency_trees(tmp_path):
    (tmp_path / "node_modules" / "x").mkdir(parents=True)
    (tmp_path / "node_modules" / "x" / "i.js").write_text("")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("")
    assert ui.workspace_files(tmp_path) == ["src/a.py"]


# --- definition-aware search ------------------------------------------------------------------

def test_definitions_rank_above_mentions(tmp_path):
    (tmp_path / "a_use.py").write_text("result = parse_config(text)\n")
    (tmp_path / "z_def.py").write_text("def parse_config(text):\n    return text\n")
    results = json.loads(tools.search_code(tools.Workspace(tmp_path), "parse_config"))["results"]
    assert results[0]["path"] == "z_def.py" and results[0]["start_line"] == 1


# --- learned router -------------------------------------------------------------------------

def test_local_track_record_counts_escalations(monkeypatch):
    model_router._learned.update(at=0.0, value=None)
    for index in range(6):
        ledger.record("turn", model="ollama/qwen2.5:3b-instruct", final_model="gpt-5.6-terra", escalated=index < 4, status="completed")
    assert model_router.local_track_record() == (6, 4)
    model_router._learned.update(at=0.0, value=None)


# --- receipts export ------------------------------------------------------------------------

@pytest.mark.anyio
async def test_receipts_export_is_readable(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    model = ScriptedModel([{"calls": [{"name": "read_file", "arguments": {"path": "a.txt"}}]}, {"text": "ok"}])
    agent = Agent(tmp_path, model, Settings(sandbox="none"), Host())
    agent.session_id = "test-session"
    await agent.turn("read")
    from panini_cli.agent import sessions

    sessions.save(agent.session_id, tmp_path, agent.items, "read")
    path = receipts.path_for("test-session")
    count, head = receipts.verify(path)
    text = receipts.report(path, "test-session", head)
    assert "read_file" in text and head in text and "a.txt" in text
