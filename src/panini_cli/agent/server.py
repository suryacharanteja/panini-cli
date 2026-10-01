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
POLICY = {"type": "confirmEveryAction", "networkAccess": True, "fileTools": "workspace"}
DELTA_INTERVAL = 0.15
DELTA_BYTES = 16384


class RpcError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class ServerHost:
    """Forwards agent activity as protocol notifications; approvals go to the host."""

    def __init__(self, server):
        self.server = server
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
        if item["type"] == "commandExecution":
            method = "item/commandExecution/requestApproval"
            scope = item.get("executionScope", "workspace")
            params = self._params(
                itemId=item["id"],
                command=item["command"],
                cwd=item["cwd"],
                executionScope=scope,
                reason=("PANINI-CLI requests host access for this command. It can reach files and the network "
                        "available to your Windows user." if scope == "host" else
                        "PANINI-CLI wants to run a command in the selected sandbox."),
            )
        else:
            method = "item/fileChange/requestApproval"
            params = self._params(itemId=item["id"], reason="PANINI-CLI wants to change a file.")
        try:
            result = await self.server.request(method, params)
        except RpcError:
            return "decline"
        decision = (result or {}).get("decision")
        return decision if decision in ("accept", "decline", "cancel") else "decline"


class Server:
    def __init__(self, reader=None, writer=None):
        self.input = reader or sys.stdin.buffer
        self.output = writer or sys.stdout.buffer
        self.write_lock = threading.Lock()
        self.pending = {}
        self.sequence = 0
        self.thread = None  # (thread_id, agent, host)
        self.turn = None  # (turn_id, task)
        self.loop = None

    def send(self, message):
        data = (json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        with self.write_lock:
            try:
                self.output.write(data)
                self.output.flush()
            except (OSError, ValueError):
                pass  # The host has gone; shutdown follows when stdin closes.

    def notify(self, method, params):
        self.send({"method": method, "params": params})

    async def request(self, method, params):
        self.sequence += 1
        identifier = f"panini-{self.sequence}"
        future = self.loop.create_future()
        self.pending[identifier] = future
        try:
            self.send({"id": identifier, "method": method, "params": params})
            return await future
        finally:
            self.pending.pop(identifier, None)

    async def serve(self):
        self.loop = asyncio.get_running_loop()
        lines = asyncio.Queue()

        def read():
            try:
                for line in self.input:
                    self.loop.call_soon_threadsafe(lines.put_nowait, line)
            finally:
                self.loop.call_soon_threadsafe(lines.put_nowait, None)

        threading.Thread(target=read, name="panini-cli-stdin", daemon=True).start()
        handlers = set()
        try:
            while (line := await lines.get()) is not None:
                try:
                    message = json.loads(line)
                except ValueError:
                    print("panini-cli: ignored a malformed protocol line", file=sys.stderr)
                    continue
                if not isinstance(message, dict):
                    continue
                if "method" in message and "id" in message:
                    task = asyncio.create_task(self.handle(message))
                    handlers.add(task)
                    task.add_done_callback(handlers.discard)
                elif "method" not in message and "id" in message:
                    future = self.pending.get(message["id"])
                    if future and not future.done():
                        error = message.get("error")
                        if error:
                            future.set_exception(
                                RpcError(error.get("code", -32603), str(error.get("message", "")))
                            )
                        else:
                            future.set_result(message.get("result"))
        finally:
            # The host disconnected: stop the running turn and everything it started.
            if self.turn and not self.turn[1].done():
                self.turn[1].cancel()
                await asyncio.gather(self.turn[1], return_exceptions=True)
            # Requests received before the disconnect still get their (quick) replies.
            if handlers:
                await asyncio.wait(set(handlers), timeout=2)
            for task in handlers:
                task.cancel()
            if self.thread:
                await self.thread[1].close()  # Terminal sessions end with the host.

    async def handle(self, message):
        identifier, method = message["id"], message["method"]
        handlers = {
            "initialize": self.initialize,
            "config/read": self.config_read,
            "thread/start": self.thread_start,
            "turn/start": self.turn_start,
            "turn/interrupt": self.turn_interrupt,
        }
        try:
            handler = handlers.get(method)
            if handler is None:
                raise RpcError(-32601, f"Unsupported method: {method}")
            result = await handler(message.get("params") or {})
            self.send({"id": identifier, "result": result})
        except RpcError as exc:
            self.send({"id": identifier, "error": {"code": exc.code, "message": str(exc)}})
        except Exception as exc:  # noqa: BLE001 -- Report any failure to the host instead of dying.
            self.send({"id": identifier, "error": {"code": -32603, "message": str(exc)[:500]}})

    async def initialize(self, params):
        return {
            "userAgent": f"panini-cli/{VERSION}",
            "serverInfo": {"name": "panini-cli", "version": VERSION},
        }

    async def config_read(self, params):
        return {"config": {"model": Settings.from_env().model, "mcp_servers": {}}}

    async def thread_start(self, params):
        if self.turn and not self.turn[1].done():
            raise RpcError(-32600, "A turn is still running.")
        try:
            workspace = Path(str(params.get("cwd") or "")).resolve(strict=True)
        except OSError:
            raise RpcError(-32602, "The workspace does not exist.") from None
        if not workspace.is_dir():
            raise RpcError(-32602, "The workspace must be a directory.")
        config = params.get("config") or {}
        settings = Settings.from_env(
            model=params.get("model"), reasoning_effort=config.get("model_reasoning_effort")
        )
        host = ServerHost(self)
        host.thread_id = str(uuid4())
        agent = Agent(
            workspace,
            create_model(settings),
            settings,
            host,
            developer_instructions=params.get("developerInstructions") or "",
            hooks=Hooks(workspace),
        )
        agent.session_id = host.thread_id
        if self.thread:
            await self.thread[1].close()  # A new thread replaces the old one and its sessions.
        self.thread = (host.thread_id, agent, host)
        return {
            "thread": {"id": host.thread_id},
            "model": settings.model,
            "cwd": str(agent.workspace.root),
            # Every command and file change is sent to the host for approval.
            "approvalPolicy": "untrusted",
            "approvalsReviewer": "user",
            "sandbox": {
                **POLICY,
                "writableRoots": [str(agent.workspace.root)],
                # Commands also run in a no-network container when the Docker sandbox is on.
                "commandIsolation": agent.settings.sandbox,
            },
        }

    async def turn_start(self, params):
        if not self.thread or params.get("threadId") != self.thread[0]:
            raise RpcError(-32602, "Unknown thread. Start a thread first.")
        if self.turn and not self.turn[1].done():
            raise RpcError(-32600, "A turn is already running.")
        text = "\n".join(
            str(part.get("text", ""))
            for part in params.get("input") or []
            if isinstance(part, dict) and part.get("type") == "text"
        ).strip()
        if not text:
            raise RpcError(-32602, "The turn needs text input.")
        thread_id, agent, host = self.thread
        turn_id = str(uuid4())
        host.turn_id = turn_id
        # The task first runs after this response is sent, so every notification follows it.
        task = asyncio.create_task(
            self._run(agent, host, thread_id, turn_id, text, params.get("outputSchema"))
        )
        self.turn = (turn_id, task)
        return {"turn": {"id": turn_id, "status": "inProgress"}}

    async def _run(self, agent, host, thread_id, turn_id, text, output_schema):
        try:
            result = await agent.turn(text, output_schema)
            status = {"completed": "completed", "interrupted": "interrupted"}.get(result.status, "failed")
            turn = {"id": turn_id, "status": status, "usage": result.usage}
            if result.status != "completed":
                turn["error"] = {"message": result.error or result.status}
        except asyncio.CancelledError:
            turn = {"id": turn_id, "status": "interrupted"}
        except Exception as exc:  # noqa: BLE001 -- The host must always learn how the turn ended.
            turn = {"id": turn_id, "status": "failed", "error": {"message": str(exc)[:500]}}
        host.flush()
        self.notify("turn/completed", {"threadId": thread_id, "turn": turn})

    async def turn_interrupt(self, params):
        if self.turn and params.get("turnId") == self.turn[0] and not self.turn[1].done():
            self.turn[1].cancel()
            await asyncio.wait([self.turn[1]], timeout=10)
        return {}


def serve():
    asyncio.run(Server().serve())
