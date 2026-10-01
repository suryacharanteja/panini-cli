import asyncio
import os
import sys

from panini_cli.agent.config import Settings
from panini_cli.agent.loop import Agent
from panini_cli.agent.model import ScriptedModel
from panini_cli.agent.terminals import Terminals

REPL = """
import sys
print("ready", flush=True)
for line in sys.stdin:
    line = line.strip()
    if line == "quit":
        break
    print(f"echo:{line.upper()}", flush=True)
print("bye", flush=True)
"""


def command(script):
    return f'& "{sys.executable}" -u "{script}"' if os.name == "nt" else f'"{sys.executable}" -u "{script}"'


def test_session_start_send_read_and_stop(tmp_path):
    script = tmp_path / "repl.py"
    script.write_text(REPL, encoding="utf-8")

    async def go():
        sessions = Terminals(tmp_path, Settings(sandbox="none"))
        terminal = await sessions.start(command(script))
        assert "ready" in await sessions.read(terminal.id, 10)
        await sessions.send(terminal.id, "hello")
        assert "echo:HELLO" in await sessions.read(terminal.id, 10)
        assert sessions.list()[0]["running"]
        await sessions.send(terminal.id, "quit")
        assert "bye" in await sessions.read(terminal.id, 10)
        await asyncio.wait_for(terminal.process.wait(), 10)
        assert await sessions.stop(terminal.id) == 0

    asyncio.run(go())


def test_stop_kills_a_process_that_never_ends(tmp_path):
    async def go():
        sessions = Terminals(tmp_path, Settings(sandbox="none"))
        forever = (
            f'& "{sys.executable}" -c "import time; time.sleep(600)"' if os.name == "nt" else "sleep 600"
        )
        terminal = await sessions.start(forever)
        await asyncio.sleep(0.5)
        await sessions.close()
        assert terminal.process.returncode is not None

    asyncio.run(go())


class Host:
    def __init__(self, decisions):
        self.decisions = list(decisions)
        self.asked = []

    async def started(self, item):
        pass

    async def delta(self, *args):
        pass

    async def completed(self, item):
        pass

    async def approve(self, item):
        self.asked.append(item["command"])
        return self.decisions.pop(0)


def test_agent_uses_sessions_with_approval(tmp_path):
    script = tmp_path / "repl.py"
    script.write_text(REPL, encoding="utf-8")
    model = ScriptedModel(
        [
            {"calls": [{"name": "terminal_start", "arguments": {"command": command(script)}}]},
            {
                "calls": [
                    {
                        "name": "terminal_send",
                        "arguments": {"session_id": "t1", "input": "abc", "wait_seconds": 5},
                    }
                ]
            },
            {
                "calls": [
                    {
                        "name": "terminal_send",
                        "arguments": {"session_id": "t1", "input": "x", "wait_seconds": None},
                    }
                ]
            },
            {"calls": [{"name": "terminal_read", "arguments": {"session_id": "nope", "wait_seconds": None}}]},
            {"text": "done"},
        ]
    )
    host = Host(["accept", "accept", "decline"])
    agent = Agent(tmp_path, model, Settings(sandbox="none"), host)

    async def go():
        try:
            await agent.turn("talk to the repl")
        finally:
            await agent.close()

    asyncio.run(go())
    outputs = [i["output"] for i in model.requests[-1]["items"] if i.get("type") == "function_call_output"]
    assert "Session t1 (running)" in outputs[0] and "ready" in outputs[0]
    assert "echo:ABC" in outputs[1]
    assert "declined" in outputs[2]
    assert outputs[3].startswith("Error: No terminal session nope")
    assert host.asked[1] == "[session t1] input: abc"
