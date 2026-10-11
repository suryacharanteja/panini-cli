"""PANINI-CLI app-server: PANINI's coding protocol as JSON lines over stdio.

Requests from the host: initialize, config/read, thread/start, turn/start and
turn/interrupt. Notifications to the host: item/started, item/*/delta,
item/completed, turn/plan/updated and turn/completed. Requests to the host: approval of each command
(item/commandExecution/requestApproval) and file change (item/fileChange/requestApproval).
"""

import asyncio
import json
import sys
import threading
from pathlib import Path
from uuid import uuid4

from . import VERSION
from .config import Settings
from .hooks import Hooks
from .loop import Agent
from .model import create_model

# Reported as-is: the agent does not claim an operating-system sandbox it does not have.
DELTA_INTERVAL = 0.15
DELTA_BYTES = 16384


class RpcError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class ServerHost:
    """Forwards agent activity as protocol notifications; approvals go to the host."""

    def __init__(self, server, *, mode="untrusted", read_only=False):
        self.server = server
        self.mode, self.read_only = mode, read_only
        self.thread_id = self.turn_id = None
        self.buffers = {}
        self.flusher = None

    def _params(self, **values):
        return {"threadId": self.thread_id, "turnId": self.turn_id, **values}

    def _flush(self, item_id=None):
        for key in [item_id] if item_id else list(self.buffers):
            kind, text = self.buffers.pop(key, (None, ""))
            if text:
                method = (
                    "item/agentMessage/delta"
                    if kind == "agentMessage"
                    else "item/commandExecution/outputDelta"
                )
                self.server.notify(method, self._params(itemId=key, delta=text))

    async def _later(self):
        await asyncio.sleep(DELTA_INTERVAL)
        self._flush()

    def flush(self):
        self._flush()

    async def started(self, item):
        self._flush()
        self.server.notify("item/started", self._params(item=item))

    async def delta(self, item_id, kind, text):
        # Coalesced so a noisy command cannot flood the host's event queue.
        buffered = self.buffers.setdefault(item_id, [kind, ""])
        buffered[1] += text
        if len(buffered[1]) >= DELTA_BYTES:
            self._flush(item_id)
        elif self.flusher is None or self.flusher.done():
            self.flusher = asyncio.create_task(self._later())

    async def completed(self, item):
        self._flush(item["id"])
        self.server.notify("item/completed", self._params(item=item))

    async def plan(self, steps):
        self._flush()
        status = {"in_progress": "inProgress"}
        self.server.notify(
            "turn/plan/updated",
            self._params(
                plan=[{"step": s["step"], "status": status.get(s["status"], s["status"])} for s in steps]
            ),
        )

    async def approve(self, item):
        self._flush()
        change = item["type"] == "fileChange"
        exception = item.get("permissionRequest") or item.get("executionScope") == "host"
        isolated = item.get("sandbox") == "mxc" and item.get("policyIdentity") and not exception
        if self.read_only and (change or exception):
            return "decline"
        if self.mode != "untrusted" and ((isolated and not change) or
                (change and self.mode in {"edits", "auto", "never"} and not self.read_only)):
            return "accept"
        if self.mode == "never":
            return "decline"
        if item["type"] == "commandExecution":
            method = "item/commandExecution/requestApproval"
            scope = item.get("executionScope", "workspace")
            params = self._params(
                itemId=item["id"],
                command=item["command"],
                cwd=item["cwd"],
                executionScope=scope,
                policy={key: item[key] for key in ("policyIdentity", "filesystemMode", "networkMode", "networkHosts", "sandbox") if key in item},
                permissionRequest=bool(item.get("permissionRequest")),
                reason=("PANINI-CLI requests host access for this command. It can reach files and the network "
                        "available to your Windows user." if scope == "host" else
                        "PANINI-CLI wants to run a command in the selected sandbox."),
            )
        else:
            method = "item/fileChange/requestApproval"
            params = self._params(itemId=item["id"], changes=item.get("changes", []), reason="PANINI-CLI wants to change a file.")
        try:
            result = await self.server.request(method, params)
        except RpcError:
            return "decline"
        decision = result.get("decision") if isinstance(result, dict) else None
        return decision if decision in ("accept", "decline", "cancel") else "decline"


