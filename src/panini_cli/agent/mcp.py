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
import hashlib
import json
import re
import shutil
import sys
import os
import threading

from . import VERSION
from ..process import ProcessTree

PROTOCOL_VERSION = "2025-06-18"
START_TIMEOUT = 30.0
CALL_TIMEOUT = 300.0
MAX_TOOLS = 512
MAX_PENDING = 64


class McpError(Exception):
    pass


def _tool_name(server, tool):
    original = f"mcp__{server}__{tool}"
    name = re.sub(r"[^A-Za-z0-9_-]", "_", original)
    if name != original or len(name) > 64:
        name = name[:51] + "_" + hashlib.sha256(original.encode()).hexdigest()[:12]
    return name


class _Server:
    def __init__(self, name, spec):
        self.name, self.spec = name, spec
        self.process = None
        self.tree = ProcessTree()
        self.pending = {}
        self.next_id = 0
        self.reader = None
        self.tools = []
        self.tools_by_name = {}
        self.http = None
        self.session = None
        self.http_lock = threading.Lock()
        self.http_slots = asyncio.Semaphore(MAX_PENDING)

    async def start(self):
        if self.spec.get("url"):
            import httpx
            from .public_http import PublicTransport, origin

            url = self.spec["url"]
            if origin(url)[0] != "https":
                raise McpError("Remote MCP requires HTTPS")
            self.http = httpx.Client(transport=PublicTransport(), trust_env=False, follow_redirects=False, timeout=30)
        else:
            await self._start_process()
        await self._initialize()

    async def _start_process(self):
        command = self.spec.get("command")
        if not command:
            raise McpError(f"MCP server {self.name} has no command")
        executable = shutil.which(command) or command
        from .shell import safe_environment

        env = {**safe_environment(), **{k: str(v) for k, v in (self.spec.get("env") or {}).items()}}
        self.process = await self.tree.start([executable, *[str(a) for a in self.spec.get("args") or []]],
                                            self.spec.get("cwd") or None, env)
        self.reader = asyncio.create_task(self._read())

    async def _initialize(self):
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
        seen = set()
        while True:
            result = await self.request("tools/list", {"cursor": cursor} if cursor else {}, timeout=START_TIMEOUT)
            page = result.get("tools") or []
            if not isinstance(page, list) or len(self.tools) + len(page) > MAX_TOOLS:
                raise McpError(f"MCP server {self.name} exceeds its {MAX_TOOLS}-tool catalog limit")
            for tool in page:
                if not isinstance(tool, dict) or not isinstance(tool.get("name"), str) or not tool["name"]:
                    raise McpError(f"MCP server {self.name} returned an invalid tool definition")
                if tool["name"] in self.tools_by_name or not isinstance(tool.get("inputSchema") or {}, dict):
                    raise McpError(f"MCP server {self.name} returned a duplicate tool or invalid schema")
                self.tools_by_name[tool["name"]] = tool
            self.tools.extend(page)
            cursor = result.get("nextCursor")
            if not cursor:
                break
            if not isinstance(cursor, str) or cursor in seen:
                raise McpError(f"MCP server {self.name} repeated or malformed its pagination cursor")
            seen.add(cursor)

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
                        error = message["error"]
                        future.set_exception(McpError(str(error.get("message", error) if isinstance(error, dict) else error)[:500]))
                    else:
                        result = message.get("result") or {}
                        if not isinstance(result, dict):
                            future.set_exception(McpError("MCP response result must be an object"))
                        else:
                            future.set_result(result)
        except asyncio.CancelledError:
            raise
        except (OSError, ValueError, TypeError, RecursionError):
            pass  # The pending requests below fail closed on malformed or oversized frames.
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
        if self.http:
            await asyncio.to_thread(self._http_request, {"jsonrpc": "2.0", "method": method, "params": params})
            return
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def request(self, method, params, timeout=CALL_TIMEOUT):
        if self.http:
            self.next_id += 1
            request_id = self.next_id
            async with self.http_slots:
                return await asyncio.wait_for(asyncio.to_thread(self._http_request,
                    {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}), timeout)
        if len(self.pending) >= MAX_PENDING:
            raise McpError(f"MCP server {self.name} has too many pending requests")
        self.next_id += 1
        request_id = self.next_id
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            return await asyncio.wait_for(future, timeout)
        except TimeoutError:
            raise McpError(f"MCP server {self.name} did not answer {method} within {timeout:.0f}s") from None
        finally:
            self.pending.pop(request_id, None)
            if not future.done():
                future.cancel()

    def _http_request(self, message):
        import httpx
        from . import mcp_auth

        headers = {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": PROTOCOL_VERSION}
        for name, value in (self.spec.get("headers") or {}).items():
            headers[name] = re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", lambda match: os.getenv(match[1], ""), str(value))
        token = mcp_auth.access_token(self.name, self.spec)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        with self.http_lock:
            if self.session:
                headers["Mcp-Session-Id"] = self.session
            try:
                with self.http.stream("POST", self.spec["url"], json=message, headers=headers) as reply:
                    if reply.status_code == 401:
                        raise McpError(f"MCP server {self.name} needs authentication: panini-cli mcp login {self.name}")
                    if reply.is_redirect:
                        raise McpError("Remote MCP redirects require a new explicit server URL")
                    reply.raise_for_status()
                    self.session = reply.headers.get("Mcp-Session-Id", self.session)
                    if "id" not in message or reply.status_code == 202:
                        return {}
                    if "text/event-stream" in reply.headers.get("content-type", ""):
                        data, size = [], 0
                        for line in reply.iter_lines():
                            size += len(line)
                            if size > 8 * 1024**2:
                                raise McpError("MCP response exceeds 8 MiB")
                            if line.startswith("data:"):
                                data.append(line[5:].lstrip())
                            elif not line and data:
                                result = json.loads("\n".join(data))
                                data.clear()
                                if result.get("id") == message["id"]:
                                    break
                        else:
                            raise McpError("MCP event stream ended without the requested result")
                    else:
                        data = bytearray()
                        for chunk in reply.iter_bytes():
                            if len(data) + len(chunk) > 8 * 1024**2:
                                raise McpError("MCP response exceeds 8 MiB")
                            data.extend(chunk)
                        result = json.loads(data)
                    if not isinstance(result, dict) or result.get("id") != message["id"]:
                        raise McpError("MCP response ID does not match the request")
                    if "error" in result:
                        raise McpError(str(result["error"])[:500])
                    if not isinstance(result.get("result"), dict):
                        raise McpError("MCP result must be an object")
                    return result["result"]
            except (httpx.HTTPError, ValueError) as exc:
                raise McpError(f"MCP server {self.name} transport failed: {type(exc).__name__}") from None

    async def close(self):
        try:
            if self.http:
                await asyncio.to_thread(self.http.close)
                self.http = None
            await self.tree.close()
        finally:
            if self.reader:
                self.reader.cancel()
                await asyncio.gather(self.reader, return_exceptions=True)


class McpManager:
    def __init__(self, servers: dict):
        self.servers = {name: _Server(name, spec) for name, spec in (servers or {}).items() if isinstance(spec, dict)}
        self.names = {}  # tool name the model sees -> (server, tool)
        self.errors = {}

    async def start(self):
        if getattr(self, "started", False):
            return self
        try:
            return await self._start()
        except BaseException:
            await self.close()
            raise

    async def _start(self):
        for name, server in self.servers.items():
            try:
                await asyncio.wait_for(server.start(), START_TIMEOUT)
            except (TimeoutError, McpError, OSError) as exc:
                self.errors[name] = str(exc)[:300] or f"MCP server {name} startup timed out"
                await server.close()
                continue
            for tool in server.tools:
                self.names[_tool_name(name, tool["name"])] = (name, tool["name"])
        self.started = True
        return self

    def tool_defs(self):
        defs = []
        for public, (server, tool_name) in self.names.items():
            tool = self.servers[server].tools_by_name[tool_name]
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

    async def protocol(self, server, method, arguments=None):
        if server not in self.servers or server in self.errors:
            raise McpError("Unknown or unavailable MCP server")
        if method not in {"resources/list", "resources/read", "prompts/list", "prompts/get"}:
            raise McpError("Unsupported MCP catalog operation")
        return await self.servers[server].request(method, arguments or {})

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
        self.read_only = mode == "read-only"

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
        if not item.get("permissionRequest") and item.get("sandbox") == "mxc" and item.get("executionScope") == "workspace" \
                and (self.mode == "edits" or item.get("filesystemMode") == "read-only"):
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
