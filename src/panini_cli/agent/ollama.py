"""Ollama backend: a local model behind the same respond() interface as OpenAIModel.

Conversation items stay in the Responses format the rest of the agent uses, so a
task can move between a local and a cloud model. Encrypted reasoning items are
OpenAI-only and are dropped when talking to Ollama.
"""

from __future__ import annotations

import json
import os
import re
import time

import httpx

from . import config, tools
from .model import ModelError, ModelTurn, ToolCall

LOCAL_NOTE = (
    "\n\nLocal-model notes: call one tool at a time, use exact tool names and JSON arguments, "
    "and keep answers short."
)
_PROBE_TTL = 20.0
_probe = {"at": 0.0, "host": None, "models": None}


def probe(host=None, *, timeout=0.5, fresh=False):
    """Installed Ollama model names, or None when the server cannot be reached. Cached briefly."""
    host = host or config.ollama_host()
    now = time.monotonic()
    if not fresh and _probe["host"] == host and now - _probe["at"] < _PROBE_TTL:
        return _probe["models"]
    try:
        reply = httpx.get(f"{host}/api/tags", timeout=timeout)
        reply.raise_for_status()
        models = [item.get("name", "") for item in reply.json().get("models", [])]
    except (httpx.HTTPError, ValueError):
        models = None
    _probe.update(at=now, host=host, models=models)
    return models


def has_model(name, host=None):
    models = probe(host) or []
    bare = name.removeprefix(config.LOCAL_PREFIX)
    return bare in models or (":" not in bare and f"{bare}:latest" in models)


def available(name=None):
    """(usable, detail). Usable means the server answers and the model is pulled."""
    bare = (name or config.local_model()).removeprefix(config.LOCAL_PREFIX)
    if probe(fresh=True) is None:
        return False, f"Ollama is not reachable at {config.ollama_host()}"
    if not has_model(bare):
        return False, f"model {bare} is not pulled; run `ollama pull {bare}`"
    return True, bare


def _ollama_tools(tool_defs):
    """Responses-format function tools -> Ollama's format. Hosted (non-function) tools are skipped."""
    return [
        {
            "type": "function",
            "function": {"name": t["name"], "description": t.get("description", ""), "parameters": t["parameters"]},
        }
        for t in tool_defs
        if t.get("type") == "function"
    ]


def to_messages(instructions, items):
    """Responses-format items -> Ollama chat messages."""
    messages = [{"role": "system", "content": instructions + LOCAL_NOTE}]
    names = {}
    for item in items:
        kind = item.get("type")
        if kind == "reasoning":
            continue
        if kind == "function_call":
            try:
                arguments = json.loads(item.get("arguments") or "{}")
            except ValueError:
                arguments = {}
            names[item["call_id"]] = item["name"]
            call = {"function": {"name": item["name"], "arguments": arguments}}
            if messages[-1]["role"] == "assistant":
                messages[-1].setdefault("tool_calls", []).append(call)
            else:
                messages.append({"role": "assistant", "content": "", "tool_calls": [call]})
        elif kind == "function_call_output":
            messages.append(
                {"role": "tool", "tool_name": names.get(item["call_id"], ""), "content": str(item["output"])}
            )
        elif kind == "message" or "role" in item:
            content = item.get("content", "")
            if isinstance(content, list):
                content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
            role = item.get("role", "assistant")
            messages.append({"role": "system" if role == "developer" else role, "content": str(content)})
    return messages


def _text_tool_call(text):
    """Small models sometimes print the call as JSON text instead of using the tool channel."""
    match = re.search(r"\{.*\}", text or "", flags=re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("name"), str):
        return None
    arguments = data.get("arguments", data.get("parameters", {}))
    return (data["name"], arguments) if isinstance(arguments, dict) else None