MAX_FRAME = 1024 * 1024
MAX_HANDLERS = 32
MAX_THREADS = 8
APPROVAL_TIMEOUT = 300


def _identifier(value):
    return isinstance(value, (str, int)) and not isinstance(value, bool)


class Server:
    def __init__(self, reader=None, writer=None, options=None):
        self.input = reader or sys.stdin.buffer
        self.output = writer or sys.stdout.buffer
        self.options = options
        self.write_lock = threading.Lock()
        self.pending = {}
        self.sequence = 0
        self.thread = None  # Most recently selected thread; retained for local clients.
        self.turn = None
        self.threads = {}
        self.turns = {}
        self.loop = None
        self.lifecycle = asyncio.Lock()
        self.stopping = threading.Event()
        self.output_queue = None

    def _write(self, data):
        with self.write_lock:
            try:
                self.output.write(data)
                self.output.flush()
            except (OSError, ValueError):
                self.stopping.set()

    def send(self, message):
        import queue

        data = (json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        if len(data) > MAX_FRAME * 16:
            self.stopping.set()
            return
        if self.output_queue is None:
            self._write(data)
        else:
            try:
                self.output_queue.put_nowait(data)
            except queue.Full:
                self.stopping.set()  # A non-reading host cannot grow memory without bound.

    def notify(self, method, params):
        self.send({"method": method, "params": params})

    async def request(self, method, params):
        if len(self.pending) >= MAX_HANDLERS:
            raise RpcError(-32000, "Too many outstanding approval requests")
        self.sequence += 1
        identifier = f"panini-{self.sequence}"
        future = asyncio.get_running_loop().create_future()
        self.pending[identifier] = future
        try:
            self.send({"id": identifier, "method": method, "params": params})
            return await asyncio.wait_for(future, APPROVAL_TIMEOUT)
        except TimeoutError:
            raise RpcError(-32000, "Approval timed out; action declined") from None
        finally:
            self.pending.pop(identifier, None)

    async def serve(self):
        import concurrent.futures
        import queue

        self.loop = asyncio.get_running_loop()
        lines = asyncio.Queue(maxsize=128)
        self.output_queue = queue.Queue(maxsize=256)

        def write():
            while True:
                data = self.output_queue.get()
                try:
                    if data is None:
                        return
                    self._write(data)
                finally:
                    self.output_queue.task_done()

        def enqueue(line):
            future = asyncio.run_coroutine_threadsafe(lines.put(line), self.loop)
            while not self.stopping.is_set():
                try:
                    future.result(timeout=.2)
                    return
                except concurrent.futures.TimeoutError:
                    continue
                except (RuntimeError, concurrent.futures.CancelledError):
                    return
            future.cancel()

        def read():
            try:
                while not self.stopping.is_set():
                    line = self.input.readline(MAX_FRAME + 1)
                    if not line:
                        break
                    if len(line) > MAX_FRAME:
                        while line and not line.endswith(b"\n"):
                            line = self.input.readline(MAX_FRAME + 1)
                        enqueue(b'{"id":null,"method":null}\n')
                    else:
                        enqueue(line)
            finally:
                enqueue(None)

        threading.Thread(target=write, name="panini-cli-stdout", daemon=True).start()
        threading.Thread(target=read, name="panini-cli-stdin", daemon=True).start()
        handlers = set()
        controls = set()
        try:
            while not self.stopping.is_set():
                try:
                    line = await asyncio.wait_for(lines.get(), .2)
                except TimeoutError:
                    continue
                if line is None:
                    break
                try:
                    message = json.loads(line)
                except (ValueError, RecursionError):
                    self.send({"id": None, "error": {"code": -32700, "message": "Invalid JSON"}})
                    continue
                if not isinstance(message, dict) or not _identifier(message.get("id")):
                    if isinstance(message, dict) and message.get("method") == "initialized":
                        continue
                    self.send({"id": None, "error": {"code": -32600, "message": "Invalid RPC envelope or ID"}})
                    continue
                if "method" not in message:
                    future = self.pending.get(message["id"])
                    if future and not future.done():
                        error = message.get("error")
                        if error:
                            future.set_exception(RpcError(-32603, str(error)[:500]))
                        else:
                            future.set_result(message.get("result"))
                    continue
                # Dispatch is bounded but approval replies and interrupts keep flowing.
                control = message["method"] in {"turn/interrupt", "thread/close"}
                if (control and len(controls) >= 8) or (not control and len(handlers - controls) >= MAX_HANDLERS):
                    self.send({"id": message["id"], "error": {"code": -32000, "message": "Server busy; retry later"}})
                    continue
                task = asyncio.create_task(self.handle(message))
                handlers.add(task)
                task.add_done_callback(handlers.discard)
                if control:
                    controls.add(task)
                    task.add_done_callback(controls.discard)
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.cancel()
            if handlers:
                await asyncio.wait(set(handlers), timeout=2)
            for task in list(handlers):
                task.cancel()
            await asyncio.gather(*handlers, return_exceptions=True)
            for _, task in self.turns.values():
                task.cancel()
            await asyncio.gather(*(task for _, task in self.turns.values()), return_exceptions=True)
            for _, agent, host in self.threads.values():
                await agent.close()
                if host.flusher:
                    host.flusher.cancel()
            self.stopping.set()
            try:
                self.output_queue.put_nowait(None)
                deadline = self.loop.time() + 2
                while self.output_queue.unfinished_tasks and self.loop.time() < deadline:
                    await asyncio.sleep(.02)
            except (queue.Full, TimeoutError):
                pass

    async def handle(self, message):
        methods = {
            "initialize": self.initialize, "config/read": self.config_read, "model/list": self.model_list,
            "thread/start": self.thread_start, "thread/resume": self.thread_resume,
            "thread/list": self.thread_list, "thread/read": self.thread_read,
            "thread/fork": self.thread_fork, "thread/close": self.thread_close,
            "turn/start": self.turn_start, "turn/steer": self.turn_steer,
            "turn/interrupt": self.turn_interrupt,
            "skills/list": self.skills_list, "mcp/resources/list": self.mcp_resources,
            "mcp/resources/read": self.mcp_resource_read, "mcp/prompts/list": self.mcp_prompts,
            "mcp/prompts/get": self.mcp_prompt_get,
            "worker/start": self.worker_start, "worker/list": self.worker_list,
            "worker/read": self.worker_read, "worker/diff": self.worker_diff, "worker/stop": self.worker_stop,
            "terminal/start": self.terminal_start, "terminal/read": self.terminal_read,
            "terminal/send": self.terminal_send, "terminal/stop": self.terminal_stop, "terminal/resize": self.terminal_resize,
            "preview/start": self.preview_start, "preview/list": self.preview_list, "preview/stop": self.preview_stop,
        }
        identifier = message.get("id")
        try:
            if not _identifier(identifier) or not isinstance(message.get("method"), str):
                raise RpcError(-32600, "Invalid RPC request")
            params = message.get("params", {})
            if not isinstance(params, dict):
                raise RpcError(-32602, "params must be an object")
            handler = methods.get(message["method"])
            if handler is None:
                raise RpcError(-32601, f"Unsupported method: {message['method']}")
            result = await handler(params)
            self.send({"id": identifier, "result": result})
        except RpcError as exc:
            self.send({"id": identifier if _identifier(identifier) else None,
                       "error": {"code": exc.code, "message": str(exc)}})
        except (ValueError, KeyError, TypeError, OSError) as exc:
            self.send({"id": identifier, "error": {"code": -32602, "message": str(exc)[:500]}})
        except Exception as exc:  # noqa: BLE001 -- preserve the protocol on provider failures.
            self.send({"id": identifier, "error": {"code": -32603, "message": str(exc)[:500]}})

    async def initialize(self, params):
        return {"userAgent": f"panini-cli/{VERSION}", "protocolVersion": 1,
                "serverInfo": {"name": "panini-cli", "version": VERSION},
                "capabilities": {"durableThreads": True, "steering": True, "typedImages": True,
                                 "sessionPolicy": True, "skills": True, "mcp": True,
                                 "backgroundWorkers": True, "pty": True,
                                 "maxThreads": MAX_THREADS, "maxFrameBytes": MAX_FRAME}}

    async def config_read(self, params):
        from . import userconfig
        servers = {name: {key: spec[key] for key in ("command", "url") if key in spec}
                   for name, spec in userconfig.section("mcp_servers").items() if isinstance(spec, dict)}
        return {"config": {"model": Settings.from_env().model, "mcp_servers": servers}}

    async def model_list(self, params):
        from .model_router import model_options
        return {"data": [{"id": option.id, "description": option.description} for option in model_options()]}

    def _policy(self, params):
        from .config import session_policy
        if set(params) - {"cwd", "model", "developerInstructions", "sandboxPolicy", "sandbox", "approvalPolicy", "config", "threadId"}:
            raise RpcError(-32602, "Unsupported thread configuration fields")
        merged = dict(params)
        config = dict(merged.get("config") or {})
        if self.options:
            backend = getattr(self.options, "sandbox", None)
            if backend and "sandbox" not in config and "sandbox" not in merged and "sandboxPolicy" not in merged:
                config["sandbox"] = backend
            for option, key in (("network_host", "network_hosts"), ("recoverable", "recoverable"), ("local_preview", "local_preview")):
                value = getattr(self.options, option, None)
                if value and key not in config:
                    config[key] = value
            if getattr(self.options, "approval_mode", None) and "approvalPolicy" not in merged:
                merged["approvalPolicy"] = self.options.approval_mode
        merged["config"] = config
        return session_policy(merged)

    async def thread_start(self, params):
        async with self.lifecycle:
            return await self._thread_start(params)

    async def _thread_start(self, params, record=None):
        if len(self.threads) >= MAX_THREADS:
            raise RpcError(-32000, "Close a thread before opening another; limit is eight")
        try:
            workspace = Path(str(params.get("cwd") or "")).resolve(strict=True)
        except OSError:
            raise RpcError(-32602, "The workspace does not exist.") from None
        if not workspace.is_dir():
            raise RpcError(-32602, "The workspace must be a directory.")
        settings, mode, read_only = self._policy(params)
        host = ServerHost(self, mode=mode, read_only=read_only)
        host.thread_id = record["id"] if record else str(uuid4())
        agent = Agent(workspace, create_model(settings), settings, host,
                      developer_instructions=params.get("developerInstructions") or "", hooks=Hooks(workspace))
        agent.session_id = host.thread_id
        try:
            if record:
                agent.items = record["items"]
                state = record.get("metadata", {}).get("state", {})
                agent.cost_usd = state.get("cost_usd", 0)
                agent.usage = state.get("usage", agent.usage)
                agent.checkpoints = state.get("checkpoints", [])
                agent.session_base = state.get("session_base")
            await agent.start()
            facts = await agent.execution.status()
        except BaseException:
            await agent.close()
            raise
        self.thread = (host.thread_id, agent, host)
        self.threads[host.thread_id] = self.thread
        self._persist(agent, host, params.get("developerInstructions") or "")
        return {"thread": {"id": host.thread_id}, "model": settings.model, "cwd": str(workspace),
                "approvalPolicy": mode, "approvalsReviewer": "user", "sandbox": facts,
                "mcpErrors": agent.mcp.errors if agent.mcp else {}}

    def _persist(self, agent, host, instructions=None):
        from dataclasses import asdict
        from . import sessions
        from .loop import text_of
        from contextlib import suppress
        if instructions is not None:
            agent.saved_instructions = instructions
        title = next((text_of(i.get("content")) for i in agent.items if i.get("role") == "user"), "New conversation")
        with suppress(OSError):
            sessions.save(agent.session_id, agent.workspace.root, agent.items, title,
                metadata={"settings": asdict(agent.settings), "approvalPolicy": host.mode,
                          "readOnly": host.read_only, "developerInstructions": getattr(agent, "saved_instructions", ""),
                          "state": {"usage": agent.usage, "cost_usd": agent.cost_usd,
                                    "checkpoints": agent.checkpoints, "session_base": agent.session_base}})

    async def thread_resume(self, params):
        from . import sessions
        identifier = params.get("threadId")
        if identifier in self.threads:
            if set(params) - {"threadId"}:
                raise RpcError(-32602, "Close the loaded thread before changing its policy or model on resume")
            _, agent, host = self.threads[identifier]
            return {"thread": {"id": identifier}, "cwd": str(agent.workspace.root),
                    "model": agent.settings.model, "sandbox": await agent.execution.status()}
        record = sessions.load(identifier)
        meta = record.get("metadata", {})
        settings = meta.get("settings", {})
        restored = {"cwd": record["workspace"], "model": settings.get("model"),
                    "config": {key: settings[key] for key in ("sandbox", "network_hosts", "recoverable", "local_preview", "acceptance_checks", "mcp_enabled") if key in settings},
                    "sandbox": "readOnly" if meta.get("readOnly") else settings.get("sandbox"),
                    "approvalPolicy": meta.get("approvalPolicy", "untrusted"),
                    "developerInstructions": meta.get("developerInstructions", ""), **params}
        async with self.lifecycle:
            return await self._thread_start(restored, record)

    async def thread_list(self, params):
        from . import sessions
        return {"data": sessions.listing(params.get("cwd"))[:50]}

    async def thread_read(self, params):
        from . import sessions
        return {"thread": sessions.load(params.get("threadId"))}

    async def thread_fork(self, params):
        from . import sessions
        record = sessions.load(params.get("threadId"))
        record["id"] = str(uuid4())
        record.get("metadata", {}).get("state", {}).update(checkpoints=[], session_base=None)
        sessions.save(record["id"], record["workspace"], record["items"], record["title"], metadata=record.get("metadata"))
        return await self.thread_resume({**params, "threadId": record["id"]})

    def _thread(self, params):
        identifier = params.get("threadId")
        found = self.threads.get(identifier)
        if found is None and self.thread and self.thread[0] == identifier:
            found = self.thread
        if found is None:
            raise RpcError(-32602, "Unknown thread. Start a thread first.")
        return found

    async def thread_close(self, params):
        identifier, agent, host = self._thread(params)
        running = self.turns.get(identifier)
        if running and not running[1].done():
            running[1].cancel()
            await asyncio.gather(running[1], return_exceptions=True)
        await agent.close()
        self.threads.pop(identifier, None)
        self.turns.pop(identifier, None)
        if self.thread and self.thread[0] == identifier:
            self.thread = None
        return {}

    @staticmethod
    def _input(params):
        from .public_http import origin
        parts = params.get("input") or []
        if not isinstance(parts, list) or len(parts) > 32:
            raise RpcError(-32602, "input must contain at most 32 typed items")
        text, images = [], []
        for part in parts:
            if not isinstance(part, dict):
                raise RpcError(-32602, "Invalid input item")
            kind = part.get("type")
            if kind == "text" and isinstance(part.get("text"), str):
                text.append(part["text"])
            elif kind == "localImage" and isinstance(part.get("path"), str):
                images.append(part["path"])
            elif kind == "image" and isinstance(part.get("url"), str):
                origin(part["url"])
                images.append({"type": "input_image", "image_url": part["url"]})
            else:
                raise RpcError(-32602, "The turn needs text input or a valid image item.")
        value = "\n".join(text).strip()
        if not value and not images:
            raise RpcError(-32602, "The turn needs text input.")
        return value or "Inspect the attached images.", images

    async def turn_start(self, params):
        identifier, agent, host = self._thread(params)
        running = self.turns.get(identifier) or (self.turn if self.thread and self.thread[0] == identifier else None)
        if running and not running[1].done():
            raise RpcError(-32600, "A turn is already running.")
        if any(key in params for key in ("sandboxPolicy", "approvalPolicy", "cwd", "model")):
            raise RpcError(-32602, "Set execution overrides on thread/start or thread/resume, not on an active turn")
        text, images = self._input(params)
        turn_id = str(uuid4())
        host.turn_id = turn_id
        task = asyncio.create_task(self._run(agent, host, identifier, turn_id, text, params.get("outputSchema"), images))
        self.turn = self.turns[identifier] = (turn_id, task)
        return {"turn": {"id": turn_id, "status": "inProgress"}}

    async def _run(self, agent, host, thread_id, turn_id, text, output_schema, images=()):
        try:
            result = await agent.turn(text, output_schema, **({"images": images} if images else {}))
            status = {"completed": "completed", "interrupted": "interrupted"}.get(result.status, "failed")
            turn = {"id": turn_id, "status": status, "usage": result.usage}
            if hasattr(agent, "verification_results"):
                turn["verification"] = agent.verification_results
            if result.status != "completed":
                turn["error"] = {"message": result.error or result.status}
        except asyncio.CancelledError:
            turn = {"id": turn_id, "status": "interrupted"}
        except Exception as exc:  # noqa: BLE001 -- always send a completion notification.
            turn = {"id": turn_id, "status": "failed", "error": {"message": str(exc)[:500]}}
        if hasattr(agent, "workspace"):
            self._persist(agent, host)
        host.flush()
        self.notify("turn/completed", {"threadId": thread_id, "turn": turn})

    async def turn_steer(self, params):
        identifier, agent, _ = self._thread(params)
        running = self.turns.get(identifier)
        if not running or running[1].done() or params.get("expectedTurnId") != running[0]:
            raise RpcError(-32602, "expectedTurnId must match a running turn")
        text, images = self._input(params)
        if images:
            raise RpcError(-32602, "Steering accepts text only")
        agent.steer(text)
        return {"turnId": running[0]}

    async def turn_interrupt(self, params):
        if "threadId" in params:
            identifier, _, _ = self._thread(params)
            running = self.turns.get(identifier)
        else:
            running = self.turn
        if running and params.get("turnId") == running[0] and not running[1].done():
            running[1].cancel()
            await asyncio.wait([running[1]], timeout=10)
        return {}

    async def skills_list(self, params):
        _, agent, _ = self._thread(params)
        return {"data": agent.skills.catalog()}

    async def _mcp(self, params, method):
        _, agent, _ = self._thread(params)
        if not agent.mcp:
            raise RpcError(-32602, "No MCP servers configured")
        return await agent.mcp.protocol(params.get("server"), method, params.get("arguments") or {})

    async def mcp_resources(self, params):
        return await self._mcp(params, "resources/list")

    async def mcp_resource_read(self, params):
        return await self._mcp(params, "resources/read")

    async def mcp_prompts(self, params):
        return await self._mcp(params, "prompts/list")

    async def mcp_prompt_get(self, params):
        return await self._mcp(params, "prompts/get")

    async def worker_start(self, params):
        _, agent, _ = self._thread(params)
        return await agent.workers.start(params.get("task"), params.get("mode") or "read-only")

    async def worker_list(self, params):
        return {"data": self._thread(params)[1].workers.list()}

    async def worker_read(self, params):
        return self._thread(params)[1].workers.get(params.get("workerId")).info()

    async def worker_diff(self, params):
        return {"diff": await self._thread(params)[1].workers.diff(params.get("workerId"))}

    async def worker_stop(self, params):
        return await self._thread(params)[1].workers.stop(params.get("workerId"))

    async def _tool(self, params, name):
        _, agent, _ = self._thread(params)
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise RpcError(-32602, "arguments must be an object")
        # The shared tool adapters apply the thread's actual approval and sandbox policy.
        output, declined = await (agent._preview(name, arguments) if name.startswith("preview_")
                                  else agent._terminal(name, arguments))
        return {"output": output, "declined": declined}

    async def terminal_start(self, params):
        return await self._tool(params, "terminal_start")

    async def terminal_read(self, params):
        return await self._tool(params, "terminal_read")

    async def terminal_send(self, params):
        return await self._tool(params, "terminal_send")

    async def terminal_stop(self, params):
        return await self._tool(params, "terminal_stop")

    async def terminal_resize(self, params):
        return await self._tool(params, "terminal_resize")

    async def preview_start(self, params):
        return await self._tool(params, "preview_start")

    async def preview_list(self, params):
        return await self._tool(params, "preview_list")

    async def preview_stop(self, params):
        return await self._tool(params, "preview_stop")


def serve(options=None):
    asyncio.run(Server(options=options).serve())
