"""Bounded, bidirectional coding app-server JSONL transport (no shell interpolation)."""

import asyncio
import json

from .process import ProcessTree


class ProtocolError(RuntimeError):
    pass


class AppServer:
    def __init__(self, argv, cwd, on_request, env=None):
        self.argv, self.cwd, self.on_request, self.env = argv, cwd, on_request, env
        self.tree = ProcessTree()
        self.pending, self.handlers = {}, set()
        self.events = asyncio.Queue(maxsize=2048)
        self.sequence = 0
        self.reader = None
        self.failure = None
        self.write_lock = asyncio.Lock()
        self.close_lock = asyncio.Lock()
        self.closed = False

    async def start(self):
        self.process = await self.tree.start(self.argv, self.cwd, self.env)
        if self.closed:
            await self.tree.close()
            raise ProtocolError("Coding engine was stopped during startup")
        self.reader = asyncio.create_task(self._read())
        try:
            result = await self.request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "panini_cli",
                        "title": "Jev Rev Code",
                        "version": "0.1.0",
                    },
                    "capabilities": {"experimentalApi": False},
                },
            )
            await self.send({"method": "initialized", "params": {}})
            return result
        except BaseException:
            await self.close()
            raise

    async def send(self, value):
        if self.closed:
            raise ProtocolError("Coding engine is stopped")
        if self.failure:
            raise self.failure
        data = (json.dumps(value, separators=(",", ":")) + "\n").encode()
        if len(data) > 8 * 1024 * 1024:
            raise ProtocolError("Protocol frame too large")
        async with self.write_lock:
            self.process.stdin.write(data)
            await self.process.stdin.drain()

    async def request(self, method, params, timeout=30):
        self.sequence += 1
        identifier = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[identifier] = future
        try:
            await self.send({"id": identifier, "method": method, "params": params})
            return await asyncio.wait_for(future, timeout)
        finally:
            self.pending.pop(identifier, None)

    async def _respond(self, message):
        try:
            result = await self.on_request(message["method"], message.get("params", {}))
            await self.send({"id": message["id"], "result": result})
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- Any host callback failure must deny the server request.
            try:
                await self.send(
                    {
                        "id": message["id"],
                        "error": {
                            "code": -32603,
                            "message": "Request declined by coordinator",
                        },
                    }
                )
            except (OSError, ProtocolError) as exc:
                self._fail(exc)

    def _fail(self, error):
        self.failure = ProtocolError(str(error))
        for future in self.pending.values():
            if not future.done():
                future.set_exception(self.failure)
        # Never silently drop authoritative events and continue executing.
        while self.events.full():
            self.events.get_nowait()
        self.events.put_nowait(self.failure)

    async def _read(self):
        try:
            while line := await self.process.stdout.readline():
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise ProtocolError("Expected protocol object")
                if "method" in message:
                    if "id" in message:
                        if len(self.handlers) >= 16:
                            raise ProtocolError("Too many approval requests")
                        task = asyncio.create_task(self._respond(message))
                        self.handlers.add(task)
                        task.add_done_callback(self.handlers.discard)
                    else:
                        self.events.put_nowait(message)
                elif "id" in message:
                    future = self.pending.get(message["id"])
                    if future and not future.done():
                        if "error" in message:
                            future.set_exception(ProtocolError(str(message["error"])[:1000]))
                        elif "result" in message:
                            future.set_result(message["result"])
                        else:
                            raise ProtocolError("Response missing result")
                else:
                    raise ProtocolError("Malformed protocol frame")
            raise ProtocolError("Coding engine disconnected; effects may already have occurred")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 -- Malformed peer data must stop execution, including unexpected shapes.
            self._fail(exc)
            await self.tree.close()

    async def next_event(self):
        item = await self.events.get()
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self):
        async with self.close_lock:
            if self.closed:
                return
            self.closed = True
            # A concurrent run may be waiting for the next event. Closing the
            # reader alone leaves that consumer blocked until its outer timeout.
            if self.failure is None:
                self._fail(ProtocolError("Coding engine was stopped"))
            for task in list(self.handlers):
                task.cancel()
            if self.reader:
                self.reader.cancel()
            await asyncio.gather(
                *self.handlers,
                *([self.reader] if self.reader else []),
                return_exceptions=True,
            )
            # A request cut off by a concurrent stop fails as stopped; it was not itself cancelled.
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(ProtocolError("Coding engine was stopped"))
            await self.tree.close()
