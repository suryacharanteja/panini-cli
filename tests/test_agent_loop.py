import asyncio
import os
import sys

import pytest
from panini_cli.agent.config import Settings
from panini_cli.agent.loop import DECLINED, INTERRUPTED, OMITTED, SKIPPED, Agent
from panini_cli.agent.model import ScriptedModel


@pytest.fixture
def anyio_backend():
    return "asyncio"


class Host:
    def __init__(self, decisions=()):
        self.decisions = list(decisions)
        self.events = []
        self.asked = []
        self.routes = []

    async def started(self, item):
        self.events.append(("started", item["type"], item.get("status")))

    async def delta(self, item_id, kind, text):
        self.events.append(("delta", kind, text))

    async def completed(self, item):
        self.events.append(("completed", item["type"], item.get("status")))

    async def approve(self, item):
        self.asked.append(item)
        return self.decisions.pop(0) if self.decisions else "decline"

    async def model_route(self, decision):
        self.routes.append(decision)


def call(name, **arguments):
    return {"name": name, "arguments": arguments}


def outputs(model):
    """The tool outputs the model saw in its latest request."""
    return [
        item["output"] for item in model.requests[-1]["items"] if item.get("type") == "function_call_output"
    ]


def agent(tmp_path, steps, host, **settings):
    model = ScriptedModel(steps)
    settings.setdefault("sandbox", "none")  # These tests exercise local command semantics.
    return Agent(tmp_path, model, Settings(**settings), host), model


class SwitchableModel(ScriptedModel):
    def __init__(self, steps, model="gpt-4.1"):
        super().__init__(steps)
        self.model = model

    def switch_model(self, model):
        self.model = model


@pytest.mark.anyio
async def test_reads_edits_and_commands_flow_through_approval(tmp_path):
    (tmp_path / "app.py").write_text("print('old')\n")
    command = f"& '{sys.executable}' app.py" if os.name == "nt" else f"'{sys.executable}' app.py"
    host = Host(["accept", "accept"])
    worker, model = agent(
        tmp_path,
        [
            {"text": "Looking.", "calls": [call("read_file", path="app.py", start_line=None, end_line=None)]},
            {"calls": [call("edit_file", path="app.py", old_text="old", new_text="new", replace_all=None)]},
            {"calls": [call("run_command", command=command, timeout_seconds=30)]},
            {"text": "Changed app.py and ran it: it prints new."},
        ],
        host,
    )
    result = await worker.turn("Make app.py print new and run it.")
    assert result.status == "completed" and result.text.startswith("Changed app.py")
    assert (tmp_path / "app.py").read_text() == "print('new')\n"
    assert [item["type"] for item in host.asked] == ["fileChange", "commandExecution"]
    assert "Exit code: 0" in outputs(model)[-1] and "new" in outputs(model)[-1]
    assert "Updated app.py (+1 -1 lines)." in outputs(model)
    assert ("delta", "agentMessage", "Looking.") in host.events
    kinds = [kind for event, kind, _ in host.events if event == "completed"]
    assert kinds.count("dynamicToolCall") == 1 and kinds.count("agentMessage") == 2


@pytest.mark.anyio
async def test_auto_model_routes_before_the_model_turn(monkeypatch, tmp_path):
    from panini_cli.agent import loop, model_router

    host = Host()
    model = SwitchableModel([{"text": "Done."}], model="gpt-4.1")

    def route(prompt, current, context_tokens, allow_local=True):
        assert prompt == "Run a quick check."
        assert current == "gpt-4.1"
        assert context_tokens > 0
        return model_router.RouteDecision("gpt-4.1-mini", "jev", True, confidence=0.87, cost_usd=0.00001)

    monkeypatch.setattr(loop.model_router, "route", route)
    worker = Agent(tmp_path, model, Settings(model="auto", sandbox="none"), host)
    result = await worker.turn("Run a quick check.")
    assert result.status == "completed"
    assert model.model == "gpt-4.1-mini"
    assert host.routes[0].model == "gpt-4.1-mini"


@pytest.mark.anyio
async def test_local_machine_requests_get_host_context_hint(tmp_path):
    host = Host()
    worker, model = agent(tmp_path, [{"text": "Done."}], host)
    result = await worker.turn("check downloads folder")
    assert result.status == "completed"
    prompt = model.requests[0]["items"][0]["content"]
    assert "%USERPROFILE%\\Downloads" in prompt
    assert "host-scoped PowerShell" in prompt


@pytest.mark.anyio
async def test_a_declined_action_skips_the_rest_of_the_step_and_changes_nothing(tmp_path):
    host = Host(["decline"])
    worker, model = agent(
        tmp_path,
        [
            {
                "calls": [
                    call("write_file", path="a.txt", content="A"),
                    call("write_file", path="b.txt", content="B"),
                ]
            },
            {"text": "Understood; I will not create those files."},
        ],
        host,
    )
    result = await worker.turn("Create two files.")
    assert result.status == "completed"
    assert not (tmp_path / "a.txt").exists() and not (tmp_path / "b.txt").exists()
    assert outputs(model) == [DECLINED, SKIPPED] and len(host.asked) == 1


