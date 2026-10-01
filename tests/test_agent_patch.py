import asyncio

import pytest
from panini_cli.agent import codex_sandbox, native_sandbox, patch, sandbox
from panini_cli.agent.config import Settings
from panini_cli.agent.loop import Agent
from panini_cli.agent.model import ScriptedModel
from panini_cli.agent.tools import ToolError, Workspace


class Host:
    def __init__(self, decision="accept"):
        self.decision = decision
        self.items = []

    async def started(self, item):
        pass

    async def delta(self, item_id, kind, text):
        pass

    async def completed(self, item):
        self.items.append(item)

    async def approve(self, item):
        return self.decision


PATCH = """*** Begin Patch
*** Add File: new.txt
+hello
*** Update File: app.py
@@ def main
 def main():
-    return 1
+    return 2
*** Delete File: old.txt
*** End Patch"""


def setup(tmp_path):
    (tmp_path / "app.py").write_text("import os\n\n\ndef main():\n    return 1\n", encoding="utf-8")
    (tmp_path / "old.txt").write_text("bye\n", encoding="utf-8")
    return Workspace(tmp_path)


def test_patch_applies_all_changes(tmp_path):
    ws = setup(tmp_path)
    changes = patch.prepare(ws, PATCH)
    assert [c.kind for c in changes] == ["add", "update", "delete"]
    patch.apply(changes)
    assert (tmp_path / "new.txt").read_text() == "hello\n"
    assert "return 2" in (tmp_path / "app.py").read_text()
    assert not (tmp_path / "old.txt").exists()


def test_patch_is_all_or_nothing(tmp_path):
    ws = setup(tmp_path)
    changes = patch.prepare(ws, PATCH)
    (tmp_path / "old.txt").write_text("edited meanwhile\n", encoding="utf-8")
    with pytest.raises(ToolError):
        patch.apply(changes)
    assert not (tmp_path / "new.txt").exists()  # The earlier change was rolled back.
    assert "return 1" in (tmp_path / "app.py").read_text()


@pytest.mark.parametrize(
    "bad",
    [
        "no envelope",
        "*** Begin Patch\n*** Update File: app.py\n-    return 9\n+x\n*** End Patch",
        "*** Begin Patch\n*** Update File: ../outside.py\n x\n*** End Patch",
        "*** Begin Patch\n*** Add File: .env\n+KEY=1\n*** End Patch",
    ],
)
def test_patch_rejects_invalid_input(tmp_path, bad):
    ws = setup(tmp_path)
    with pytest.raises(ToolError):
        patch.prepare(ws, bad)


def test_agent_patch_then_undo(tmp_path):
    setup(tmp_path)
    model = ScriptedModel(
        [{"calls": [{"name": "apply_patch", "arguments": {"patch": PATCH}}]}, {"text": "Done."}]
    )
    agent = Agent(tmp_path, model, Settings(), Host())
    result = asyncio.run(agent.turn("change things"))
    assert result.status == "completed"
    assert (tmp_path / "new.txt").exists()
    restored, skipped = agent.undo()
    assert sorted(restored) == ["app.py", "new.txt", "old.txt"] and skipped == []
    assert not (tmp_path / "new.txt").exists()
    assert (tmp_path / "old.txt").read_text() == "bye\n"
    assert "return 1" in (tmp_path / "app.py").read_text()
    assert agent.undo() == ([], [])


def test_undo_skips_files_edited_since(tmp_path):
    setup(tmp_path)
    model = ScriptedModel(
        [
            {"calls": [{"name": "write_file", "arguments": {"path": "a.txt", "content": "one\n"}}]},
            {"text": "ok"},
        ]
    )
    agent = Agent(tmp_path, model, Settings(), Host())
    asyncio.run(agent.turn("write"))
    (tmp_path / "a.txt").write_text("user edit\n", encoding="utf-8")
    assert agent.undo() == ([], ["a.txt"])
    assert (tmp_path / "a.txt").read_text() == "user edit\n"


