import json

import httpx
import pytest

from panini_cli.agent import ollama
from panini_cli.agent.model import ModelError, ModelTurn, RoutedModel, ToolCall


@pytest.fixture
def anyio_backend():
    return "asyncio"


def stream(*chunks, status=200):
    body = "\n".join(json.dumps(chunk) for chunk in chunks)
    return httpx.Response(status, content=body.encode())


def local(handler):
    model = ollama.OllamaModel("ollama/qwen2.5:3b-instruct", host="http://ollama.test")
    model.transport = httpx.MockTransport(handler)
    return model


DONE = {"message": {"content": ""}, "done": True, "prompt_eval_count": 11, "eval_count": 4}


@pytest.mark.anyio
async def test_streams_text_and_reports_usage(monkeypatch):
    monkeypatch.delenv("PANINI_CLI_LOCAL_CTX", raising=False)
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return stream({"message": {"content": "Hel"}}, {"message": {"content": "lo"}}, DONE)

    deltas = []

    async def on_text(item_id, delta):
        deltas.append(delta)

    turn = await local(handler).respond("sys", [{"role": "user", "content": "hi"}], [], on_text=on_text)
    assert turn.text == "Hello" and deltas == ["Hel", "lo"]
    assert turn.usage == {"input_tokens": 11, "output_tokens": 4, "total_tokens": 15}
    assert seen["model"] == "qwen2.5:3b-instruct"
    assert seen["options"]["num_ctx"] == 16_384
    assert seen["messages"][0]["role"] == "system"
    assert "read_file" in {tool["function"]["name"] for tool in seen["tools"]}


@pytest.mark.anyio
async def test_tool_calls_become_agent_calls():
    call = {"function": {"name": "read_file", "arguments": {"path": "a.py"}}}
    turn = await local(lambda request: stream({"message": {"tool_calls": [call]}}, DONE)).respond("s", [], [])
    assert [c.name for c in turn.calls] == ["read_file"]
    assert json.loads(turn.calls[0].arguments) == {"path": "a.py"}
    assert turn.items[0]["type"] == "function_call"
    assert ollama.invalid_turn(turn) is None


@pytest.mark.anyio
async def test_json_printed_as_text_is_recovered_as_a_tool_call():
    text = '```json\n{"name": "read_file", "arguments": {"path": "a.py"}}\n```'
    turn = await local(lambda request: stream({"message": {"content": text}}, DONE)).respond("s", [], [])
    assert turn.calls and turn.calls[0].name == "read_file" and turn.text == ""


def test_unknown_tools_and_empty_turns_are_flagged():
    assert "unknown tool" in ollama.invalid_turn(ModelTurn([], "", [ToolCall("1", "rm_rf", "{}")]))
    assert "malformed" in ollama.invalid_turn(ModelTurn([], "", [ToolCall("1", "read_file", "{oops")]))
    assert ollama.invalid_turn(ModelTurn([], "  ", [])) == "empty response"


def test_history_converts_to_chat_messages_and_drops_reasoning():
    items = [
        {"role": "user", "content": "go"},
        {"type": "reasoning", "encrypted_content": "x"},
        {"role": "assistant", "content": "checking"},
        {"type": "function_call", "call_id": "c1", "name": "read_file", "arguments": '{"path": "a"}'},
        {"type": "function_call_output", "call_id": "c1", "output": "contents"},
    ]
    messages = ollama.to_messages("sys", items)
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "tool"]
    assert messages[2]["tool_calls"][0]["function"] == {"name": "read_file", "arguments": {"path": "a"}}
    assert messages[3]["tool_name"] == "read_file"


@pytest.mark.anyio
async def test_errors_are_reported_plainly():
    with pytest.raises(ModelError, match="Ollama error 404"):
        await local(lambda request: httpx.Response(404, text="model not found")).respond("s", [], [])

    def refuse(request):
        raise httpx.ConnectError("no")

    with pytest.raises(ModelError, match="Could not reach Ollama"):
        await local(refuse).respond("s", [], [])


class FakeCloud:
    def __init__(self):
        self.model = None

    def switch_model(self, model):
        self.model = model

    async def respond(self, *args, **kwargs):
        return ModelTurn([], f"cloud:{self.model}", [])


def routed(handler, *, escalate=True):
    model = RoutedModel("ollama/qwen2.5:3b-instruct", escalate=escalate)
    model._local = local(handler)
    model._cloud = FakeCloud()
    return model


@pytest.mark.anyio
async def test_a_bad_local_turn_escalates_once_to_the_cloud():
    bad_call = {"function": {"name": "nonsense", "arguments": {}}}
    model = routed(lambda request: stream({"message": {"tool_calls": [bad_call]}}, DONE))
    turn = await model.respond("s", [], [])
    assert turn.text.startswith("cloud:") and model.local_blocked and "unknown tool" in model.escalation
    assert not model.model.startswith("ollama/")


@pytest.mark.anyio
async def test_a_pinned_local_model_does_not_escalate():
    model = routed(lambda request: httpx.Response(500, text="boom"), escalate=False)
    with pytest.raises(ModelError):
        await model.respond("s", [], [])


@pytest.mark.anyio
async def test_a_good_local_turn_never_touches_the_cloud():
    model = routed(lambda request: stream({"message": {"content": "ok"}}, DONE))
    turn = await model.respond("s", [], [])
    assert turn.text == "ok" and not model.local_blocked
