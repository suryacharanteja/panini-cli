"""Model clients: OpenAI's Responses API (stateless), local Ollama models, a router over both, and a scripted model for tests."""

import asyncio
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from . import config, model_router


class ModelError(Exception):
    """The model could not respond; the message is safe to show the user."""


@dataclass
class ToolCall:
    call_id: str
    name: str
    arguments: str


@dataclass
class ModelTurn:
    items: list  # Output items, sent back as input on the next request.
    text: str
    calls: list
    usage: dict = field(default_factory=dict)


def _detail(exc):
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error", body)
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])[:300]
    return str(exc)[:300]


class OpenAIModel:
    """Nothing is stored at OpenAI (store=False). Encrypted reasoning is carried forward so a
    reasoning model keeps its chain of thought across tool calls within the conversation."""

    def __init__(self, model, reasoning_effort=None, timeout=180.0):
        from openai import AsyncOpenAI

        self.model, self.effort = model, reasoning_effort
        self.client = AsyncOpenAI(timeout=timeout, max_retries=3)
        self.reasoning = True

    def switch_model(self, model):
        if model != self.model:
            self.model = model
            self.reasoning = True

    async def respond(self, instructions, items, tools, on_text=None, output_schema=None, cache_key=None):
        import openai

        params = {
            "model": self.model,
            "instructions": instructions,
            "input": items,
            "tools": tools,
            "store": False,
            "stream": True,
            "parallel_tool_calls": True,
        }
        if cache_key:
            params["prompt_cache_key"] = cache_key
        if output_schema:
            params["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": "final_answer",
                    "schema": output_schema,
                    "strict": True,
                }
            }
        while True:
            request = dict(params)
            if self.reasoning:
                request["include"] = ["reasoning.encrypted_content"]
                if self.effort:
                    request["reasoning"] = {"effort": self.effort}
            try:
                return await self._stream(request, on_text)
            except openai.BadRequestError as exc:
                if self.reasoning and "reasoning" in str(exc).lower():
                    self.reasoning = False  # Not a reasoning model; ask again without those options.
                    continue
                raise ModelError(f"The model rejected the request: {_detail(exc)}") from None
            except openai.AuthenticationError:
                raise ModelError(
                    "OpenAI rejected the API key. Replace it with `panini-cli auth login`, "
                    "or set a valid OPENAI_API_KEY."
                ) from None
            except openai.PermissionDeniedError:
                raise ModelError(f"This API key is not allowed to use {self.model}.") from None
            except openai.NotFoundError:
                raise ModelError(f"The model {self.model} is not available to this API key.") from None
            except openai.RateLimitError:
                raise ModelError("OpenAI's rate limit or quota was reached. Try again later.") from None
            except openai.APITimeoutError:
                raise ModelError("The model request timed out.") from None
            except openai.APIConnectionError:
                raise ModelError("Could not reach OpenAI. Check the network connection.") from None
            except openai.APIStatusError as exc:
                raise ModelError(f"OpenAI error {exc.status_code}: {_detail(exc)}") from None

    async def _stream(self, request, on_text):
        stream = await self.client.responses.create(**request)
        response = None
        try:
            async for event in stream:
                if event.type == "response.output_text.delta":
                    if on_text:
                        await on_text(event.item_id, event.delta)
                elif event.type in ("response.completed", "response.incomplete", "response.failed"):
                    response = event.response
                elif event.type == "error":
                    raise ModelError(f"The model stream failed: {event.message}")
        finally:
            await stream.close()
        if response is None:
            raise ModelError("The model stream ended before the response was complete.")
        if response.status == "failed":
            message = response.error.message if response.error else "unknown error"
            raise ModelError(f"The model failed: {message}")
        if response.status == "incomplete":
            reason = response.incomplete_details.reason if response.incomplete_details else "unknown"
            raise ModelError(f"The model's response was cut off ({reason}).")
        text = "".join(
            part.text
            for item in response.output
            if item.type == "message"
            for part in item.content
            if part.type == "output_text"
        )
        calls = [
            ToolCall(item.call_id, item.name, item.arguments)
            for item in response.output
            if item.type == "function_call"
        ]
        return ModelTurn(
            [item.model_dump(exclude_none=True) for item in response.output],
            text,
            calls,
            response.usage.model_dump(exclude_none=True) if response.usage else {},
        )


