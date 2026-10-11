"""Regression reproductions and integration checks for the feature audit."""

import asyncio
import io
import json
import os
import shutil
import socket
import subprocess
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from panini_cli.agent import config, mcp, mcp_auth, mxc_runtime, public_http, server, skills, verify, web, workers
from panini_cli.agent.loop import Agent
from panini_cli.agent.model import ScriptedModel
from panini_cli.agent.scout import _SilentHost


@pytest.mark.parametrize("url", ["http://localhost/", "http://127.0.0.1/", "http://[::1]/", "http://[::ffff:127.0.0.1]/",
                                 "https://example.test:60053/", "https://user:pass@example.test/", "http://224.0.0.1/"])
def test_public_fetch_rejects_host_and_special_destinations(url):
    with pytest.raises(ValueError):
        public_http.origin(url)


def test_tool_catalog_exposes_only_usable_capabilities(tmp_path):
    agent = Agent(tmp_path, ScriptedModel([]), config.Settings(sandbox="mxc", local_preview=False), _SilentHost())
    names = {definition.get("name") for definition in agent.tool_defs()}
    assert {"read_file", "edit_file", "run_command", "worker_start", "terminal_start"} <= names
    assert not names & {"skill_load", "run_check", "mcp_resource_read", "mcp_prompt_get", "preview_start",
                        "worker_read", "worker_diff", "terminal_send", "terminal_resize"}
    agent.workers.entries["fixture"] = object()
    agent.terminals.sessions["fixture"] = SimpleNamespace(tty=True)
    agent.settings.local_preview = True
    names = {definition.get("name") for definition in agent.tool_defs()}
    assert {"worker_read", "worker_diff", "terminal_send", "terminal_resize", "preview_start"} <= names


def test_cancelled_pty_start_waits_for_child_ownership(tmp_path, monkeypatch):
    import threading
    from panini_cli.agent.pty_process import PtyProcess
    entered, proceed = threading.Event(), threading.Event()
    events = []
    def start(process, *args):
        entered.set()
        assert proceed.wait(3)
        events.append("owned")
    async def release(process):
        events.append("released")
    monkeypatch.setattr(PtyProcess, "_start", start)
    monkeypatch.setattr(PtyProcess, "kill", lambda process: events.append("killed"))
    monkeypatch.setattr(PtyProcess, "release", release)
    async def run():
        task = asyncio.create_task(PtyProcess.start([], tmp_path, {}))
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(.01)
        assert not task.done()
        proceed.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert events == ["owned", "killed", "released"]
    asyncio.run(run())


@pytest.mark.skipif(os.name != "nt", reason="Windows toolchain layout")
def test_venv_does_not_hide_base_python_read_grant(tmp_path):
    from panini_cli.agent.native_sandbox import toolchain_roots
    venv, base = tmp_path / "venv" / "Scripts", tmp_path / "Python311"
    for folder in (venv, base):
        folder.mkdir(parents=True)
        (folder / "python.exe").write_bytes(b"fixture")
    roots = toolchain_roots({"PATH": str(venv) + os.pathsep + str(base), "USERPROFILE": str(tmp_path / "home")}, include_system=True)
    assert venv in roots and venv.parent in roots and base in roots


def test_malformed_tool_keys_get_actionable_diagnostics(tmp_path):
    (tmp_path / "calc.py").write_text("return a - b\n")
    agent = Agent(tmp_path, ScriptedModel([]), config.Settings(sandbox="none"), _SilentHost())
    output, _ = asyncio.run(agent._run_tool(SimpleNamespace(name="edit_file"),
        {"path": "calc.py", "old_text:": "a - b", "new_text:": "a + b"}))
    assert "exact JSON keys" in output and "old_text" in output
    assert (tmp_path / "calc.py").read_text() == "return a - b\n"


def test_check_startup_failure_remains_unverified(tmp_path, monkeypatch):
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    class Approving(_SilentHost):
        async def approve(self, item):
            return "accept"
    steps = [{"calls": [{"name": "run_check", "arguments": {"command": "python -m pytest -q"}}]},
             {"text": "tests passed"}, {"text": "tests passed"}]
    agent = Agent(tmp_path, ScriptedModel(steps), config.Settings(sandbox="none"), Approving())
    async def fail(*args, **kwargs):
        raise OSError("fixture launcher unavailable")
    monkeypatch.setattr(agent.execution, "run", fail)
    result = asyncio.run(agent.turn("test this project"))
    assert result.status == "unverified"
    assert agent.verification_results[0]["exitCode"] is None
    assert "launcher" in agent.verification_results[0]["error"]


