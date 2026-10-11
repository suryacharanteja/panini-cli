import json
import os
import subprocess
import sys
import pytest
from pathlib import Path

SOURCE = str(Path(__file__).resolve().parents[1] / "src")


@pytest.mark.parametrize("arguments,message", [
    (("exec", " "), "task is empty"),
    (("exec", "test", "--output-schema", "missing-schema.json"), "Cannot read the output schema"),
    (("resume", "missing-session"), "No saved session"),
])
def test_invalid_exec_and_resume_inputs(tmp_path, monkeypatch, arguments, message):
    monkeypatch.setenv("PANINI_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PANINI_CLI_SANDBOX", "none")
    completed, _ = cli(tmp_path, [], *arguments)
    assert completed.returncode in (1, 2)
    assert message in completed.stdout + completed.stderr


def test_interactive_local_commands_and_invalid_approval_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("PANINI_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PANINI_CLI_SANDBOX", "none")
    script = tmp_path / "script.json"
    script.write_text("[]")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    env = {**os.environ, "PYTHONPATH": SOURCE, "PANINI_CLI_SCRIPTED_MODEL": str(script), "NO_COLOR": "1"}
    result = subprocess.run(
        [sys.executable, "-m", "panini_cli.agent", "-C", str(workspace)],
        input="/help\n/undo\n/approvals invalid\n/approvals read-only\n/image\n/new\n/exit\n",
        env=env, capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert result.returncode == 0, result.stderr
    for expected in ("Nothing to undo", "Modes:", "Usage: /image", "Started a new conversation"):
        assert expected in result.stdout


def test_exec_keyboard_interrupt_returns_130_and_closes_agent(tmp_path, monkeypatch):
    from panini_cli.agent import cli as agent_cli
    closed = []
    monkeypatch.setenv("PANINI_CLI_SANDBOX", "none")
    monkeypatch.setenv("PANINI_DATA_DIR", str(tmp_path / "data"))
    script = tmp_path / "script.json"
    script.write_text("[]")
    monkeypatch.setenv("PANINI_CLI_SCRIPTED_MODEL", str(script))
    async def interrupted(self, *args, **kwargs):
        raise KeyboardInterrupt()
    async def close(self):
        closed.append(True)
    monkeypatch.setattr(agent_cli.Agent, "turn", interrupted)
    monkeypatch.setattr(agent_cli.Agent, "close", close)
    assert agent_cli.main(["exec", "test", "-C", str(tmp_path)]) == 130
    assert closed == [True]


def cli(tmp_path, steps, *arguments):
    script = tmp_path / "model.json"
    script.write_text(json.dumps(steps), encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    env = {
        **os.environ,
        "PYTHONPATH": SOURCE,
        "PANINI_CLI_SCRIPTED_MODEL": str(script),
        "NO_COLOR": "1",
    }
    completed = subprocess.run(
        [sys.executable, "-m", "panini_cli.agent", *arguments, "-C", str(workspace)],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    return completed, workspace


STEPS = [
    {"calls": [{"name": "write_file", "arguments": {"path": "notes.md", "content": "# Notes\n"}}]},
    {"text": "Created notes.md."},
]


def test_exec_runs_one_task_with_approval_flags(tmp_path):
    completed, workspace = cli(tmp_path, STEPS, "exec", "Create notes.md", "--approve-all")
    assert completed.returncode == 0, completed.stderr
    assert (workspace / "notes.md").read_text() == "# Notes\n"
    assert "Create notes.md" in completed.stdout and "applied" in completed.stdout
    assert "Created notes.md." in completed.stdout and "done" in completed.stdout
    assert "Named for Panini" not in completed.stdout


def test_nobody_to_ask_means_nothing_is_approved(tmp_path):
    completed, workspace = cli(tmp_path, STEPS, "exec", "Create notes.md")
    assert completed.returncode == 0, completed.stderr
    assert not (workspace / "notes.md").exists()
    assert "Needs approval" in completed.stdout and "not applied" in completed.stdout


def test_app_server_answers_the_protocol_handshake(tmp_path):
    env = {**os.environ, "PYTHONPATH": SOURCE}
    completed = subprocess.run(
        [sys.executable, "-m", "panini_cli.agent", "app-server", "--stdio"],
        input='{"id":1,"method":"initialize","params":{}}\n{"id":2,"method":"nope","params":{}}\n',
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    replies = [json.loads(line) for line in completed.stdout.splitlines()]
    assert replies[0]["result"]["serverInfo"]["name"] == "panini-cli"
    assert replies[1]["error"]["code"] == -32601


def test_auth_status_accepts_runtime_env_without_disclosing_key(tmp_path):
    (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-panini-private-test\n", encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": SOURCE, "PANINI_RUNTIME_ROOT": str(tmp_path)}
    env.pop("OPENAI_API_KEY", None)
    result = subprocess.run(
        [sys.executable, "-m", "panini_cli.agent", "auth", "status"],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == 0
    assert "configured" in result.stdout
    assert "sk-panini-private-test" not in result.stdout + result.stderr


def test_interactive_welcome_explains_identity_and_key_location(monkeypatch, tmp_path, capsys):
    from panini_cli.agent import cli as agent_cli
    from panini_cli.agent.config import Settings
    from rich.console import Console

    monkeypatch.setattr(agent_cli, "FOUNDER", "Charan")
    monkeypatch.setattr(agent_cli, "COMPANY", "PANINI")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    console = Console(highlight=False, force_terminal=False, color_system=None, width=120)
    agent_cli.print_welcome(console, Settings(model="gpt-test"), tmp_path, "ask before commands")
    output = capsys.readouterr().out
    assert "PANINI-CLI" in output
    assert "Built by Charan for PANINI" in output
    assert "Named for Panini" in output
    assert "panini-cli auth login" in output
    assert "Windows Credential Manager" in output


def test_env_default_approval_mode_is_respected(monkeypatch):
    from panini_cli.agent import cli as agent_cli
    from rich.console import Console

    monkeypatch.setenv("PANINI_CLI_APPROVAL_MODE", "auto")
    host = agent_cli.TerminalHost(Console(highlight=False, force_terminal=False, color_system=None))
    mode = agent_cli.default_approval_mode()
    if mode:
        host.set_mode(mode)
    assert host.mode == "auto"


def test_interactive_auth_commands_are_local_not_model_tasks(tmp_path):
    script = tmp_path / "model.json"
    script.write_text("[]", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    env = {
        **os.environ,
        "PYTHONPATH": SOURCE,
        "PANINI_CLI_SCRIPTED_MODEL": str(script),
        "NO_COLOR": "1",
    }
    completed = subprocess.run(
        [sys.executable, "-m", "panini_cli.agent", "-C", str(workspace)],
        input="auth status\nauth login\n/exit\n",
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    assert "OpenAI API key:" in completed.stdout
    assert "Use an interactive terminal" in completed.stdout
    assert "Scripted model is exhausted" not in completed.stdout + completed.stderr
    assert "OpenAI rejected the API key" not in completed.stdout


def test_power_command_enables_auto_approval(tmp_path):
    script = tmp_path / "model.json"
    script.write_text(json.dumps(STEPS), encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    env = {
        **os.environ,
        "PYTHONPATH": SOURCE,
        "PANINI_CLI_SCRIPTED_MODEL": str(script),
        "NO_COLOR": "1",
    }
    completed = subprocess.run(
        [sys.executable, "-m", "panini_cli.agent", "-C", str(workspace)],
        input="/power\nCreate notes.md\n/exit\n",
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    assert (workspace / "notes.md").read_text() == "# Notes\n"
    assert "Power mode enabled" in completed.stdout


def test_auth_login_does_not_save_rejected_key(monkeypatch, capsys):
    from panini_cli.agent import cli as agent_cli

    saved = []
    monkeypatch.setattr(agent_cli, "_validate_login_key", lambda key: (False, "rejected"))

    def save_key(key):
        saved.append(key)

    import panini_cli.agent.auth as auth

    monkeypatch.setattr(auth, "save_key", save_key)
    monkeypatch.setattr(sys, "stdin", type("Input", (), {"readline": lambda self: "sk-bad\n"})())
    assert agent_cli.main(["auth", "login", "--key-stdin"]) == 1
    assert saved == []
    assert "rejected" in capsys.readouterr().out


def test_auth_login_saves_verified_key(monkeypatch, capsys):
    from panini_cli.agent import cli as agent_cli

    saved = []
    monkeypatch.setattr(agent_cli, "_validate_login_key", lambda key: (True, "verified"))

    def save_key(key, *, target):
        assert target == auth.TARGET
        saved.append(key)

    import panini_cli.agent.auth as auth

    monkeypatch.setattr(auth, "save_key", save_key)
    monkeypatch.setattr(sys, "stdin", type("Input", (), {"readline": lambda self: "sk-good\n"})())
    assert agent_cli.main(["auth", "login", "--key-stdin"]) == 0
    assert saved == ["sk-good"]
    assert "verified" in capsys.readouterr().out


def test_credential_manager_failure_is_reported_without_traceback(monkeypatch, capsys):
    from panini_cli.agent import cli as agent_cli

    for failure in (
        OSError("Windows Credential Manager is unavailable"),
        UnicodeError("Invalid credential data"),
    ):

        def unavailable():
            raise failure

        monkeypatch.setattr(agent_cli, "load_panini_env", unavailable)
        assert agent_cli.main(["auth", "status"]) == 2
        output = capsys.readouterr().out
        assert "API key setup failed" in output
        assert str(failure) in output
        assert "Traceback" not in output


def test_sandbox_status_is_bundled_and_needs_no_api_key(monkeypatch, capsys):
    from panini_cli.agent import cli as agent_cli

    monkeypatch.setattr(agent_cli, "load_panini_env", lambda: (_ for _ in ()).throw(OSError("no key")))
    monkeypatch.setattr(agent_cli.native_sandbox, "available", lambda: (True, "available"))
    monkeypatch.setattr(agent_cli.native_probe, "verify_boundary", lambda: (True, "boundary passed"))
    assert agent_cli.main(["--sandbox", "native", "sandbox", "status"]) == 0
    output = capsys.readouterr().out
    assert "bundled in PANINI-CLI.exe" in output
    assert "boundary passed" in output
    assert "Docker Desktop and Codex CLI are not required" in output


def test_sandbox_status_fails_closed_when_boundary_fails(monkeypatch, capsys):
    from panini_cli.agent import cli as agent_cli

    monkeypatch.setattr(agent_cli.native_sandbox, "available", lambda: (True, "available"))
    monkeypatch.setattr(agent_cli.native_probe, "verify_boundary", lambda: (False, "outside write allowed"))
    assert agent_cli.main(["--sandbox", "native", "sandbox", "status"]) == 2
    assert "unavailable — outside write allowed" in capsys.readouterr().out


def test_plan_agents_md_sessions_and_resume(tmp_path, monkeypatch):
    monkeypatch.setenv("PANINI_DATA_DIR", str(tmp_path / "data"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "AGENTS.md").write_text("Always use tabs.", encoding="utf-8")
    steps = [
        {
            "calls": [
                {
                    "name": "update_plan",
                    "arguments": {
                        "steps": [
                            {"step": "Read the code", "status": "completed"},
                            {"step": "Fix the bug", "status": "in_progress"},
                        ]
                    },
                }
            ]
        },
        {"text": "Fixed."},
    ]
    completed, _ = cli(tmp_path, steps, "exec", "Fix the bug")
    assert completed.returncode == 0, completed.stderr
    assert "✓ Read the code" in completed.stdout and "› Fix the bug" in completed.stdout
    listed, _ = cli(tmp_path, [], "sessions")
    assert "Fix the bug" in listed.stdout
    record = next((tmp_path / "data" / "cli-sessions").glob("*.json"))
    saved = json.loads(record.read_text(encoding="utf-8"))
    assert saved["items"][0]["content"] == "Fix the bug"

    from panini_cli.agent.prompt import instructions

    assert "Always use tabs." in instructions(workspace)
    resumed, _ = cli(tmp_path, [{"text": "Still here."}], "resume")
    assert resumed.returncode == 0, resumed.stderr
    assert "Resumed: Fix the bug" in resumed.stdout


def test_jev_login_uses_separate_credential(monkeypatch, capsys):
    from panini_cli.agent import auth, cli as agent_cli

    saved = []
    monkeypatch.setattr(agent_cli, "_finish_jev_login", lambda console: 0)
    monkeypatch.setattr(auth, "save_key", lambda key, **kwargs: saved.append((key, kwargs["target"])))
    monkeypatch.setattr(agent_cli, "_validate_login_key", lambda key: (_ for _ in ()).throw(AssertionError("OpenAI validation called")))
    monkeypatch.setattr(sys, "stdin", type("Input", (), {"readline": lambda self: "gateway-secret\n"})())
    assert agent_cli.main(["auth", "login", "jev", "--key-stdin"]) == 0
    assert saved == [("gateway-secret", auth.PROVIDERS["jev"][2])]
    output = capsys.readouterr().out
    assert "saved securely" in output
    assert "gateway-secret" not in output


def test_interactive_jev_commands_are_handled_locally(monkeypatch):
    from panini_cli.agent import cli as agent_cli

    calls = []
    monkeypatch.setattr(agent_cli, "router_command", lambda args, console: calls.append(args.jev_command))
    for action in ("setup", "status", "check"):
        assert agent_cli._local_session_command(f"/jev {action}", None, None, None, None)
    assert calls == ["setup", "status", "check"]


def test_jev_login_installs_missing_sidecar_and_restarts(monkeypatch, tmp_path):
    from panini_cli.agent import cli as agent_cli
    from panini_cli.jev import client
    from rich.console import Console

    calls = []
    monkeypatch.setattr(client, "stop", lambda: calls.append("stop"))
    monkeypatch.setattr(client, "available", lambda: (False, "not installed"))
    monkeypatch.setattr(client, "sidecar_script", lambda: tmp_path / "missing.mjs")
    monkeypatch.setattr(agent_cli, "router_command", lambda args, console: calls.append(args.jev_command) or 0)
    assert agent_cli._finish_jev_login(Console()) == 0
    assert calls == ["stop", "setup"]


@pytest.mark.parametrize("interactive", [False, True])
def test_jev_logout_clears_credentials_environment_and_warm_router(monkeypatch, capsys, interactive):
    from panini_cli.agent import auth, cli
    from panini_cli.jev import client
    from rich.console import Console

    deleted, stopped = [], []
    monkeypatch.setenv(auth.PROVIDERS["jev"][1], "private-test-value")
    monkeypatch.setattr(auth, "delete_key", lambda *, target: deleted.append(target) or True)
    monkeypatch.setattr(client, "stop", lambda: stopped.append(True))
    if interactive:
        result = cli._interactive_auth(Console(), "auth logout jev")
    else:
        result = cli.main(["auth", "logout", "jev"])
    assert result == 0
    assert deleted == [auth.PROVIDERS["jev"][2]]
    assert stopped == [True]
    assert auth.PROVIDERS["jev"][1] not in os.environ
    assert "private-test-value" not in capsys.readouterr().out


@pytest.mark.parametrize("session", ["../escape", "invalid/name"])
def test_receipts_invalid_session_returns_controlled_error(capsys, session):
    from panini_cli.agent import cli

    assert cli.main(["receipts", session]) == 2
    assert "Receipt verification failed" in capsys.readouterr().out

