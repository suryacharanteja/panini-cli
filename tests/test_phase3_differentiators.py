"""Phase 3 (docs/AUDIT-2026-10-01.md §3.4, §4.2, §4.4, §4.5)."""

import subprocess

import pytest

from panini_cli.agent import complexity, gitops, ollama, scout, verify
from panini_cli.agent.config import Settings
from panini_cli.agent.loop import Agent
from panini_cli.agent.model import ModelTurn, RoutedModel, ScriptedModel


@pytest.fixture
def anyio_backend():
    return "asyncio"


class Host:
    def __init__(self):
        self.routes = []

    async def started(self, item):
        pass

    async def delta(self, *args):
        pass

    async def completed(self, item):
        pass

    async def approve(self, item):
        return "accept"

    async def model_route(self, decision):
        self.routes.append(decision)


def git_repo(path):
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    (path / "keep.txt").write_text("keep\n")
    (path / "app.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"], cwd=path, check=True)


# --- git checkpoints ------------------------------------------------------------------------

def test_snapshot_never_touches_the_users_index_or_branch(tmp_path):
    git_repo(tmp_path)
    (tmp_path / "new.txt").write_text("untracked\n")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True).stdout
    sha = gitops.snapshot(tmp_path, "test/one")
    status = subprocess.run(["git", "status", "--short"], cwd=tmp_path, capture_output=True, text=True).stdout
    assert sha and "?? new.txt" in status  # still untracked: the user's index is unchanged
    assert subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True).stdout == head
    assert gitops.changed_paths(tmp_path, head.strip(), sha) == ["new.txt"]


def test_snapshot_outside_a_repository_is_none(tmp_path):
    assert gitops.snapshot(tmp_path) is None


@pytest.mark.anyio
async def test_undo_reverts_command_side_effects_and_new_files(tmp_path):
    git_repo(tmp_path)
    # The "command" modifies app.py and creates out.txt — side effects the file tools never saw.
    script = (
        "python -c \"import pathlib; pathlib.Path('app.py').write_text('x = 2\\n'); "
        "pathlib.Path('out.txt').write_text('o')\""
    )
    model = ScriptedModel([{"calls": [{"name": "run_command", "arguments": {"command": script, "timeout_seconds": 60, "scope": None}}]},
                           {"text": "done"}])
    agent = Agent(tmp_path, model, Settings(sandbox="none"), Host())
    await agent.turn("change things")
    assert (tmp_path / "app.py").read_text() == "x = 2\n" and (tmp_path / "out.txt").exists()
    assert "app.py" in agent.session_diff()
    restored, skipped = agent.undo()
    assert (tmp_path / "app.py").read_text() == "x = 1\n"
    assert not (tmp_path / "out.txt").exists()
    assert "app.py" in restored and not skipped


@pytest.mark.anyio
async def test_undo_skips_files_edited_after_the_turn(tmp_path):
    git_repo(tmp_path)
    model = ScriptedModel([{"calls": [{"name": "write_file", "arguments": {"path": "app.py", "content": "x = 3\n"}}]},
                           {"text": "done"}])
    agent = Agent(tmp_path, model, Settings(sandbox="none"), Host())
    await agent.turn("edit")
    (tmp_path / "app.py").write_text("x = 99  # my own edit\n")
    restored, skipped = agent.undo()
    assert skipped == ["app.py"] and (tmp_path / "app.py").read_text().startswith("x = 99")


# --- verification-first ---------------------------------------------------------------------