class ScriptedModel:
    """Replays scripted steps for tests and offline demos; it is never a real model.

    Each step is {"text": str, "calls": [{"name": str, "arguments": {...}}]}, optionally
    with "delay" (seconds) or "error" (a ModelError message).
    """

    def __init__(self, steps):
        self.steps = list(steps)
        self.requests = []
        self.count = 0

    @classmethod
    def from_file(cls, path):
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    async def respond(self, instructions, items, tools, on_text=None, output_schema=None, cache_key=None):
        self.requests.append({"instructions": instructions, "items": json.loads(json.dumps(items))})
        step = self.steps.pop(0) if self.steps else {"text": "Done."}
        if step.get("delay"):
            await asyncio.sleep(step["delay"])
        if step.get("error"):
            raise ModelError(step["error"])
        self.count += 1
        text = step.get("text", "")
        message_id = f"msg_scripted_{self.count}"
        if text and on_text:
            for start in range(0, len(text), 24):
                await on_text(message_id, text[start : start + 24])
        output, calls = [], []
        if text:
            output.append(
                {
                    "type": "message",
                    "id": message_id,
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": text, "annotations": []}],
                }
            )
        for index, call in enumerate(step.get("calls", [])):
            call_id = f"call_scripted_{self.count}_{index}"
            arguments = (
                call["arguments"] if isinstance(call["arguments"], str) else json.dumps(call["arguments"])
            )
            output.append(
                {
                    "type": "function_call",
                    "call_id": call_id,
                    "name": call["name"],
                    "arguments": arguments,
                    "status": "completed",
                }
            )
            calls.append(ToolCall(call_id, call["name"], arguments))
        return ModelTurn(output, text, calls, {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0})


class RoutedModel:
    """One model object for the whole session; the router decides which backend answers.

    A model id starting with "ollama/" runs locally through Ollama, anything else on OpenAI.
    Backends are created on first use, so a local-only setup never needs an OpenAI key.
    When the local model errors or produces an unusable turn, and the model was chosen by the
    router (not pinned by the user), the same request is retried once on the cloud fallback
    and local routing is switched off for the rest of the session.
    """

    def __init__(self, model, reasoning_effort=None, timeout=180.0, *, escalate=True):
        self.model, self.effort, self.timeout, self.escalate = model, reasoning_effort, timeout, escalate
        self.local_blocked = False
        self.escalation = None  # Set to a reason each time a local turn moves to the cloud.
        self._cloud = None
        self._local = None

    def switch_model(self, model):
        self.model = model

    def _cloud_backend(self):
        if self._cloud is None:
            try:
                self._cloud = OpenAIModel(self.model, self.effort, self.timeout)
            except Exception as exc:  # noqa: BLE001 -- the client raises when no key is configured.
                raise ModelError(f"OpenAI is not available: {str(exc)[:200]}") from None
        self._cloud.switch_model(self.model)
        return self._cloud

    def _local_backend(self):
        from .ollama import OllamaModel

        if self._local is None:
            self._local = OllamaModel(self.model)
        self._local.switch_model(self.model)
        return self._local

    async def respond(self, instructions, items, tools, on_text=None, output_schema=None, cache_key=None):
        if not config.is_local(self.model):
            return await self._cloud_backend().respond(instructions, items, tools, on_text, output_schema, cache_key)
        from .ollama import invalid_turn

        streamed = []

        async def watch(item_id, delta):
            streamed.append(delta)
            if on_text:
                await on_text(item_id, delta)

        try:
            turn = await self._local_backend().respond(instructions, items, tools, watch, output_schema, cache_key)
            problem = invalid_turn(turn, tools)
            if problem:
                raise ModelError(f"the local model gave an unusable turn ({problem})")
            return turn
        except ModelError as exc:
            # Text already shown to the user cannot be taken back, so only retry silently.
            if not self.escalate or streamed:
                raise
            self.local_blocked = True
            self.escalation = str(exc)[:160]
            self.model = model_router.fallback_model()
            return await self._cloud_backend().respond(instructions, items, tools, on_text, output_schema, cache_key)


def create_model(settings):
    script = os.getenv("PANINI_CLI_SCRIPTED_MODEL")
    if script:
        return ScriptedModel.from_file(script)
    return RoutedModel(
        model_router.actual_model(settings.model),
        settings.reasoning_effort,
        settings.request_timeout,
        escalate=model_router.enabled(settings.model),
    )
