import asyncio
import json
import os
import socket
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from rich.console import Console

from panini_cli.agent import approvals, hooks, mxc_runtime, recovery
from panini_cli.agent.cli import TerminalHost
from panini_cli.agent.config import Settings
from panini_cli.agent.execution import ExecutionBroker, ExecutionPolicy, require_capabilities
from panini_cli.agent.network_proxy import NetworkProxy, normalize_hosts, public_addresses
from panini_cli.agent.shell import safe_environment
from panini_cli.agent.terminals import Terminals


def test_grants_cannot_cross_scope_workspace_task_or_policy(tmp_path):
    rules = approvals.SessionRules()
    item = {"executionScope": "workspace", "cwd": str(tmp_path), "policyIdentity": "offline", "taskId": "one"}
    rules.allow("npm test", item)
    assert rules.allowed("npm test", item)
    for key, value in {"executionScope": "host", "cwd": str(tmp_path / "other"), "policyIdentity": "network", "taskId": "two"}.items():
        assert not rules.allowed("npm test", {**item, key: value})


def test_secret_commands_are_not_classified_as_trusted():
    assert not approvals.is_trusted("Get-Content .env")
    assert not approvals.is_trusted("cat config/secrets.json")


def test_unknown_environment_and_credential_agents_are_not_inherited():
    env = safe_environment({"PATH": "safe", "DATABASE_URL": "synthetic", "SSH_AUTH_SOCK": "synthetic", "PYTHONPATH": "injected"})
    assert env["PATH"] == "safe"
    assert not {"DATABASE_URL", "SSH_AUTH_SOCK", "PYTHONPATH"} & env.keys()
    assert "PATH" not in safe_environment({})


def test_backend_selection_fails_closed():
    with pytest.raises(OSError, match="native BaseContainer"):
        require_capabilities({"tier": "appcontainer-dacl", "needsDaclAugmentation": True})
    with pytest.raises(OSError, match="secret-file exclusions"):
        require_capabilities({"tier": "base-container", "needsDaclAugmentation": False, "probes": {}})


def test_policy_changes_invalidate_authorization(tmp_path):
    offline = ExecutionPolicy(tmp_path, "mxc")
    assert offline.identity != ExecutionPolicy(tmp_path, "mxc", network_hosts=("pypi.org",)).identity
    assert offline.identity != ExecutionPolicy(tmp_path, "mxc", read_only=True).identity
    assert offline.identity != ExecutionPolicy(tmp_path, "mxc", local_preview=True).identity
    assert offline.identity != ExecutionPolicy(tmp_path, "mxc", read_roots=(str(tmp_path / "tools"),)).identity
    assert offline.identity != ExecutionPolicy(tmp_path, "mxc", runtime_identity="another-native-build").identity


def test_explicit_host_scope_is_separate_from_scoped_network_grants(tmp_path):
    broker = ExecutionBroker(tmp_path, Settings(sandbox="mxc", network_hosts=("pypi.org",)))
    assert broker.policy("host").backend == "none"
    assert broker.authorization("host")["executionScope"] == "host"
    assert broker.authorization("host")["filesystemMode"] == "host-unrestricted"
    assert broker.authorization("host")["networkMode"] == "host-unrestricted"
    assert not broker.policy("host").network_hosts


def test_routine_commands_are_autonomous_but_host_access_is_not(tmp_path):
    host = TerminalHost(Console())
    host.trust_read_only = False
    item = {"command": "npm test", "executionScope": "workspace", "sandbox": "mxc", "policyIdentity": "one"}
    assert host.auto_approved(item)
    assert not host.auto_approved({**item, "executionScope": "host"})
    assert not host.auto_approved({**item, "permissionRequest": True})


def test_project_hook_refuses_host_execution(tmp_path, monkeypatch):
    config = tmp_path / ".panini" / "hooks.json"
    config.parent.mkdir()
    config.write_text(json.dumps({"hooks": {"PreToolUse": [{"command": "echo safe"}]}}))
    monkeypatch.setattr(hooks, "TRUST_FILE", tmp_path.parent / "trusted-test.json")
    hooks.trust(config)
    configured = hooks.Hooks(tmp_path, user_file=tmp_path / "none.json")
    configured.broker = ExecutionBroker(tmp_path, Settings(sandbox="none"))
    blocked, _ = asyncio.run(configured.run("PreToolUse", tool="run_command"))
    assert "host execution was refused" in blocked


