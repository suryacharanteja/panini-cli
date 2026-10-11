"""Protocol rejection, disconnect and turn failure paths without a model service."""

import asyncio
import io
import json
from types import SimpleNamespace

import pytest

from panini_cli.agent.server import RpcError, Server


def test_successful_thread_replacement_and_turn_lifecycle(tmp_path, monkeypatch):
    from panini_cli.agent import server as server_module
    from panini_cli.agent.model import ScriptedModel
    monkeypatch.setenv("PANINI_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(server_module, "create_model", lambda settings: ScriptedModel([]))
    async def run():
        output = io.BytesIO()
        server = Server(io.BytesIO(), output)
        first = await server.thread_start({"cwd": str(tmp_path)})
        second = await server.thread_start({"cwd": str(tmp_path)})
        assert first["thread"]["id"] != second["thread"]["id"]
        assert second["approvalPolicy"] == "untrusted"
        async def turn(*args):
            return SimpleNamespace(status="completed", usage={"total_tokens": 0}, error="")
        server.thread[1].turn = turn
        try:
            started = await server.turn_start({"threadId": second["thread"]["id"],
                                              "input": [{"type": "text", "text": "test"}]})
            await server.turn[1]
            assert started["turn"]["status"] == "inProgress"
            assert await server.turn_interrupt({"turnId": started["turn"]["id"]}) == {}
            messages = [json.loads(line) for line in output.getvalue().splitlines()]
            assert messages[-1]["params"]["turn"]["status"] == "completed"
        finally:
            await server.thread[1].close()
    asyncio.run(run())


@pytest.mark.parametrize("method,params,code", [
    ("unknown", {}, -32601),
    ("turn/start", {"threadId": "missing"}, -32602),
    ("thread/start", {"cwd": "missing-directory-for-protocol-test"}, -32602),
])
def test_protocol_rejects_invalid_requests(method, params, code):
    output = io.BytesIO()
    server = Server(io.BytesIO(), output)
    asyncio.run(server.handle({"id": 1, "method": method, "params": params}))
    reply = json.loads(output.getvalue())
    assert reply["id"] == 1 and reply["error"]["code"] == code


def test_turn_requires_nonempty_text():
    server = Server(io.BytesIO(), io.BytesIO())
    server.thread = ("thread", None, None)
    with pytest.raises(RpcError, match="text input"):
        asyncio.run(server.turn_start({"threadId": "thread", "input": [{"type": "image"}]}))


def test_cancelled_host_request_removes_pending_entry():
    async def run():
        server = Server(io.BytesIO(), io.BytesIO())
        server.loop = asyncio.get_running_loop()
        task = asyncio.create_task(server.request("approval", {}))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not server.pending
    asyncio.run(run())


@pytest.mark.parametrize("failure", [False, True])
def test_turn_failure_or_cancellation_always_notifies_host(failure):
    output = io.BytesIO()
    server = Server(io.BytesIO(), output)
    async def turn(*args):
        if failure:
            raise ValueError("synthetic model failure")
        raise asyncio.CancelledError()
    agent = SimpleNamespace(turn=turn)
    flushed = []
    host = SimpleNamespace(flush=lambda: flushed.append(True))
    asyncio.run(server._run(agent, host, "thread", "turn", "test", None))
    notification = json.loads(output.getvalue())
    assert notification["method"] == "turn/completed"
    assert notification["params"]["turn"]["status"] == ("failed" if failure else "interrupted")
    assert flushed == [True]