def test_failed_edit_cannot_finish_as_success_and_can_recover(tmp_path):
    (tmp_path / "calc.py").write_text("return a - b\n")
    failed = {"calls": [{"name": "edit_file", "arguments": {"path": "calc.py", "old_text": "wrong", "new_text": "return a + b"}}]}
    async def run():
        agent = Agent(tmp_path, ScriptedModel([failed, {"text": "fixed"}, {"text": "fixed"}]),
                      config.Settings(sandbox="none"), _SilentHost())
        result = await agent.turn("fix calc.py")
        assert result.status == "unverified" and "unsuccessful" in result.error
        assert (tmp_path / "calc.py").read_text() == "return a - b\n"
        class Approving(_SilentHost):
            async def approve(self, item):
                return "accept"
        recovered = Agent(tmp_path, ScriptedModel([failed,
            {"calls": [{"name": "edit_file", "arguments": {"path": "calc.py", "old_text": "a - b", "new_text": "a + b"}}]},
            {"text": "fixed"}]), config.Settings(sandbox="none"), Approving())
        assert (await recovered.turn("fix calc.py")).status == "completed"
        assert (tmp_path / "calc.py").read_text() == "return a + b\n"
    asyncio.run(run())


def test_redirect_cannot_reach_host_or_other_origin():
    requests = []
    def respond(request):
        requests.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://127.0.0.1:60053/internal"})
    with pytest.raises(web.tools.ToolError):
        web.fetch("https://approved.example/docs", httpx.MockTransport(respond))
    assert requests == ["https://approved.example/docs"]


def test_same_origin_redirect_and_mixed_dns(monkeypatch):
    def respond(request):
        return httpx.Response(302, headers={"Location": "/final"}) if request.url.path == "/start" else httpx.Response(200, text="safe")
    assert "safe" in web.fetch("https://example.test/start", httpx.MockTransport(respond))
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(None, None, None, None, ("8.8.8.8", 443)),
                                                             (None, None, None, None, ("127.0.0.1", 443))])
    with pytest.raises(ValueError, match="DNS"):
        public_http.addresses("example.test", 443)


def test_rpc_malformed_ids_and_oversized_frame_keep_server_alive(monkeypatch):
    monkeypatch.setattr(server, "MAX_FRAME", 512)
    source = b'{"id":[],"result":{}}\n' + b'x' * 600 + b'\n' + b'{"id":1,"method":"initialize"}\n'
    output = io.BytesIO()
    asyncio.run(server.Server(io.BytesIO(source), output).serve())
    messages = [json.loads(line) for line in output.getvalue().splitlines()]
    assert len([m for m in messages if m.get("error")]) == 2
    assert next(m for m in messages if m.get("id") == 1)["result"]["capabilities"]["sessionPolicy"]


def test_policy_rejects_unenforced_and_unscoped_grants():
    settings, mode, readonly = config.session_policy({"sandboxPolicy": {"type": "readOnly", "networkAccess": False}, "approvalPolicy": "never"})
    assert settings.sandbox == "mxc" and readonly and mode == "never" and not settings.network_hosts
    for params in [{"sandboxPolicy": {"type": "workspaceWrite", "networkAccess": True}},
                   {"sandbox": "none", "approvalPolicy": "read-only"}, {"config": {"unrecognized_security_option": True}}]:
        with pytest.raises(ValueError):
            config.session_policy(params)


def test_rpc_durable_threads_fork_and_policy_override(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "create_model", lambda settings: ScriptedModel([{"text": "remembered"}]))
    async def run():
        first = server.Server(io.BytesIO(), io.BytesIO())
        response = await first.thread_start({"cwd": str(tmp_path), "sandbox": "none"})
        identifier = response["thread"]["id"]
        await first.turn_start({"threadId": identifier, "input": [{"type": "text", "text": "remember this"}]})
        await first.turns[identifier][1]
        await first.thread_close({"threadId": identifier})
        second = server.Server(io.BytesIO(), io.BytesIO())
        try:
            await second.thread_resume({"threadId": identifier})
            assert "remember this" in json.dumps(second.threads[identifier][1].items)
            assert (await second.thread_list({"cwd": str(tmp_path)}))["data"]
            fork = await second.thread_fork({"threadId": identifier})
            assert fork["thread"]["id"] != identifier and len(second.threads) == 2
            with pytest.raises(server.RpcError, match="Close"):
                await second.thread_resume({"threadId": identifier, "sandbox": "readOnly"})
        finally:
            for key in list(second.threads):
                await second.thread_close({"threadId": key})
    asyncio.run(run())


