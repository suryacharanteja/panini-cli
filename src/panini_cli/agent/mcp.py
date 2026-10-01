"""Model Context Protocol: PANINI as an MCP client (tools from your servers) and as an MCP server.

Client: servers are declared in ~/.panini/config.toml and started over stdio when a session starts.

    [mcp_servers.filesystem]
    command = "npx"
    args = ["-y", "@modelcontextprotocol/server-filesystem", "."]
    env = { SOME_TOKEN = "..." }          # optional
    trusted_tools = ["read_file"]         # optional: these run without asking

Their tools appear to the model as `mcp__<server>__<tool>`. Every call goes through PANINI's
approval prompt (unless trusted), hooks and receipts, and the output is marked as data.

Server: `panini-cli mcp-server` lets another agent use PANINI's router and agent as tools.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys

from . import VERSION

PROTOCOL_VERSION = "2025-06-18"
START_TIMEOUT = 30.0
CALL_TIMEOUT = 300.0


class McpError(Exception):
    pass


def _tool_name(server, tool):
    name = re.sub(r"[^A-Za-z0-9_-]", "_", f"mcp__{server}__{tool}")
    return name[:64]


class _Server:
    def __init__(self, name, spec):
        self.name, self.spec = name, spec
        self.process = None
        self.pending = {}
        self.next_id = 0
        self.reader = None
        self.tools = []

    async def start(self):
        command = self.spec.get("command")
        if not command:
            raise McpError(f"MCP server {self.name} has no command")
        executable = shutil.which(command) or command
        env = {**os.environ, **{k: str(v) for k, v in (self.spec.get("env") or {}).items()}}
        self.process = await asyncio.create_subprocess_exec(
            executable,
            *[str(a) for a in self.spec.get("args") or []],
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
            cwd=self.spec.get("cwd") or None,
            limit=16 * 1024 * 1024,
        )
        self.reader = asyncio.create_task(self._read())
        await self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "panini-cli", "version": VERSION},
            },
            timeout=START_TIMEOUT,
        )
        await self.notify("notifications/initialized", {})
        cursor = None
        while True:
            result = await self.request("tools/list", {"cursor": cursor} if cursor else {}, timeout=START_TIMEOUT)
            self.tools.extend(result.get("tools") or [])
            cursor = result.get("nextCursor")
            if not cursor:
                break

    async def _read(self):
        try:
            while True:
                line = await self.process.stdout.readline()
                if not line:
                    break
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                future = self.pending.pop(message.get("id"), None) if isinstance(message, dict) else None
                if future and not future.done():
                    if "error" in message:
                        future.set_exception(McpError(str(message["error"].get("message", message["error"]))[:500]))
                    else:
                        future.set_result(message.get("result") or {})
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(McpError(f"MCP server {self.name} exited"))
            self.pending.clear()

    async def _send(self, message):
        if self.process is None or self.process.stdin is None or self.process.returncode is not None:
            raise McpError(f"MCP server {self.name} is not running")
        self.process.stdin.write((json.dumps(message) + "\n").encode())
        await self.process.stdin.drain()

    async def notify(self, method, params):
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def request(self, method, params, timeout=CALL_TIMEOUT):
        self.next_id += 1
        request_id = self.next_id
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        await self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        try:
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            self.pending.pop(request_id, None)
            raise McpError(f"MCP server {self.name} did not answer {method} within {timeout:.0f}s") from None

    async def close(self):
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except asyncio.TimeoutError:
                self.process.kill()
        if self.reader:
            self.reader.cancel()


class McpManager:
    def __init__(self, servers: dict):
        self.servers = {name: _Server(name, spec) for name, spec in (servers or {}).items() if isinstance(spec, dict)}
        self.names = {}  # tool name the model sees -> (server, tool)
        self.errors = {}

    async def start(self):
        for name, server in self.servers.items():
            try:
                await server.start()
            except (McpError, OSError, asyncio.TimeoutError) as exc:
                self.errors[name] = str(exc)[:300]
                await server.close()
                continue
            for tool in server.tools:
                self.names[_tool_name(name, tool["name"])] = (name, tool["name"])
        return self

    def tool_defs(self):
        defs = []
        for public, (server, tool_name) in self.names.items():
            tool = next(t for t in self.servers[server].tools if t["name"] == tool_name)
            defs.append(
                {
                    "type": "function",
                    "name": public,
                    "description": f"[MCP server {server}] {tool.get('description', '')}"[:1000],
                    "parameters": tool.get("inputSchema") or {"type": "object", "properties": {}},
                    "strict": False,
                }
            )
        return defs

    def split(self, public):
        if public not in self.names:
            raise McpError(f"Unknown MCP tool {public}")
        return self.names[public]

    def trusted(self, server, tool):
        spec = self.servers[server].spec
        return bool(spec.get("trust")) or tool in (spec.get("trusted_tools") or [])

    async def call(self, public, arguments):
        server, tool = self.split(public)
        result = await self.servers[server].request("tools/call", {"name": tool, "arguments": arguments})
        parts = []
        for block in result.get("content") or []:
            if block.get("type") == "text":
                parts.append(block.get("text", ""))
            else:
                parts.append(f"[{block.get('type')} content omitted]")
        text = "\n".join(parts) or json.dumps(result.get("structuredContent") or {}, ensure_ascii=False)
        return ("Error: " + text) if result.get("isError") else text

    async def close(self):
        for server in self.servers.values():
            await server.close()


# --- PANINI as an MCP server ----------------------------------------------------------------

SERVER_TOOLS = [
    {
        "name": "panini_route",
        "description": "Ask PANINI's router which model (local Ollama or a cloud tier) a coding task should use, and why.",
        "inputSchema": {"type": "object", "properties": {"task": {"type": "string"}}, "required": ["task"]},
    },
    {
        "name": "panini_run",
        "description": (
            "Run a coding task with the PANINI CLI agent in a workspace and return its final answer. "
            "approval_mode: read-only (default; no changes), edits (file changes allowed, commands need "
            "approval and are declined) or auto (everything allowed)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {"type": "string"},
                "workspace": {"type": "string"},
                "approval_mode": {"type": "string", "enum": ["read-only", "edits", "auto"]},
            },
            "required": ["task", "workspace"],
        },
    },
]


class _ServerHost:
    def __init__(self, mode):
        self.mode = mode

    async def started(self, item):
        pass

    async def delta(self, *args):
        pass

    async def completed(self, item):
        pass

    async def approve(self, item):
        from . import approvals

        if self.mode == "auto" or (self.mode == "edits" and item["type"] == "fileChange"):
            return "accept"
        if item["type"] == "commandExecution" and item.get("executionScope") == "workspace" and approvals.is_trusted(item.get("command", "")):
            return "accept"
        return "decline"


async def _run_tool(name, arguments):
    from . import model_router
    from .config import Settings
    from .loop import Agent
    from .model import create_model

    if name == "panini_route":
        decision = await asyncio.to_thread(model_router.route, arguments.get("task", ""), model_router.fallback_model())
        return f"{decision.model} ({decision.reason})"
    if name == "panini_run":
        mode = arguments.get("approval_mode") or "read-only"
        settings = Settings.from_env()
        agent = Agent(arguments["workspace"], create_model(settings), settings, _ServerHost(mode))
        try:
            result = await agent.turn(arguments["task"])
        finally:
            await agent.close()
        spent = "unpriced" if result.cost_usd is None else f"${result.cost_usd:.4f}"
        return f"{result.text or result.error}\n\n[status {result.status} · {result.steps} steps · {spent}]"
    raise McpError(f"Unknown tool {name}")


async def serve_stdio(stdin=None, stdout=None):
    """A minimal MCP server over stdio (newline-delimited JSON-RPC)."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout

    def send(message):
        stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
        stdout.flush()

    while True:
        line = await asyncio.to_thread(stdin.readline)
        if not line:
            return
        try:
            message = json.loads(line)
        except ValueError:
            continue
        method, request_id = message.get("method"), message.get("id")
        if request_id is None:
            continue  # notifications
        try:
            if method == "initialize":
                result = {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "panini-cli", "version": VERSION},
                }
            elif method == "tools/list":
                result = {"tools": SERVER_TOOLS}
            elif method == "tools/call":
                params = message.get("params") or {}
                text = await _run_tool(params.get("name"), params.get("arguments") or {})
                result = {"content": [{"type": "text", "text": text}], "isError": False}
            elif method == "ping":
                result = {}
            else:
                send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"Unknown method {method}"}})
                continue
            send({"jsonrpc": "2.0", "id": request_id, "result": result})
        except Exception as exc:  # noqa: BLE001 -- report failures to the client instead of dying.
            send({"jsonrpc": "2.0", "id": request_id, "result": {"content": [{"type": "text", "text": f"Error: {exc}"}], "isError": True}})