def test_network_grants_are_exact_public_hosts():
    assert normalize_hosts(["PyPI.org", "pypi.org"]) == ("pypi.org",)
    for host in ["localhost", "127.0.0.1", "*.github.com", "https://github.com", "github.com:443"]:
        with pytest.raises(ValueError):
            normalize_hosts([host])


def test_dns_private_and_mixed_answers_are_blocked(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))])
    with pytest.raises(OSError, match="private"):
        public_addresses("pypi.org")


def test_proxy_rejects_ungranted_host_and_port():
    proxy = NetworkProxy(["pypi.org"]).start()
    try:
        for destination in ["example.com:443", "pypi.org:80", "127.0.0.1:443"]:
            with socket.create_connection(("127.0.0.1", proxy.server.server_port), timeout=3) as client:
                client.sendall(f"CONNECT {destination} HTTP/1.1\r\nHost: {destination}\r\n\r\n".encode())
                assert b"403" in client.recv(1024)
    finally:
        proxy.close()


def test_recovery_restores_deleted_untracked_files_and_preserves_later_edits(tmp_path, monkeypatch):
    monkeypatch.setattr(recovery, "root", lambda: tmp_path.parent / "recovery-test")
    (tmp_path / "untracked.txt").write_text("original")
    (tmp_path / "edited.txt").write_text("original")
    (tmp_path / ".env").write_text("synthetic-secret")
    snapshot = recovery.Snapshot(tmp_path)
    (tmp_path / "untracked.txt").unlink()
    (tmp_path / "edited.txt").write_text("agent")
    (tmp_path / "new.txt").write_text("agent")
    snapshot.finish()
    (tmp_path / "edited.txt").write_text("user")
    restored, conflicts = recovery.restore(snapshot.id, tmp_path)
    assert "untracked.txt" in restored and "new.txt" in restored
    assert conflicts == ["edited.txt"]
    assert (tmp_path / "untracked.txt").read_text() == "original"
    assert not (tmp_path / "new.txt").exists()
    assert (tmp_path / "edited.txt").read_text() == "user"
    assert not (snapshot.path / "files" / ".env").exists()


def test_recovery_recreates_deleted_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(recovery, "root", lambda: tmp_path / "recovery")
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "notes.txt").write_text("original")
    snapshot = recovery.Snapshot(workspace)
    assert workspace.resolve().is_relative_to(tmp_path.resolve())
    shutil.rmtree(workspace)
    snapshot.finish()
    restored, conflicts = recovery.restore(snapshot.id, workspace)
    assert restored == ["notes.txt"] and not conflicts
    assert (workspace / "notes.txt").read_text() == "original"


def test_git_bookkeeping_does_not_run_repository_filters_or_signing(tmp_path, monkeypatch):
    from panini_cli.agent import gitops

    monkeypatch.setenv("DATABASE_URL", "SYNTHETIC_GIT_SECRET")
    def git(*arguments):
        return subprocess.run(["git", "-C", str(tmp_path), *arguments], check=True, capture_output=True, text=True)
    git("init")
    git("config", "filter.unsafe.clean", "echo HOST_FILTER_RAN > escaped.txt; cat")
    git("config", "filter.unsafe.smudge", "echo HOST_FILTER_RAN > escaped.txt; cat")
    git("config", "filter.unsafe.required", "true")
    git("config", "diff.unsafe.textconv", "echo HOST_TEXTCONV_RAN > textconv.txt; cat")
    git("config", "commit.gpgsign", "true")
    git("config", "gpg.program", "PANINI_NONEXISTENT_SIGNER")
    (tmp_path / ".gitattributes").write_text("*.txt filter=unsafe diff=unsafe\n")
    (tmp_path / "file.txt").write_text("original")
    before = gitops.snapshot(tmp_path)
    (tmp_path / "file.txt").write_text("changed")
    after = gitops.snapshot(tmp_path)
    assert before and after and "changed" in gitops.diff(tmp_path, before, after)
    gitops.undo(tmp_path, before, after)
    assert (tmp_path / "file.txt").read_text() == "original"
    assert not (tmp_path / "escaped.txt").exists() and not (tmp_path / "textconv.txt").exists()