def test_shared_mcp_startup_and_typed_images(tmp_path, monkeypatch):
    image = tmp_path / "attached.png"
    image.write_bytes(b"fake image data for plumbing test")
    seen = []
    class Connector:
        errors = {}
        def __init__(self, specs):
            seen.append(specs)
        async def start(self):
            seen.append("started")
        def tool_defs(self):
            return []
        async def close(self):
            seen.append("closed")
    monkeypatch.setattr(mcp, "McpManager", Connector)
    monkeypatch.setattr(server, "create_model", lambda settings: ScriptedModel([{"text": "image received"}]))
    monkeypatch.setattr(server, "Settings", config.Settings)
    from panini_cli.agent import userconfig
    monkeypatch.setattr(userconfig, "section", lambda name: {"fixture": {"command": "fixture"}})
    async def run():
        owner = server.Server(io.BytesIO(), io.BytesIO())
        result = await owner.thread_start({"cwd": str(tmp_path), "sandbox": "none"})
        identifier = result["thread"]["id"]
        try:
            await owner.turn_start({"threadId": identifier, "input": [{"type": "localImage", "path": str(image)}]})
            await owner.turns[identifier][1]
            assert "data:image/png;base64," in json.dumps(owner.threads[identifier][1].items)
        finally:
            await owner.thread_close({"threadId": identifier})
    asyncio.run(run())
    assert "started" in seen and "closed" in seen


def test_skill_catalog_is_progressive_and_crlf_supported(tmp_path):
    skill = tmp_path / ".agents" / "skills" / "fixture" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_bytes(b'---\r\nname: fixture\r\ndescription: Small workflow\r\n---\r\nUNIQUE FULL WORKFLOW BODY')
    catalog = skills.Skills(tmp_path)
    assert "$fixture" in catalog.instructions() and "UNIQUE FULL" not in catalog.instructions()
    assert "UNIQUE FULL" in catalog.load("fixture")
    with pytest.raises(web.tools.ToolError):
        catalog.load("missing")


def test_diagnostics_are_not_acceptance_checks():
    assert not verify.is_check("pwd", ["npm test"])
    assert not verify.is_check("npm test; echo bypass", ["npm test"])
    assert verify.is_check("npm test -- --run", ["npm test"])
    assert verify.is_check("python -m pytest tests/test_example.py", ["python -m pytest -q"])


def test_unverified_completion_cannot_be_green(tmp_path):
    model = ScriptedModel([{"calls": [{"name": "write_file", "arguments": {"path": "app.py", "content": "pass\n"}}]},
                           {"text": "All done"}, {"text": "All done"}])
    class Host(_SilentHost):
        async def approve(self, item):
            return "accept"
    agent = Agent(tmp_path, model, config.Settings(sandbox="none", acceptance_checks=("npm test",)), Host())
    async def run():
        try:
            result = await agent.turn("create app.py")
            assert result.status == "unverified" and agent._needs_verify
        finally:
            await agent.close()
    asyncio.run(run())


@pytest.mark.parametrize("sse", [False, True])
def test_remote_mcp_json_and_sse_session_resources_prompts(monkeypatch, sse):
    requests = []
    monkeypatch.setattr(mcp_auth, "access_token", lambda *args: "fixture-secret")
    def respond(request):
        message = json.loads(request.content)
        requests.append((message, dict(request.headers)))
        if "id" not in message:
            return httpx.Response(202)
        result = {"tools": []} if message["method"] == "tools/list" else {"fixture": message["method"]}
        body = {"jsonrpc": "2.0", "id": message["id"], "result": result}
        return httpx.Response(200, headers={"Mcp-Session-Id": "fixture-session", "Content-Type": "text/event-stream" if sse else "application/json"},
                              text="data: " + json.dumps(body) + "\n\n" if sse else json.dumps(body))
    async def run():
        manager = mcp.McpManager({"fixture": {"url": "https://example.test/mcp"}})
        remote = manager.servers["fixture"]
        remote.http = httpx.Client(transport=httpx.MockTransport(respond))
        try:
            await remote._initialize()
            assert (await manager.protocol("fixture", "resources/list", {}))["fixture"] == "resources/list"
            assert (await manager.protocol("fixture", "prompts/get", {"name": "fixture"}))["fixture"] == "prompts/get"
        finally:
            await manager.close()
    asyncio.run(run())
    assert requests[-1][1]["mcp-session-id"] == "fixture-session"
    assert requests[-1][1]["authorization"] == "Bearer fixture-secret"


