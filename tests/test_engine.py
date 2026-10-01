import asyncio
import hashlib

import pytest
from panini_cli.engine import CodeEngine
from panini_cli.verification import FileCondition


@pytest.fixture
def anyio_backend():
    return "asyncio"


class FakeServer:
    def __init__(self, argv, cwd, callback):
        self.cwd, self.callback, self.events, self.requests = (
            cwd,
            callback,
            asyncio.Queue(),
            [],
        )
        self.closed = False

    async def start(self):
        return {}

    async def request(self, method, params, **kwargs):
        self.requests.append((method, params))
        if method == "config/read":
            return {
                "config": {
                    "mcp_servers": {
                        "desktop": {"tool_timeout_sec": None},
                        "remote.tools": {},
                    }
                }
            }
        if method == "thread/start":
            return {
                "thread": {"id": "thread"},
                "cwd": str(self.cwd),
                "approvalPolicy": "untrusted",
                "approvalsReviewer": "user",
                "sandbox": {"type": "workspaceWrite", "networkAccess": False},
            }
        if method == "turn/start":
            self.queue(
                "item/completed",
                item={
                    "id": "cmd",
                    "type": "commandExecution",
                    "command": "test",
                    "cwd": str(self.cwd),
                    "exitCode": 0,
                    "aggregatedOutput": "PASS",
                    "status": "completed",
                },
            )
            self.queue(
                "item/completed",
                item={
                    "id": "cmd",
                    "type": "commandExecution",
                    "command": "test",
                    "cwd": str(self.cwd),
                    "exitCode": 0,
                    "aggregatedOutput": "PASS",
                    "status": "completed",
                },
            )
            self.queue(
                "item/completed",
                item={
                    "id": "msg",
                    "type": "agentMessage",
                    "text": "I verified everything",
                },
            )
            self.queue("turn/completed", turn={"id": "turn", "status": "completed"})
            return {"turn": {"id": "turn"}}
        return {}

    def queue(self, method, **params):
        self.events.put_nowait(
            {
                "method": method,
                "params": {"threadId": "thread", "turnId": "turn", **params},
            }
        )

    async def next_event(self):
        return await self.events.get()

    async def close(self):
        self.closed = True


async def run(engine, **kwargs):
    async def confirm(review):
        return False

    async def emit(record):
        pass

    return await engine.run(
        "run",
        "Synthetic task",
        confirm=confirm,
        emit=emit,
        check=lambda: None,
        **kwargs,
    )


@pytest.mark.anyio
async def test_live_activity_filters_foreign_turns_and_keeps_output(tmp_path):
    class StreamingServer(FakeServer):
        async def request(self, method, params, **kwargs):
            if method == "turn/start":
                self.events.put_nowait(
                    {
                        "method": "item/commandExecution/outputDelta",
                        "params": {
                            "threadId": "other",
                            "turnId": "turn",
                            "itemId": "cmd",
                            "delta": "foreign",
                        },
                    }
                )
                self.queue(
                    "item/started",
                    item={"id": "cmd", "type": "commandExecution", "command": "test"},
                )
                self.queue(
                    "item/commandExecution/outputDelta",
                    itemId="cmd",
                    delta="streamed output",
                )
            return await super().request(method, params, **kwargs)

    records = []

    async def emit(record):
        records.append(record)

    engine = CodeEngine(
        tmp_path,
        tmp_path / "journal.db",
        executable="fixture",
        server_factory=StreamingServer,
    )
    try:
        await engine.run(
            "stream",
            "fixture",
            confirm=lambda _: None,
            emit=emit,
            check=lambda: None,
            model="lightweight",
            reasoning_effort="low",
        )
        assert any(r.get("type") == "item.started" for r in records)
        assert [r["delta"] for r in records if r.get("type") == "item.delta"] == ["streamed output"]
        started = next(p for m, p in engine.server.requests if m == "thread/start")
        assert started["model"] == "lightweight" and started["config"]["model_reasoning_effort"] == "low"
    finally:
        await engine.close()


@pytest.mark.anyio
async def test_completion_claim_is_partial_and_duplicate_events_are_not_executed(
    tmp_path,
):
    engine = CodeEngine(
        tmp_path,
        tmp_path / "journal.db",
        executable="fixture",
        server_factory=FakeServer,
    )
    try:
        result = await run(engine)
        assert result.status == "partial" and not result.verified and len(result.commands) == 1
        start = next(params for name, params in engine.server.requests if name == "thread/start")
        assert start["config"]["mcp_servers"]["desktop"]["enabled"] is False
        assert start["config"]["mcp_servers"]["remote.tools"] == {"enabled": False}
        assert engine.server.closed
    finally:
        await engine.close()


@pytest.mark.anyio
async def test_only_host_postcondition_can_verify_completion(tmp_path):
    (tmp_path / "answer").write_bytes(b"actual")
    engine = CodeEngine(
        tmp_path,
        tmp_path / "journal.db",
        executable="fixture",
        server_factory=FakeServer,
    )
    try:
        result = await run(
            engine,
            postconditions=[FileCondition("answer", hashlib.sha256(b"actual").hexdigest())],
        )
        assert result.status == "verified_complete" and result.verified
        assert result.verification[0]["bytes"] == 6
    finally:
        await engine.close()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "policy",
    [
        {"type": "dangerFullAccess"},
        {"type": "workspaceWrite", "networkAccess": False, "writableRoots": ["C:/"]},
    ],
)
async def test_policy_downgrade_aborts_before_a_turn(tmp_path, policy):
    class UnsafeServer(FakeServer):
        async def request(self, method, params, **kwargs):
            result = await super().request(method, params, **kwargs)
            if method == "thread/start":
                result["sandbox"] = policy
            return result

    engine = CodeEngine(
        tmp_path,
        tmp_path / "journal.db",
        executable="fixture",
        server_factory=UnsafeServer,
    )
    try:
        result = await run(engine)
        assert result.status == "unknown_effect"
        assert not any(name == "turn/start" for name, _ in engine.server.requests)
        assert engine.server.closed
    finally:
        await engine.close()


@pytest.mark.anyio
async def test_journal_prevents_blind_redispatch_after_restart(tmp_path):
    first = CodeEngine(
        tmp_path,
        tmp_path / "journal.db",
        executable="fixture",
        server_factory=FakeServer,
    )
    await run(first)
    await first.close()
    second = CodeEngine(
        tmp_path,
        tmp_path / "journal.db",
        executable="fixture",
        server_factory=FakeServer,
    )
    try:
        with pytest.raises(ValueError, match="replay"):
            await run(second)
        assert second.server is None
    finally:
        await second.close()
