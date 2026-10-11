"""Phase 1 (docs/AUDIT-2026-10-01.md §3): approvals, compaction, exec automation, config, AGENTS.md."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from panini_cli.agent import approvals, compaction, prompt, userconfig
from panini_cli.agent.config import Settings
from panini_cli.agent.loop import Agent, has_image, image_part
from panini_cli.agent.model import ScriptedModel

SOURCE = str(Path(__file__).resolve().parents[1] / "src")


@pytest.fixture
def anyio_backend():
    return "asyncio"


# --- trusted read-only commands -------------------------------------------------------------

@pytest.mark.parametrize(
    "command",
    [
        "git status",
        "git diff --stat",
        "git log --oneline -5",
        "git branch -a",
        "Get-ChildItem src",
        "Get-Content README.md | Select-Object -First 20",
        "rg TODO src",
        "python --version",
        "ls; git status --short",
    ],
)
def test_read_only_commands_are_trusted(command):
    assert approvals.is_trusted(command)


@pytest.mark.parametrize(
    "command",
    [
        "git commit -m x",
        "git push",
        "git branch -D main",
        "git diff --output=x.txt",
        "rm -rf build",
        "Remove-Item x",
        "Get-Content ../secret.txt",
        "Get-Content C:\\Users\\me\\.ssh\\id_rsa",
        "cat ~/.ssh/id_rsa",
        "Get-Content $env:USERPROFILE\\x",
        "ls > files.txt",
        "Get-ChildItem | ForEach-Object { Remove-Item $_ }",
        "echo $(whoami)",
        "npm install",
        "python script.py",
        "find . -delete",
        "rg --pre evil TODO",
        "git -C .. status",
        "",
    ],
)
def test_anything_that_writes_runs_code_or_leaves_the_workspace_is_not(command):
    assert not approvals.is_trusted(command)


def _host(**options):
    from rich.console import Console

    from panini_cli.agent.cli import TerminalHost

    return TerminalHost(Console(file=open(os.devnull, "w", encoding="utf-8")), **options)


@pytest.mark.anyio
async def test_terminal_host_runs_trusted_commands_without_asking():
    host = _host()
    assert await host.approve({"type": "commandExecution", "command": "git status", "executionScope": "workspace"}) == "accept"
    # git at host scope is not trusted: a repository's git config can run programs.
    assert await host.approve({"type": "commandExecution", "command": "git status", "executionScope": "host"}) == "decline"
    assert await host.approve({"type": "commandExecution", "command": "Get-ChildItem", "executionScope": "host"}) == "decline"
    assert await host.approve({"type": "commandExecution", "command": "npm test", "executionScope": "workspace"}) == "decline"


@pytest.mark.anyio
async def test_read_only_mode_still_allows_sandboxed_reads():
    host = _host()
    host.set_mode("read-only")
    assert await host.approve({"type": "commandExecution", "command": "git diff", "executionScope": "workspace"}) == "accept"
    assert await host.approve({"type": "commandExecution", "command": "npm test", "executionScope": "workspace"}) == "decline"


@pytest.mark.anyio
async def test_session_rules_remember_always_answers():
    host = _host()
    host.rules.allow("npm test")
    assert await host.approve({"type": "commandExecution", "command": "npm test", "executionScope": "workspace"}) == "accept"


# --- compaction -----------------------------------------------------------------------------

class Host:
    def __init__(self):
        self.compactions = []

    async def started(self, item):
        pass

    async def delta(self, *args):
        pass

    async def completed(self, item):
        pass

    async def approve(self, item):
        return "decline"

    async def compacted(self, *args):
        self.compactions.append(args)


@pytest.mark.anyio
async def test_long_history_is_compacted_before_the_next_message(tmp_path, monkeypatch):
    monkeypatch.setenv("PANINI_CLI_LOCAL", "off")  # summarize with the session model
    model = ScriptedModel([{"text": "## Goal\n- build x"}, {"text": "Next answer."}])
    host = Host()
    agent = Agent(tmp_path, model, Settings(sandbox="none", context_tokens=20_000), host)
    for index in range(20):
        agent.items.append({"role": "user", "content": f"message {index} " + "x" * 4000})
    result = await agent.turn("continue")
    assert result.status == "completed" and result.text == "Next answer."
    assert agent.items[0]["role"] == "developer" and "build x" in agent.items[0]["content"]
    assert agent.items[1]["content"].startswith("continue")
    assert host.compactions and host.compactions[0][0] == "auto"
    # The summarizer saw the transcript, not the tools.
    assert "Transcript:" in model.requests[0]["items"][0]["content"]


def test_transcript_keeps_the_most_recent_part():
    items = [{"role": "user", "content": "old " * 30_000}, {"role": "user", "content": "THE END"}]
    text = compaction.transcript(items)
    assert text.endswith("THE END") and len(text) <= compaction.MAX_TRANSCRIPT_CHARS


# --- config.toml ----------------------------------------------------------------------------

def test_profiles_fill_unset_environment_only(tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    config.write_text(
        'model = "auto"\napproval_mode = "ask"\nrouter_models = ["a:cheap:x", {id = "b", tier = "strong", description = "y"}]\n'
        '[profiles.offline]\nmodel = "local"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("PANINI_CONFIG", str(config))
    for name in ("PANINI_CLI_MODEL", "PANINI_CLI_APPROVAL_MODE", "PANINI_CLI_ROUTER_MODELS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PANINI_CLI_APPROVAL_MODE", "edits")  # the environment wins
    userconfig.apply("offline")
    assert os.environ["PANINI_CLI_MODEL"] == "local"
    assert os.environ["PANINI_CLI_APPROVAL_MODE"] == "edits"
    assert os.environ["PANINI_CLI_ROUTER_MODELS"] == "a:cheap:x,b:strong:y"


def test_unknown_profile_is_an_error(tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    config.write_text("[profiles.a]\nmodel = 'x'\n", encoding="utf-8")
    monkeypatch.setenv("PANINI_CONFIG", str(config))
    with pytest.raises(ValueError, match="Unknown profile"):
        userconfig.apply("missing")


# --- AGENTS.md hierarchy --------------------------------------------------------------------

def test_agents_md_from_repo_root_to_workspace_and_nested_files_listed(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    workspace = repo / "packages" / "app"
    (workspace / "src" / "db").mkdir(parents=True)
    (repo / "AGENTS.md").write_text("root rule")
    (workspace / "AGENTS.md").write_text("app rule")
    (workspace / "src" / "db" / "AGENTS.md").write_text("db rule")
    text = prompt.project_instructions(workspace)
    assert text.index("root rule") < text.index("app rule")
    assert "src/db/AGENTS.md" in text and "db rule" not in text


# --- images ---------------------------------------------------------------------------------

def test_images_become_input_image_parts(tmp_path):
    picture = tmp_path / "shot.png"
    picture.write_bytes(b"\x89PNG\r\n\x1a\n")
    part = image_part(picture)
    assert part["type"] == "input_image" and part["image_url"].startswith("data:image/png;base64,")
    assert has_image([{"type": "input_text", "text": "x"}, part])
    with pytest.raises(ValueError):
        image_part(tmp_path / "notes.txt")


# --- exec automation ------------------------------------------------------------------------

def run_exec(tmp_path, steps, *arguments, stdin=None):
    script = tmp_path / "model.json"
    script.write_text(json.dumps(steps), encoding="utf-8")
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    env = {**os.environ, "PYTHONPATH": SOURCE, "PANINI_CLI_SCRIPTED_MODEL": str(script), "NO_COLOR": "1"}
    return subprocess.run(
        [sys.executable, "-m", "panini_cli.agent", "exec", *arguments, "-C", str(workspace)],
        env=env, input=stdin, capture_output=True, text=True, encoding="utf-8", timeout=120,
    )


def test_exec_json_prints_one_event_per_line(tmp_path):
    steps = [{"calls": [{"name": "list_files", "arguments": {"path": None, "depth": None}}]}, {"text": "All good."}]
    completed = run_exec(tmp_path, steps, "--json", "list the files")
    assert completed.returncode == 0, completed.stderr
    events = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
    kinds = [event["type"] for event in events]
    assert "item.started" in kinds and kinds[-1] == "turn.completed"
    assert events[-1]["status"] == "completed" and events[-1]["text"] == "All good."


def test_exec_reads_the_task_from_stdin_and_passes_the_schema(tmp_path):
    schema = tmp_path / "schema.json"
    schema.write_text(json.dumps({"type": "object", "properties": {"ok": {"type": "boolean"}}}), encoding="utf-8")
    completed = run_exec(tmp_path, [{"text": '{"ok": true}'}], "--json", "--output-schema", str(schema), "-", stdin="from stdin")
    assert completed.returncode == 0, completed.stderr
    final = json.loads(completed.stdout.splitlines()[-1])
    assert final["text"] == '{"ok": true}'


def test_exec_json_declines_unapproved_changes(tmp_path):
    steps = [{"calls": [{"name": "write_file", "arguments": {"path": "a.txt", "content": "x"}}]}, {"text": "Done."}]
    completed = run_exec(tmp_path, steps, "--json", "write a")
    events = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
    assert any(e["type"] == "approval" and e["decision"] == "decline" for e in events)
    assert not (tmp_path / "ws" / "a.txt").exists()