native_mxc = pytest.mark.skipif(os.name != "nt" or not mxc_runtime.executable().is_file(), reason="installed Microsoft MXC Windows runtime required")


@native_mxc
def test_real_mxc_accepts_internal_build_hardlinks_and_rejects_external_alias(tmp_path):
    original = tmp_path / "build-script.exe"
    original.write_text("original")
    os.link(original, tmp_path / "build-script-build.exe")
    broker = ExecutionBroker(tmp_path, Settings(sandbox="mxc"))
    result = asyncio.run(broker.run("Set-Content build-script.exe 'changed'; Get-Content build-script-build.exe", 20))
    assert result.exit_code == 0 and "changed" in result.output
    outside = tmp_path.parent / "outside-build-alias.exe"
    os.link(original, outside)
    try:
        with pytest.raises(OSError, match="hard link outside"):
            asyncio.run(broker.run("Write-Output refused", 20))
    finally:
        outside.unlink()


def test_mxc_refuses_hardlink_aliases_to_secrets_or_git_metadata(tmp_path):
    from panini_cli.agent.native_sandbox import _reject_unsafe_workspace_links

    (tmp_path / ".git").mkdir()
    for source in (tmp_path / ".env", tmp_path / ".git" / "config"):
        source.write_text("protected")
        alias = tmp_path / "public.txt"
        os.link(source, alias)
        try:
            with pytest.raises(OSError, match="protected file"):
                _reject_unsafe_workspace_links(tmp_path, allow_internal_hardlinks=True)
        finally:
            alias.unlink()


@native_mxc
def test_real_mxc_workspace_write_secret_denial_and_outside_denial(tmp_path):
    (tmp_path / ".env").write_text("SYNTHETIC_MUST_NOT_REACH_MODEL")
    outside = tmp_path.parent / "outside-denial.txt"
    async def run():
        broker = ExecutionBroker(tmp_path, Settings(sandbox="mxc"))
        success = await broker.run("Set-Content inside.txt 'ok'; Get-Content inside.txt", 20)
        secret = await broker.run("Get-Content .env", 20)
        escape = await broker.run(f"Set-Content -LiteralPath '{outside}' 'bad'", 20)
        return success, secret, escape
    success, secret, escape = asyncio.run(run())
    assert success.exit_code == 0 and "ok" in success.output
    assert secret.exit_code != 0 and "SYNTHETIC_MUST_NOT_REACH_MODEL" not in secret.output
    assert escape.exit_code != 0 and not outside.exists()


@native_mxc
def test_real_mxc_read_only_and_protected_git_metadata(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("original")
    async def run():
        read_only = ExecutionBroker(tmp_path, Settings(sandbox="mxc"), read_only=True)
        denied = await read_only.run("Set-Content changed.txt 'bad'", 20)
        broker = ExecutionBroker(tmp_path, Settings(sandbox="mxc"))
        git = await broker.run("Set-Content .git/config 'bad'", 20)
        return denied, git
    denied, git = asyncio.run(run())
    assert denied.exit_code != 0 and not (tmp_path / "changed.txt").exists()
    assert git.exit_code != 0 and (tmp_path / ".git" / "config").read_text() == "original"


@native_mxc
def test_real_mxc_terminal_stdin_and_child_cleanup(tmp_path):
    (tmp_path / "repl.js").write_text("console.log('ready'); require('readline').createInterface({input:process.stdin}).on('line', x=>{console.log('echo:'+x);if(x==='exit')process.exit(0)});")
    async def run():
        terminals = Terminals(tmp_path, Settings(sandbox="mxc"))
        try:
            terminal = await terminals.start("node repl.js")
            assert "ready" in await terminals.read(terminal.id, 5)
            await terminals.send(terminal.id, "hello")
            assert "echo:hello" in await terminals.read(terminal.id, 5)
            await terminals.send(terminal.id, "exit")
            await asyncio.wait_for(terminal.process.wait(), 10)
        finally:
            await terminals.close()
    asyncio.run(run())


@native_mxc
def test_real_mxc_timeout_and_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "SYNTHETIC_MUST_NOT_REACH_CHILD")
    async def run():
        broker = ExecutionBroker(tmp_path, Settings(sandbox="mxc"))
        env = await broker.run("Write-Output $env:DATABASE_URL; Write-Output SAFE", 20)
        timeout = await broker.run("Start-Sleep -Seconds 60", 1)
        return env, timeout
    env, timeout = asyncio.run(run())
    assert "SAFE" in env.output and "SYNTHETIC_MUST_NOT_REACH_CHILD" not in env.output
    assert timeout.timed_out