def test_verification_commands_are_detected(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n[tool.ruff]\n")
    (tmp_path / "package.json").write_text('{"scripts": {"test": "vitest", "lint": "eslint ."}}')
    assert verify.commands(tmp_path) == ["python -m pytest -q", "ruff check .", "npm test", "npm run lint"]
    empty = tmp_path / "empty"
    empty.mkdir()
    assert verify.commands(empty) == []


@pytest.mark.anyio
async def test_finishing_after_a_change_without_running_checks_gets_one_reminder(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    steps = [
        {"calls": [{"name": "write_file", "arguments": {"path": "a.py", "content": "x = 1\n"}}]},
        {"text": "Done, all verified."},
        {"text": "I could not run the tests here."},
    ]
    model = ScriptedModel(steps)
    agent = Agent(tmp_path, model, Settings(sandbox="none"), Host())
    result = await agent.turn("write a.py")
    assert result.text == "I could not run the tests here."
    reminder = [i for i in model.requests[-1]["items"] if i.get("role") == "developer"]
    assert reminder and "python -m pytest -q" in reminder[0]["content"] and "a.py" in reminder[0]["content"]


@pytest.mark.anyio
async def test_no_reminder_when_a_command_ran_after_the_change(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    steps = [
        {"calls": [{"name": "write_file", "arguments": {"path": "a.py", "content": "x = 1\n"}}]},
        {"calls": [{"name": "run_command", "arguments": {"command": "python -c \"print(1)\"", "timeout_seconds": 60, "scope": None}}]},
        {"text": "Done."},
    ]
    agent = Agent(tmp_path, ScriptedModel(steps), Settings(sandbox="none"), Host())
    result = await agent.turn("write a.py")
    assert result.text == "Done." and result.steps == 3


# --- the local tier: questions only, honest answers ---------------------------------------

def test_edit_requests_never_route_to_the_local_model():
    for prompt in ("rename foo to bar", "fix the typo", "write a hello script", "add a docstring"):
        assert complexity.estimate(prompt).tier != "local", prompt
    assert complexity.estimate("what does main.py do?").tier == "local"


def test_narrating_a_tool_call_is_an_unusable_local_turn():
    turn = ModelTurn([], "I need to search calc.py for the add() function.", [])
    assert "described a tool call" in ollama.invalid_turn(turn)
    assert ollama.invalid_turn(ModelTurn([], "add() returns a - b.", [])) is None


class LocalThenCloud(ScriptedModel):
    """Scripted, but behaves like a RoutedModel that starts on the local model."""

    def __init__(self, steps):
        super().__init__(steps)
        self.model, self.escalate, self.local_blocked, self.escalation = "ollama/qwen2.5:3b-instruct", True, False, None

    def switch_model(self, model):
        self.model = model

    force_escalate = RoutedModel.force_escalate


@pytest.mark.anyio
async def test_a_local_claim_of_unmade_changes_is_redone_in_the_cloud(tmp_path, monkeypatch):
    from panini_cli.agent import loop, model_router

    monkeypatch.setattr(loop.model_router, "route", lambda *a, **k: model_router.RouteDecision(a[1], "x", False))
    model = LocalThenCloud([{"text": "The function has been renamed everywhere."}, {"text": "Cloud did it properly."}])
    host = Host()
    agent = Agent(tmp_path, model, Settings(model="auto", sandbox="none"), host)
    result = await agent.turn("what is going on")
    assert result.text == "Cloud did it properly."
    assert not model.model.startswith("ollama/") and model.local_blocked
    assert not any("has been renamed" in str(item.get("content")) for item in agent.items)
    assert any(route.reason.startswith("escalated:") for route in host.routes)


def test_local_model_gets_a_reduced_tool_set():
    from panini_cli.agent import tools

    names = {tool["name"] for tool in tools.TOOLS if tool["name"] in tools.LOCAL_TOOLS}
    assert "apply_patch" not in names and "terminal_start" not in names and "read_file" in names


# --- local scout ----------------------------------------------------------------------------

@pytest.mark.anyio
async def test_explore_runs_a_read_only_scout_and_reports_back(tmp_path, monkeypatch):
    (tmp_path / "retry.py").write_text("def retry():\n    pass\n")
    captured = {}

    async def fake_explore(workspace, settings, question, path=None):
        captured.update(workspace=workspace, question=question)
        return "[Local scout report] retry.py:1 defines retry()"

    monkeypatch.setattr(scout, "explore", fake_explore)
    steps = [{"calls": [{"name": "explore", "arguments": {"question": "Where is retry?", "path": None}}]}, {"text": "ok"}]
    model = ScriptedModel(steps)
    agent = Agent(tmp_path, model, Settings(sandbox="none"), Host())
    await agent.turn("find retry")
    outputs = [i["output"] for i in model.requests[-1]["items"] if i.get("type") == "function_call_output"]
    assert "retry.py:1" in outputs[0] and captured["question"] == "Where is retry?"


def test_explore_is_offered_only_to_routed_cloud_models(tmp_path, monkeypatch):
    monkeypatch.setattr(scout, "available", lambda: True)

    class M:
        model = "gpt-5.6-terra"

    agent = Agent(tmp_path, M(), Settings(model="auto", sandbox="none"), Host())
    assert "explore" in {t["name"] for t in agent.tool_defs()}
    M.model = "ollama/qwen2.5:3b-instruct"
    assert "explore" not in {t["name"] for t in agent.tool_defs()}
    pinned = Agent(tmp_path, M(), Settings(model="gpt-5.6-terra", sandbox="none"), Host())
    assert "explore" not in {t["name"] for t in pinned.tool_defs()}


def test_the_scout_itself_only_has_read_tools(tmp_path):
    agent = Agent(tmp_path, ScriptedModel([]), Settings(sandbox="none"), Host())
    agent.allowed_tools = scout.SCOUT_TOOLS
    assert {t["name"] for t in agent.tool_defs()} == set(scout.SCOUT_TOOLS)


@pytest.mark.anyio
async def test_a_local_answer_about_an_unread_file_is_redone_in_the_cloud(tmp_path, monkeypatch):
    from panini_cli.agent import loop, model_router

    monkeypatch.setattr(loop.model_router, "route", lambda *a, **k: model_router.RouteDecision(a[1], "x", False))
    model = LocalThenCloud([{"text": "add() returns the sum."}, {"text": "add() returns a - b."}])
    agent = Agent(tmp_path, model, Settings(model="auto", sandbox="none"), Host())
    result = await agent.turn("What does add() in calc.py return?")
    assert result.text == "add() returns a - b." and model.local_blocked


@pytest.mark.anyio
async def test_a_local_answer_after_reading_the_file_stands(tmp_path, monkeypatch):
    from panini_cli.agent import loop, model_router

    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    monkeypatch.setattr(loop.model_router, "route", lambda *a, **k: model_router.RouteDecision(a[1], "x", False))
    steps = [{"calls": [{"name": "read_file", "arguments": {"path": "calc.py"}}]}, {"text": "It returns a - b."}]
    model = LocalThenCloud(steps)
    agent = Agent(tmp_path, model, Settings(model="auto", sandbox="none"), Host())
    result = await agent.turn("What does add() in calc.py return?")
    assert result.text == "It returns a - b." and model.model.startswith("ollama/")
