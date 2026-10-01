import json

import pytest
from panini_cli.agent import receipts
from panini_cli.agent.config import Settings
from panini_cli.agent.loop import Agent
from panini_cli.agent.model import ScriptedModel


@pytest.fixture
def anyio_backend():
    return "asyncio"


class Host:
    async def started(self, item):
        pass

    async def delta(self, item_id, kind, text):
        pass

    async def completed(self, item):
        pass

    async def approve(self, item):
        return "accept"


@pytest.mark.anyio
async def test_tool_receipts_survive_turn_and_record_approval_without_payloads(tmp_path, monkeypatch):
    monkeypatch.setenv("PANINI_DATA_DIR", str(tmp_path / "data"))
    workspace = tmp_path / "work"
    workspace.mkdir()
    agent = Agent(
        workspace,
        ScriptedModel([
            {"calls": [{"name": "write_file", "arguments": {"path": "proof.txt", "content": "PRIVATE_VALUE"}}]},
            {"text": "Done."},
        ]),
        Settings(sandbox="none"),
        Host(),
    )
    agent.session_id = "test-receipt-session"
    result = await agent.turn("Create the proof file.")
    assert result.status == "completed"
    path = receipts.path_for(agent.session_id)
    assert path.is_file()
    assert "PRIVATE_VALUE" not in path.read_text(encoding="utf-8")
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [row["phase"] for row in rows] == ["started", "approval", "completed"]
    assert rows[1]["approval"] == "accept"
    assert rows[0]["arguments_sha256"] and rows[2]["output_sha256"]
    assert receipts.verify(path)[0] == 3


def test_receipts_detect_tampering(tmp_path, monkeypatch):
    monkeypatch.setenv("PANINI_DATA_DIR", str(tmp_path))
    log = receipts.ReceiptLog("audit-one", tmp_path)
    log.append(call_id="one", tool="read_file", phase="started", arguments={"path": "a.txt"})
    log.append(call_id="one", tool="read_file", phase="completed", output="hello")
    assert receipts.verify(log.path)[0] == 2
    lines = log.path.read_text(encoding="utf-8").splitlines()
    lines[0] = lines[0].replace("read_file", "write_file")
    log.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="line 1"):
        receipts.verify(log.path)


def test_receipt_session_id_rejects_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("PANINI_DATA_DIR", str(tmp_path))
    with pytest.raises(ValueError):
        receipts.path_for("../another-session")