def test_declined_patch_changes_nothing(tmp_path):
    setup(tmp_path)
    model = ScriptedModel(
        [{"calls": [{"name": "apply_patch", "arguments": {"patch": PATCH}}]}, {"text": "ok"}]
    )
    asyncio.run(Agent(tmp_path, model, Settings(), Host("decline")).turn("x"))
    assert not (tmp_path / "new.txt").exists() and (tmp_path / "old.txt").exists()


def test_sandbox_argv_isolates_the_command(tmp_path):
    argv = sandbox.argv("pytest -q", tmp_path, name="panini-cli-test")
    joined = " ".join(argv)
    assert "--network none" in joined and "--cap-drop ALL" in joined
    assert f"{tmp_path}:/workspace" in argv and argv[-3:] == ["sh", "-c", "pytest -q"]
    assert "no-new-privileges" in argv and "--pids-limit" in argv


def test_sandboxed_agent_runs_commands_through_docker(tmp_path, monkeypatch):
    seen = {}

    async def fake_run(command, cwd, timeout, on_output=None, environ=None, argv=None):
        seen["argv"] = argv
        from panini_cli.agent.shell import CommandResult

        return CommandResult(0, "ok", 0.1, False, 0)

    removed = []
    monkeypatch.setattr("panini_cli.agent.loop.run_command", fake_run)
    monkeypatch.setattr(sandbox, "remove", removed.append)
    model = ScriptedModel(
        [
            {"calls": [{"name": "run_command", "arguments": {"command": "ls", "timeout_seconds": None}}]},
            {"text": "ok"},
        ]
    )
    agent = Agent(tmp_path, model, Settings(sandbox="docker"), Host())
    asyncio.run(agent.turn("list"))
    assert seen["argv"][1] == "run" and seen["argv"][-1] == "ls"
    assert removed and removed[0].startswith("panini-cli-")
    assert "Linux container" in agent.instructions


def test_agent_routes_approved_commands_through_native_sandbox(tmp_path, monkeypatch):
    seen = {}

    async def fake_run(command, cwd, timeout, on_output=None, environ=None, argv=None):
        seen["argv"] = argv
        from panini_cli.agent.shell import CommandResult

        return CommandResult(0, "ok", 0.1, False, 0)

    monkeypatch.setattr("panini_cli.agent.loop.run_command", fake_run)
    monkeypatch.setattr(codex_sandbox, "executable", lambda: "codex.exe")
    model = ScriptedModel(
        [
            {
                "calls": [
                    {
                        "name": "run_command",
                        "arguments": {"command": "Write-Output ok", "timeout_seconds": None},
                    }
                ]
            },
            {"text": "ok"},
        ]
    )
    agent = Agent(tmp_path, model, Settings(sandbox="codex"), Host())
    asyncio.run(agent.turn("run"))
    assert seen["argv"][:6] == ["codex.exe", "sandbox", "-P", ":workspace", "-C", str(tmp_path)]
    assert "Codex native Windows workspace sandbox" in agent.instructions


def test_agent_routes_native_mode_to_panini_runner(tmp_path, monkeypatch):
    seen = {}

    async def fake_native(command, workspace, timeout, on_output=None):
        seen.update(command=command, workspace=workspace)
        from panini_cli.agent.shell import CommandResult

        return CommandResult(0, "ok", 0.1, False, 0)

    monkeypatch.setattr(native_sandbox, "run_command", fake_native)
    model = ScriptedModel(
        [
            {
                "calls": [
                    {
                        "name": "run_command",
                        "arguments": {"command": "Write-Output ok", "timeout_seconds": None},
                    }
                ]
            },
            {"text": "ok"},
        ]
    )
    agent = Agent(tmp_path, model, Settings(sandbox="native"), Host())
    asyncio.run(agent.turn("run"))
    assert seen == {"command": "Write-Output ok", "workspace": tmp_path}
    assert "PANINI's Windows AppContainer sandbox" in agent.instructions
    assert "scope=host" in agent.instructions
