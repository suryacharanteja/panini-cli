import asyncio
import json
import os
import sys

import pytest
from panini_cli.agent import hooks
from panini_cli.agent.config import Settings
from panini_cli.agent.loop import Agent
from panini_cli.agent.model import ScriptedModel

GUARD = """
import json, sys
event = json.load(sys.stdin)
if event["tool"] == "run_command" and "rm " in event["arguments"]["command"]:
    print("Deleting files is not allowed in this repo.", file=sys.stderr)
    sys.exit(2)
print(f"checked {event['tool']}")
"""


def invoke(script):
    if os.name == "nt":
        return f'& "{sys.executable}" "{script}"'
    return f'"{sys.executable}" "{script}"'


class Host:
    def __init__(self):
        self.notes = []

    async def started(self, item):
        pass

    async def delta(self, *args):
        pass

    async def completed(self, item):
        pass

    async def approve(self, item):
        return "accept"

    async def hook_note(self, text):
        self.notes.append(text)


def write_hooks(path, config):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"hooks": config}), encoding="utf-8")


@pytest.fixture
def isolated_trust(tmp_path, monkeypatch):
    monkeypatch.setattr(hooks, "TRUST_FILE", tmp_path / "trusted.json")


def test_pre_hook_blocks_and_post_hook_adds_context(tmp_path, isolated_trust):
    script = tmp_path / "guard.py"
    script.write_text(GUARD, encoding="utf-8")
    user = tmp_path / "user-hooks.json"
    write_hooks(
        user,
        {
            "PreToolUse": [{"matcher": "run_command", "command": invoke(script)}],
            "PostToolUse": [{"matcher": "list_files", "command": invoke(script)}],
        },
    )
    workspace = tmp_path / "ws"
    workspace.mkdir()
    model = ScriptedModel(
        [
            {
                "calls": [
                    {
                        "name": "run_command",
                        "arguments": {"command": "rm -rf build", "timeout_seconds": None},
                    },
                    {"name": "list_files", "arguments": {"path": None, "depth": None}},
                ]
            },
            {"text": "ok"},
        ]
    )
    host = Host()
    agent = Agent(workspace, model, Settings(), host, hooks=hooks.Hooks(workspace, user_file=user))
    asyncio.run(agent.turn("clean"))
    outputs = [i["output"] for i in model.requests[-1]["items"] if i.get("type") == "function_call_output"]
    assert outputs[0].startswith("Blocked by the user's hook: Deleting files is not allowed")
    assert "checked list_files" in outputs[1]
    assert any("blocked run_command" in n for n in host.notes)


def test_project_hooks_need_trust_and_retrust_after_changes(tmp_path, isolated_trust):
    workspace = tmp_path / "ws"
    project = workspace / ".panini" / "hooks.json"
    write_hooks(project, {"TurnEnd": [{"command": "echo hi"}]})
    configured = hooks.Hooks(workspace, user_file=tmp_path / "none.json")
    assert configured.untrusted and configured.describe() == []
    hooks.trust(project)
    configured = hooks.Hooks(workspace, user_file=tmp_path / "none.json")
    assert not configured.untrusted and configured.describe()[0]["origin"] == "project"
    write_hooks(project, {"TurnEnd": [{"command": "echo changed"}]})
    assert hooks.Hooks(workspace, user_file=tmp_path / "none.json").untrusted


def test_session_start_and_turn_end_hooks(tmp_path, isolated_trust):
    user = tmp_path / "user-hooks.json"
    write_hooks(
        user, {"SessionStart": [{"command": "echo branch-main"}], "TurnEnd": [{"command": "echo diffstat"}]}
    )
    workspace = tmp_path / "ws"
    workspace.mkdir()
    model = ScriptedModel([{"text": "hello"}])
    host = Host()
    agent = Agent(workspace, model, Settings(), host, hooks=hooks.Hooks(workspace, user_file=user))
    asyncio.run(agent.turn("hi"))
    assert "branch-main" in model.requests[0]["items"][0]["content"]
    assert any("diffstat" in n for n in host.notes)