def test_oauth_pkce_callback_and_refresh(monkeypatch):
    saved, calls = {}, []
    monkeypatch.setattr(mcp_auth.auth, "save_key", lambda key, *, target: saved.update({target: key}))
    monkeypatch.setattr(mcp_auth.auth, "read_key", lambda *, target: saved.get(target))
    def respond(request):
        calls.append(request)
        if request.url.path == "/metadata":
            return httpx.Response(200, json={"authorization_endpoint": "https://example.test/authorize",
                "token_endpoint": "https://example.test/token", "registration_endpoint": "https://example.test/register"})
        if request.url.path == "/register":
            return httpx.Response(200, json={"client_id": "fixture-client"})
        return httpx.Response(200, json={"access_token": "fixture-access", "refresh_token": "fixture-refresh", "expires_in": 3600})
    monkeypatch.setattr(mcp_auth, "PublicTransport", lambda: httpx.MockTransport(respond))
    def browser(url):
        params = parse_qs(urlsplit(url).query)
        assert params["code_challenge_method"] == ["S256"]
        redirect = params["redirect_uri"][0]
        with httpx.Client(trust_env=False) as client:
            assert client.get(redirect, params={"code": "fixture", "state": "wrong"}).status_code == 400
            assert client.get(redirect, params={"code": "fixture", "state": params["state"][0]}).status_code == 200
    monkeypatch.setattr(mcp_auth.webbrowser, "open", browser)
    spec = {"url": "https://example.test/mcp", "oauth": {"metadata_url": "https://example.test/metadata"}}
    assert "saved" in mcp_auth.login("fixture", spec, timeout=1)
    record = json.loads(saved[mcp_auth.target("fixture", spec)])
    record["expires_at"] = 0
    saved[mcp_auth.target("fixture", spec)] = json.dumps(record)
    assert mcp_auth.access_token("fixture", spec) == "fixture-access"
    assert b"code_verifier=" in calls[-2].content and b"refresh_token=" in calls[-1].content


def test_workers_use_detached_worktrees_and_block_unlisted_tools(tmp_path, monkeypatch):
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    (tmp_path / "app.txt").write_text("committed")
    subprocess.run(["git", "-C", str(tmp_path), "add", "app.txt"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=fixture", "-c", "user.email=fixture@localhost",
                    "-c", "commit.gpgsign=false", "commit", "-m", "fixture"], check=True, capture_output=True)
    (tmp_path / "app.txt").write_text("parent uncommitted")
    monkeypatch.setattr(workers.Path, "home", lambda: tmp_path / "managed-home")
    monkeypatch.setattr(workers, "create_model", lambda settings: ScriptedModel([{"text": "worker complete"}]))
    parent = Agent(tmp_path, ScriptedModel([]), config.Settings(sandbox="mxc"), _SilentHost())
    async def run():
        try:
            info = await parent.workers.start("inspect", "worktree")
            worker = parent.workers.get(info["id"])
            await worker.task
            assert (worker.workspace / "app.txt").read_text() == "committed"
            assert (tmp_path / "app.txt").read_text() == "parent uncommitted"
            call = SimpleNamespace(name="worker_start")
            result, _ = await worker.agent._run_tool(call, {"task": "recurse"})
            assert "not allowed" in result
            assert worker.info()["status"] == "completed"
        finally:
            await parent.close()
    asyncio.run(run())


@pytest.mark.skipif(os.name != "nt" or not shutil.which("node") or not mxc_runtime.executable().is_file(),
                   reason="Installed Windows MXC runtime and Node required; run sandbox install before live tests")
def test_live_mxc_pty_resize_and_authenticated_ssr(tmp_path):
    from panini_cli.agent.execution import ExecutionBroker
    from panini_cli.agent.live_preview import start_live
    from panini_cli.agent.preview import Previews
    from panini_cli.agent.terminals import Terminals
    settings = config.Settings(sandbox="mxc", local_preview=True)
    terminals = Terminals(tmp_path, settings)
    previews = Previews(ExecutionBroker(tmp_path, settings))
    previews.terminals = terminals
    (tmp_path / "tty.cjs").write_text('console.log("TTY="+process.stdout.isTTY);setTimeout(()=>{process.stdout._refreshSize();console.log("COLS="+process.stdout.columns)},1500)')
    (tmp_path / "server.cjs").write_text('require("http").createServer((req,res)=>res.end("SSR OK "+req.url)).listen(5173)')
    async def run():
        try:
            terminal = await terminals.start('node tty.cjs', tty=True)
            await asyncio.sleep(.3)
            terminals.resize(terminal.id, 120, 40)
            await asyncio.sleep(2)
            output = await terminals.read(terminal.id, 1)
            assert "TTY=true" in output and "COLS=120" in output
            preview = await start_live(previews, terminals, "server.cjs", [])
            def request():
                base = f"http://127.0.0.1:{preview.server.server.server_port}"
                with httpx.Client(follow_redirects=True, trust_env=False) as client:
                    assert client.get(base).status_code == 403
                    assert client.get(preview.server.url).text == "SSR OK /"
                    assert client.get(base + "/.env").status_code != 200
                    assert client.get(base, headers={"Origin": "https://evil.example"}).status_code == 403
            await asyncio.to_thread(request)
            assert preview.policy["hostLoopback"] == "deny" and preview.policy["transport"] == "stdio"
        finally:
            await previews.close()
            await terminals.close()
    asyncio.run(run())