@native_mxc
def test_real_mxc_output_limit_stops_execution_and_reports_recovery(tmp_path, monkeypatch):
    monkeypatch.setattr(recovery, "root", lambda: tmp_path.parent / "output-recovery-test")
    (tmp_path / "before.txt").write_text("original")
    broker = ExecutionBroker(tmp_path, Settings(sandbox="mxc", recoverable=True))
    with pytest.raises(OSError, match="output limit.*Recovery snapshot"):
        asyncio.run(broker.run("Remove-Item before.txt; Write-Output ('x' * 5000000); Start-Sleep -Seconds 60", 15))
    manifest = next(recovery.root().glob("*/manifest.json"))
    restored, conflicts = recovery.restore(manifest.parent.name, tmp_path)
    assert restored == ["before.txt"] and not conflicts


def test_preview_does_not_broaden_command_network_policy(tmp_path):
    broker = ExecutionBroker(tmp_path, Settings(sandbox="mxc", network_hosts=("pypi.org",), local_preview=True))
    assert broker.policy().local_preview
    assert broker.authorization()["networkMode"] == "proxy-only"
    assert broker.authorization()["executionScope"] == "workspace"
    with pytest.raises(OSError, match="require the MXC"):
        ExecutionBroker(tmp_path, Settings(sandbox="none", local_preview=True)).policy()


@native_mxc
def test_real_mxc_python_toolchain_and_sandboxed_server(tmp_path):
    (tmp_path / "server.js").write_text("require('http').createServer((q,r)=>r.end('OK')).listen(0,'127.0.0.1',()=>console.log('ready'))")
    async def run():
        broker = ExecutionBroker(tmp_path, Settings(sandbox="mxc"))
        result = await broker.run('python -c "print(123)"', 20)
        assert result.exit_code == 0 and "123" in result.output
        terminals = Terminals(tmp_path, Settings(sandbox="mxc"))
        try:
            terminal = await terminals.start("node server.js")
            assert "ready" in await terminals.read(terminal.id, 5)
        finally:
            await terminals.close()
    asyncio.run(run())


def test_network_exception_does_not_escalate_filesystem_or_host_access(tmp_path, monkeypatch):
    from panini_cli.agent.loop import Agent
    from panini_cli.agent.model import ScriptedModel

    monkeypatch.setenv("PANINI_DATA_DIR", str(tmp_path.parent / "network-test-data"))
    class Host:
        async def started(self, item):
            pass
        async def completed(self, item):
            pass
        async def approve(self, item):
            assert item["permissionRequest"] and item["executionScope"] == "workspace"
            return "accept"
    settings = Settings(sandbox="mxc")
    agent = Agent(tmp_path, ScriptedModel([]), settings, Host())
    output, declined = asyncio.run(agent._network_access({"hosts": ["pypi.org"], "reason": "install dependency"}))
    assert not declined and "pypi.org" in output
    assert settings.sandbox == "mxc" and settings.network_hosts == ("pypi.org",)


