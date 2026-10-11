import asyncio
import http.client
import http.cookiejar
import json
import os
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from panini_cli.agent import mxc_runtime
from panini_cli.agent.config import Settings
from panini_cli.agent.execution import ExecutionBroker
from panini_cli.agent.preview import CSP, Previews


@pytest.fixture
def site(tmp_path):
    root = tmp_path / "dist"
    root.mkdir()
    (root / "index.html").write_text('<html><body><script src="/app.js"></script>WEB_UAT</body></html>')
    (root / "app.js").write_text("console.log('app loaded')")
    return tmp_path


def authenticated(url):
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    with opener.open(url, timeout=5) as response:
        assert b"WEB_UAT" in response.read()
        assert response.headers["Content-Security-Policy"] == CSP
    return opener


def test_preview_authentication_assets_spa_and_cross_origin_denials(site):
    async def run():
        previews = Previews(ExecutionBroker(site, Settings(sandbox="mxc", local_preview=True)))
        try:
            preview = await previews.start()
            base = preview.server.url.split("/_panini/")[0]
            with pytest.raises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(base, timeout=3)
            assert error.value.code == 403
            opener = await asyncio.to_thread(authenticated, preview.server.url)
            for path in ("/app.js", "/dashboard/settings"):
                with opener.open(base + path, timeout=3) as response:
                    assert response.status == 200
            for headers in ({"Host": "evil.test"}, {"Origin": "http://127.0.0.1:9999"}, {"Sec-Fetch-Site": "same-site"}):
                with pytest.raises(urllib.error.HTTPError) as error:
                    opener.open(urllib.request.Request(base, headers=headers), timeout=3)
                assert error.value.code == 403
            connection = http.client.HTTPConnection("127.0.0.1", preview.server.server.server_port, timeout=3)
            connection.request("POST", "/", body="ignored")
            assert connection.getresponse().status == 501
            connection.close()
            port = preview.server.server.server_port
            await previews.close()
            with pytest.raises(OSError):
                socket.create_connection(("127.0.0.1", port), timeout=1)
        finally:
            await previews.close()
    asyncio.run(run())


def test_preview_rejects_secret_traversal_hardlinks_and_runtime_changes(site):
    (site / "outside.txt").write_text("OUTSIDE_SECRET")
    (site / "dist" / ".env").write_text("FILE_SECRET")
    (site / "dist" / "credentials.json").write_text("FILE_SECRET")
    os.link(site / "outside.txt", site / "dist" / "alias.txt")
    async def run():
        previews = Previews(ExecutionBroker(site, Settings(sandbox="mxc", local_preview=True)))
        try:
            preview = await previews.start()
            base = preview.server.url.split("/_panini/")[0]
            opener = await asyncio.to_thread(authenticated, preview.server.url)
            for path in ("/.env", "/credentials.json", "/../outside.txt", "/%2e%2e/outside.txt",
                         "/%252e%252e/outside.txt", "/alias.txt", "/app.js:stream"):
                with pytest.raises(urllib.error.HTTPError) as error:
                    opener.open(base + path, timeout=3)
                assert error.value.code == 404
            (site / "dist" / "app.js").unlink()
            os.link(site / "outside.txt", site / "dist" / "app.js")
            with pytest.raises(urllib.error.HTTPError) as error:
                opener.open(base + "/app.js", timeout=3)
            assert error.value.code == 404
        finally:
            await previews.close()
    asyncio.run(run())


def test_preview_requires_explicit_grant_and_built_subdirectory(site):
    async def run():
        previews = Previews(ExecutionBroker(site, Settings(sandbox="mxc")))
        with pytest.raises(OSError, match="require --sandbox"):
            await previews.start()
        previews.broker.settings.local_preview = True
        for path in (".", "../", ".git", "missing"):
            with pytest.raises(OSError):
                await previews.start(path)
        await previews.close()
    asyncio.run(run())


def test_preview_session_limit_and_expiry(site):
    async def run():
        previews = Previews(ExecutionBroker(site, Settings(sandbox="mxc", local_preview=True, terminal_timeout=1)))
        results = await asyncio.gather(*(previews.start() for _ in range(3)), return_exceptions=True)
        assert sum(isinstance(result, OSError) for result in results) == 1
        live = [result for result in results if not isinstance(result, OSError)]
        await asyncio.wait_for(asyncio.gather(*(preview.closed.wait() for preview in live)), 5)
        assert previews.list() == []
        await previews.close()
    asyncio.run(run())


@pytest.mark.skipif(os.name != "nt" or not mxc_runtime.executable().is_file(), reason="native Windows runtime")
@pytest.mark.parametrize("hosts", [(), ("registry.npmjs.org",)])
def test_preview_grant_still_denies_sandbox_connections_to_host_services(site, hosts):
    async def run():
        broker = ExecutionBroker(site, Settings(sandbox="mxc", local_preview=True, network_hosts=hosts))
        previews = Previews(broker)
        try:
            preview = await previews.start()
            port = preview.server.server.server_port
            result = await broker.run(f"curl.exe --noproxy '*' --silent --max-time 2 http://127.0.0.1:{port}/", 10)
            assert result.exit_code != 0 and "WEB_UAT" not in result.output
        finally:
            await previews.close()
    asyncio.run(run())


@pytest.mark.skipif(os.name != "nt" or not mxc_runtime.executable().is_file(), reason="native Windows runtime")
def test_cli_preview_without_model_key_stops_on_timeout(site, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    result = subprocess.run([sys.executable, "-m", "panini_cli.agent", "preview", "dist", "-C", str(site),
                             "--timeout", "1", "--json"], env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    event = json.loads(result.stdout)
    assert event["type"] == "preview.started" and event["mode"] == "built-assets"
    assert event["policy"]["executionScope"] == "workspace" and event["policy"]["networkMode"] == "offline"


def test_agent_preview_tools_preserve_session_and_record_policy(site, monkeypatch):
    from rich.console import Console
    from panini_cli.agent.loop import Agent
    from panini_cli.agent.model import ScriptedModel
    from panini_cli.agent.cli import TerminalHost

    monkeypatch.setenv("PANINI_DATA_DIR", str(site / "agent-data"))
    async def run():
        model = ScriptedModel([
            {"calls": [{"name": "preview_start", "arguments": {"path": "dist"}}]},
            {"calls": [{"name": "preview_list", "arguments": {}}]},
            {"text": "Preview is ready."},
        ])
        agent = Agent(site, model, Settings(sandbox="mxc", local_preview=True), TerminalHost(Console()))
        try:
            await agent.turn("Preview the built app")
            assert len(agent.previews.list()) == 1
            preview = next(iter(agent.previews.sessions.values()))
            await asyncio.to_thread(authenticated, preview.server.url)
            receipts = [json.loads(line) for path in (site / "agent-data").rglob("receipts/*.jsonl")
                        for line in path.read_text().splitlines()]
            event = next(event for event in receipts if event.get("phase") == "preview_policy")
            assert event["policy"]["executionScope"] == "workspace" and event["policy"]["previewServing"] == "read-only"
            assert event["policy"]["networkMode"] == "offline" and event["policy"]["previewRoot"] == "dist"
        finally:
            await agent.close()
        assert agent.previews.list() == []
    asyncio.run(run())