@pytest.mark.anyio
async def test_cancel_at_approval_ends_the_turn(tmp_path):
    host = Host(["cancel"])
    worker, model = agent(tmp_path, [{"calls": [call("write_file", path="a.txt", content="A")]}], host)
    result = await worker.turn("Create a file.")
    assert result.status == "interrupted" and not (tmp_path / "a.txt").exists()
    # The conversation stays valid for a follow-up request.
    assert worker.items[-1] == {
        "type": "function_call_output",
        "call_id": "call_scripted_1_0",
        "output": INTERRUPTED,
    }


@pytest.mark.anyio
async def test_interrupting_a_running_command_repairs_the_conversation(tmp_path):
    command = f"& '{sys.executable}' -c 'import time; time.sleep(30)'" if os.name == "nt" else "sleep 30"
    host = Host(["accept"])
    worker, model = agent(
        tmp_path,
        [{"calls": [call("run_command", command=command, timeout_seconds=60)]}, {"text": "ok"}],
        host,
    )
    task = asyncio.create_task(worker.turn("Wait."))
    await asyncio.sleep(3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert worker.items[-1]["output"] == INTERRUPTED
    assert (await worker.turn("Continue.")).status == "completed"


@pytest.mark.anyio
async def test_bad_tool_calls_are_reported_to_the_model(tmp_path):
    host = Host()
    worker, model = agent(
        tmp_path,
        [
            {
                "calls": [
                    {"name": "read_file", "arguments": "not json"},
                    {"name": "delete_everything", "arguments": {}},
                    call("read_file", path="../escape.txt", start_line=None, end_line=None),
                ]
            },
            {"text": "Recovered."},
        ],
        host,
    )
    assert (await worker.turn("Go.")).status == "completed"
    seen = outputs(model)
    assert "not a JSON object" in seen[0] and "no tool named delete_everything" in seen[1]
    assert "outside the workspace" in seen[2]


@pytest.mark.anyio
async def test_step_budget_and_model_errors_end_the_turn_honestly(tmp_path):
    host = Host()
    endless = [{"calls": [call("list_files", path=None, depth=None)]}] * 5
    worker, _ = agent(tmp_path, endless, host, max_steps=3)
    result = await worker.turn("Loop.")
    assert result.status == "incomplete" and "3 steps" in result.error
    failing, _ = agent(tmp_path, [{"error": "The model failed: overloaded"}], host)
    result = await failing.turn("Anything.")
    assert result.status == "failed" and "overloaded" in result.error


@pytest.mark.anyio
async def test_old_tool_output_is_trimmed_to_fit_the_context(tmp_path):
    (tmp_path / "big.txt").write_text(("x" * 100 + "\n") * 900)  # About 97,000 characters per read.
    reads = [{"calls": [call("read_file", path="big.txt", start_line=None, end_line=None)]}] * 9
    worker, model = agent(tmp_path, [*reads, {"text": "done"}], Host(), context_tokens=20_000)
    assert (await worker.turn("Read it repeatedly.")).status == "completed"
    seen = outputs(model)
    assert seen[0] == OMITTED and seen[-1] != OMITTED


@pytest.mark.anyio
async def test_routing_happens_once_per_user_message_not_per_tool_step(monkeypatch, tmp_path):
    from panini_cli.agent import loop, model_router

    (tmp_path / "a.txt").write_text("hi")
    seen = []

    def route(prompt, current, context_tokens, allow_local=True):
        seen.append(prompt)
        return model_router.RouteDecision(current, "jev/no-change", False)

    monkeypatch.setattr(loop.model_router, "route", route)
    model = SwitchableModel([{"calls": [call("read_file", path="a.txt")]}, {"text": "Done."}])
    worker = Agent(tmp_path, model, Settings(model="auto", sandbox="none"), Host())
    await worker.turn("read a.txt")
    assert len(seen) == 1
    await worker.turn("thanks")
    assert len(seen) == 2


@pytest.mark.anyio
async def test_a_local_to_cloud_escalation_is_announced_and_stops_local_routing(monkeypatch, tmp_path):
    from panini_cli.agent import loop, model_router

    monkeypatch.setattr(
        loop.model_router, "route", lambda *a, **k: model_router.RouteDecision(a[1], "x", False)
    )
    model = SwitchableModel([{"text": "Done."}], model="gpt-4.1")
    model.escalation = "unknown tool 'nonsense'"
    host = Host()
    worker = Agent(tmp_path, model, Settings(model="auto", sandbox="none"), host)
    await worker.turn("hello")
    assert any(route.reason.startswith("escalated:") for route in host.routes)
    assert model.escalation is None