@native_mxc
def test_real_mxc_cannot_reach_host_service_even_with_proxy(tmp_path):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    received = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"CONTROL_PLANE")
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    async def run():
        for settings in [Settings(sandbox="mxc"),
                         Settings(sandbox="mxc", network_hosts=("registry.npmjs.org",))]:
            broker = ExecutionBroker(tmp_path, settings)
            result = await broker.run(f"curl.exe --noproxy '*' --silent --max-time 2 http://127.0.0.1:{server.server_port}", 10)
            assert result.exit_code != 0 and "CONTROL_PLANE" not in result.output
    try:
        asyncio.run(run())
        assert not received
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@native_mxc
@pytest.mark.skipif(os.getenv("PANINI_TEST_NETWORK") != "1", reason="explicit live registry test")
def test_real_npm_install_then_offline_execution_without_host_escalation(tmp_path):
    async def run():
        connected = ExecutionBroker(tmp_path, Settings(sandbox="mxc", network_hosts=("registry.npmjs.org",)))
        result = await connected.run("npm.cmd install is-number@7.0.0 --ignore-scripts --no-audit --no-fund", 40)
        assert result.exit_code == 0, result.output
        offline = ExecutionBroker(tmp_path, Settings(sandbox="mxc"))
        result = await offline.run("node -e \"console.log(require('is-number')(123))\"", 20)
        assert result.exit_code == 0 and "true" in result.output
    asyncio.run(run())


@native_mxc
def test_cli_exec_finishes_workspace_command_without_host_approval(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    script = tmp_path / "model.json"
    script.write_text(json.dumps([
        {"calls": [{"name": "run_command", "arguments": {"command": "Set-Content result.txt OK; Get-Content result.txt"}}]},
        {"text": "Verified result.txt."},
    ]))
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
           "PANINI_CLI_SCRIPTED_MODEL": str(script), "PANINI_CLI_SANDBOX": "mxc", "PANINI_CLI_APPROVAL_MODE": "ask",
           "PANINI_CLI_NETWORK_HOSTS": "", "PANINI_CLI_LOCAL_PREVIEW": "0", "PANINI_CLI_RECOVERABLE": "0",
           "PANINI_DATA_DIR": str(tmp_path / "data")}
    result = subprocess.run([sys.executable, "-m", "panini_cli.agent", "exec", "Run the command", "-C", str(workspace), "--json"],
                            env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stderr
    assert (workspace / "result.txt").read_text().strip() == "OK"
    events = [json.loads(line) for line in result.stdout.splitlines()]
    command = next(event["item"] for event in events if event.get("type") == "item.started" and event["item"]["type"] == "commandExecution")
    assert command["sandbox"] == "mxc" and command["executionScope"] == "workspace" and command["policyIdentity"]
    assert "Needs approval" not in result.stderr


@native_mxc
def test_project_hook_script_cannot_read_secret(tmp_path, monkeypatch):
    monkeypatch.setattr(hooks, "TRUST_FILE", tmp_path.parent / "hook-trust-test.json")
    config = tmp_path / ".panini" / "hooks.json"
    config.parent.mkdir()
    config.write_text(json.dumps({"hooks": {"PreToolUse": [{"command": "python .panini/guard.py"}]}}))
    (config.parent / "guard.py").write_text("from pathlib import Path\nprint(Path('.env').read_text())")
    (tmp_path / ".env").write_text("SYNTHETIC_HOOK_SECRET")
    hooks.trust(config)
    configured = hooks.Hooks(tmp_path, user_file=tmp_path / "missing.json")
    configured.broker = ExecutionBroker(tmp_path, Settings(sandbox="mxc"))
    blocked, output = asyncio.run(configured.run("PreToolUse", tool="run_command"))
    assert not blocked and "failed" in output and "SYNTHETIC_HOOK_SECRET" not in output


@native_mxc
def test_read_only_mode_does_not_relabel_existing_writable_terminal(tmp_path, monkeypatch):
    from panini_cli.agent.loop import Agent
    from panini_cli.agent.model import ScriptedModel

    monkeypatch.setenv("PANINI_DATA_DIR", str(tmp_path.parent / "terminal-policy-test"))
    host = TerminalHost(Console())
    agent = Agent(tmp_path, ScriptedModel([]), Settings(sandbox="mxc"), host)
    (tmp_path / "repl.js").write_text("console.log('ready'); require('readline').createInterface({input:process.stdin}).on('line', x=>console.log('echo:'+x));")
    async def run():
        try:
            await agent._terminal("terminal_start", {"command": "node repl.js"})
            terminal = agent.terminals.get("t1")
            assert terminal.authorization["filesystemMode"] == "workspace-write"
            host.set_mode("read-only")
            output, declined = await agent._terminal("terminal_send", {"session_id": "t1", "input": "hello"})
            assert declined and "declin" in output.lower()
        finally:
            await agent.terminals.close()
    asyncio.run(run())
