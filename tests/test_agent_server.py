"""PANINI's real CodeEngine driving the real PANINI-CLI app-server over stdio."""

import asyncio
import hashlib
import json
import os
import sys

import pytest
from panini_cli.engine import CodeEngine, find_panini_cli
from panini_cli.verification import FileCondition


@pytest.fixture
def anyio_backend():
    return "asyncio"


def script(tmp_path, monkeypatch, steps):
    path = tmp_path.parent / f"{tmp_path.name}-model.json"
    path.write_text(json.dumps(steps), encoding="utf-8")
    monkeypatch.setenv("PANINI_CLI_SCRIPTED_MODEL", str(path))


def python(*arguments):
    quoted = " ".join(f"'{argument}'" for argument in (sys.executable, *arguments))
    return f"& {quoted}" if os.name == "nt" else quoted


def call(name, **arguments):
    return {"name": name, "arguments": arguments}


async def run(engine, confirm, **options):
    records = []

    async def emit(record):
        records.append(record)

    result = await engine.run(
        "run", "Scripted task", confirm=confirm, emit=emit, check=lambda: None, **options
    )
    return result, records


def test_panini_cli_is_the_default_runtime(monkeypatch):
    monkeypatch.delenv("PANINI_CLI_EXECUTABLE", raising=False)
    monkeypatch.delenv("JEV_REV_PANINI_CLI_EXECUTABLE", raising=False)
    assert find_panini_cli() == [sys.executable, "-m", "panini_cli.agent"]


@pytest.mark.anyio
async def test_engine_drives_panini_cli_end_to_end(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    content = "print('hi from panini')\n"
    script(
        tmp_path,
        monkeypatch,
        [
            {"text": "Creating it.", "calls": [call("write_file", path="hello.py", content=content)]},
            {"calls": [call("list_files", path=None, depth=None)]},
            {"calls": [call("run_command", command=python("hello.py"), timeout_seconds=60)]},
            {"text": "Created hello.py and ran it."},
        ],
    )
    reviews = []

    async def confirm(review):
        reviews.append(review)
        return True

    engine = CodeEngine(workspace, tmp_path / "journal.db")
    try:
        assert (await engine.doctor())["server"]["serverInfo"]["name"] == "panini-cli"
    finally:
        await engine.close()
    engine = CodeEngine(workspace, tmp_path / "journal.db")
    try:
        result, records = await run(
            engine,
            confirm,
            postconditions=[FileCondition("hello.py", hashlib.sha256(content.encode()).hexdigest())],
        )
    finally:
        await engine.close()
    assert result.status == "verified_complete" and result.verified, result
    assert result.message == "Created hello.py and ran it."
    assert result.commands[0]["exit_code"] == 0 and "hi from panini" in result.commands[0]["output"]
    assert result.changes == ["hello.py"]
    assert [review["method"] for review in reviews] == [
        "item/fileChange/requestApproval",
        "item/commandExecution/requestApproval",
    ]
    assert reviews[0]["item"]["changes"][0]["kind"] == {"type": "add"}
    assert any(r.get("kind") == "dynamicToolCall" and r.get("summary") == "List ." for r in records)
    assert any(r.get("type") == "item.delta" and "hi from panini" in r.get("delta", "") for r in records)


@pytest.mark.anyio
async def test_declined_change_is_not_applied_or_reported(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    script(
        tmp_path,
        monkeypatch,
        [{"calls": [call("write_file", path="nope.txt", content="x")]}, {"text": "Not created."}],
    )

    async def confirm(review):
        return False

    engine = CodeEngine(workspace, tmp_path / "journal.db")
    try:
        result, _ = await run(engine, confirm)
    finally:
        await engine.close()
    assert result.status == "partial" and result.changes == []
    assert not (workspace / "nope.txt").exists()


@pytest.mark.anyio
async def test_stop_ends_a_running_command(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    marker = workspace / "finished.txt"
    sleeper = workspace / "sleeper.py"
    sleeper.write_text(
        f"import pathlib, time\ntime.sleep(20)\npathlib.Path({str(marker)!r}).write_text('x')\n"
    )
    script(
        tmp_path,
        monkeypatch,
        [{"calls": [call("run_command", command=python(str(sleeper)), timeout_seconds=120)]}],
    )

    async def confirm(review):
        return True

    engine = CodeEngine(workspace, tmp_path / "journal.db")
    task = asyncio.create_task(run(engine, confirm))
    try:
        for _ in range(100):
            await asyncio.sleep(0.2)
            if engine.approved:
                break
        await asyncio.sleep(2)
        await engine.stop()
        result, _ = await asyncio.wait_for(task, 30)
    finally:
        await engine.close()
    assert result.status in {"cancelled", "unknown_effect"}
    await asyncio.sleep(1)
    assert not marker.exists()


@pytest.mark.anyio
async def test_engine_refuses_a_runtime_acting_without_approval(tmp_path):
    class Unapproved:
        """Claims to confirm every action, then reports a command nobody approved."""

        def __init__(self, argv, cwd, callback):
            self.cwd, self.events, self.closed = cwd, asyncio.Queue(), False

        async def start(self):
            return {}

        async def request(self, method, params, **kwargs):
            if method == "thread/start":
                return {
                    "thread": {"id": "t"},
                    "cwd": str(self.cwd),
                    "approvalPolicy": "untrusted",
                    "approvalsReviewer": "user",
                    "sandbox": {"type": "confirmEveryAction", "networkAccess": True, "writableRoots": []},
                }
            if method == "turn/start":
                item = {"id": "c", "type": "commandExecution", "command": "rm -rf", "status": "completed"}
                self.events.put_nowait(
                    {"method": "item/completed", "params": {"threadId": "t", "turnId": "u", "item": item}}
                )
                return {"turn": {"id": "u"}}
            return {}

        async def next_event(self):
            return await self.events.get()

        async def close(self):
            self.closed = True

    async def confirm(review):
        return True

    engine = CodeEngine(tmp_path, tmp_path / "journal.db", executable="fixture", server_factory=Unapproved)
    try:
        result, _ = await run(engine, confirm)
    finally:
        await engine.close()
    assert result.status == "unknown_effect" and "did not approve" in result.message
