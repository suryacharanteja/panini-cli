"""Provider failures and streaming cleanup, without live credentials or requests."""

import asyncio
from types import SimpleNamespace

import httpx
import openai
import pytest

from panini_cli.agent.model import ModelError, OpenAIModel


@pytest.mark.parametrize("error_type,status,message", [
    (openai.AuthenticationError, 401, "rejected the API key"),
    (openai.PermissionDeniedError, 403, "not allowed"),
    (openai.NotFoundError, 404, "not available"),
    (openai.RateLimitError, 429, "rate limit"),
    (openai.APIStatusError, 500, "error 500"),
    (openai.BadRequestError, 400, "rejected the request"),
])
def test_provider_status_failures_have_actionable_messages(error_type, status, message):
    model = OpenAIModel.__new__(OpenAIModel)
    model.model, model.reasoning, model.effort = "test-model", True, None
    async def fail(request, callback):
        response = httpx.Response(status, request=httpx.Request("POST", "https://api.example.test"))
        raise error_type("synthetic failure", response=response, body={"error": {"message": "safe detail"}})
    model._stream = fail
    with pytest.raises(ModelError, match=message):
        asyncio.run(model.respond("test", [], []))


@pytest.mark.parametrize("timeout,message", [(True, "timed out"), (False, "network connection")])
def test_provider_connection_failures(timeout, message):
    model = OpenAIModel.__new__(OpenAIModel)
    model.model, model.reasoning, model.effort = "test-model", True, None
    async def fail(request, callback):
        request = httpx.Request("POST", "https://api.example.test")
        raise openai.APITimeoutError(request=request) if timeout else openai.APIConnectionError(request=request)
    model._stream = fail
    with pytest.raises(ModelError, match=message):
        asyncio.run(model.respond("test", [], []))


def test_unsupported_reasoning_retries_once_without_reasoning_options():
    model = OpenAIModel.__new__(OpenAIModel)
    model.model, model.reasoning, model.effort = "test-model", True, "high"
    requests = []
    async def stream(request, callback):
        requests.append(request)
        if len(requests) == 1:
            response = httpx.Response(400, request=httpx.Request("POST", "https://api.example.test"))
            raise openai.BadRequestError("reasoning unsupported", response=response, body=None)
        return "done"
    model._stream = stream
    assert asyncio.run(model.respond("test", [], [], output_schema={"type": "object"}, cache_key="test")) == "done"
    assert "reasoning" in requests[0] and "reasoning" not in requests[1]
    assert "include" not in requests[1]
    assert requests[1]["store"] is False
    assert requests[1]["text"]["format"]["strict"] is True


@pytest.mark.parametrize("events,message", [
    ([], "ended before"),
    ([SimpleNamespace(type="error", message="synthetic")], "stream failed"),
    ([SimpleNamespace(type="response.failed", response=SimpleNamespace(status="failed", error=None))], "model failed"),
    ([SimpleNamespace(type="response.incomplete", response=SimpleNamespace(status="incomplete", incomplete_details=None))], "cut off"),
])
def test_failed_streams_always_close(events, message):
    class Stream:
        closed = False
        def __aiter__(self):
            async def iterate():
                for event in events:
                    yield event
            return iterate()
        async def close(self):
            self.closed = True
    stream = Stream()
    async def create(**kwargs):
        return stream
    model = OpenAIModel.__new__(OpenAIModel)
    model.client = SimpleNamespace(responses=SimpleNamespace(create=create))
    with pytest.raises(ModelError, match=message):
        asyncio.run(model._stream({}, None))
    assert stream.closed


def test_completed_stream_preserves_tool_calls_text_and_usage():
    class Item(SimpleNamespace):
        def model_dump(self, **kwargs):
            return {"type": self.type}
    output = [Item(type="message", content=[SimpleNamespace(type="output_text", text="done")]),
              Item(type="function_call", call_id="call-1", name="read_file", arguments='{"path":"a.py"}')]
    response = SimpleNamespace(status="completed", output=output,
                               usage=SimpleNamespace(model_dump=lambda **kwargs: {"total_tokens": 9}))
    class Stream:
        closed = False
        def __aiter__(self):
            async def iterate():
                yield SimpleNamespace(type="response.output_text.delta", item_id="msg", delta="done")
                yield SimpleNamespace(type="response.completed", response=response)
            return iterate()
        async def close(self):
            self.closed = True
    stream = Stream()
    async def create(**kwargs):
        return stream
    deltas = []
    async def callback(item_id, delta):
        deltas.append((item_id, delta))
    model = OpenAIModel.__new__(OpenAIModel)
    model.client = SimpleNamespace(responses=SimpleNamespace(create=create))
    result = asyncio.run(model._stream({}, callback))
    assert result.text == "done" and result.usage == {"total_tokens": 9}
    assert result.calls[0].name == "read_file" and result.calls[0].call_id == "call-1"
    assert deltas == [("msg", "done")] and stream.closed