class OllamaModel:
    def __init__(self, model, timeout=300.0, host=None, context=None):
        self.model = model.removeprefix(config.LOCAL_PREFIX)
        self.host = host or config.ollama_host()
        self.timeout = timeout
        try:
            self.context = int(context or os.getenv("PANINI_CLI_LOCAL_CTX") or config.DEFAULT_LOCAL_CONTEXT)
        except ValueError:
            self.context = config.DEFAULT_LOCAL_CONTEXT
        self.transport = None  # Tests inject an httpx transport.
        self.known_tools = {tool["name"] for tool in tools.TOOLS}

    def switch_model(self, model):
        self.model = model.removeprefix(config.LOCAL_PREFIX)

    async def respond(self, instructions, items, tool_defs, on_text=None, output_schema=None, cache_key=None):
        body = {
            "model": self.model,
            "messages": to_messages(instructions, items),
            "tools": [] if output_schema else _ollama_tools(tool_defs or []),
            "stream": True,
            "keep_alive": "10m",
            "options": {"num_ctx": self.context, "temperature": 0.2},
        }
        if output_schema:
            body["format"] = output_schema
        self.known_tools = {t["name"] for t in (tool_defs or []) if "name" in t}
        text, calls, usage = [], [], {}
        item_id = f"msg_ollama_{time.monotonic_ns()}"
        try:
            timeout = httpx.Timeout(self.timeout, connect=5.0)
            async with httpx.AsyncClient(timeout=timeout, transport=self.transport) as client:
                async with client.stream("POST", f"{self.host}/api/chat", json=body) as reply:
                    if reply.status_code != 200:
                        detail = (await reply.aread()).decode("utf-8", "replace")[:300]
                        raise ModelError(f"Ollama error {reply.status_code}: {detail}")
                    async for line in reply.aiter_lines():
                        if not line.strip():
                            continue
                        chunk = json.loads(line)
                        if chunk.get("error"):
                            raise ModelError(f"Ollama: {chunk['error']}"[:300])
                        message = chunk.get("message") or {}
                        if message.get("content"):
                            text.append(message["content"])
                            if on_text:
                                await on_text(item_id, message["content"])
                        calls.extend(call.get("function") or {} for call in message.get("tool_calls") or [])
                        if chunk.get("done"):
                            prompt, output = chunk.get("prompt_eval_count", 0), chunk.get("eval_count", 0)
                            usage = {"input_tokens": prompt, "output_tokens": output, "total_tokens": prompt + output}
        except httpx.ConnectError:
            raise ModelError(f"Could not reach Ollama at {self.host}. Is it running?") from None
        except httpx.TimeoutException:
            raise ModelError("The local model timed out.") from None
        except (httpx.HTTPError, ValueError) as exc:
            raise ModelError(f"The local model stream failed: {str(exc)[:200]}") from None
        return self._turn("".join(text), calls, usage)

    def _turn(self, text, raw_calls, usage):
        if not raw_calls:
            parsed = _text_tool_call(text)
            if parsed and parsed[0] in self.known_tools:
                raw_calls, text = [{"name": parsed[0], "arguments": parsed[1]}], ""
        # Plain role messages, not output-message items: valid input for OpenAI if the task escalates.
        output, calls = [], []
        if text:
            output.append({"role": "assistant", "content": text})
        stamp = time.monotonic_ns()
        for index, raw in enumerate(raw_calls):
            arguments = raw.get("arguments") or {}
            arguments = arguments if isinstance(arguments, str) else json.dumps(arguments)
            call_id = f"call_ollama_{stamp}_{index}"
            name = raw.get("name", "")
            output.append({"type": "function_call", "call_id": call_id, "name": name, "arguments": arguments})
            calls.append(ToolCall(call_id, name, arguments))
        return ModelTurn(output, text, calls, usage)


def invalid_turn(turn, tool_defs=None):
    """Why a local turn cannot be trusted, or None. The router escalates instead of looping on it."""
    known = {tool["name"] for tool in (tools.TOOLS if tool_defs is None else tool_defs) if "name" in tool}
    for call in turn.calls:
        if call.name not in known:
            return f"unknown tool {call.name!r}"
        try:
            if not isinstance(json.loads(call.arguments or "{}"), dict):
                return f"bad arguments for {call.name}"
        except ValueError:
            return f"malformed arguments for {call.name}"
    if not turn.calls and not turn.text.strip():
        return "empty response"
    return None
