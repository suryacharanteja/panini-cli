"""Phase 2 (docs/AUDIT-2026-10-01.md §4.1): cost ledger and budgets."""

import pytest

from panini_cli.agent import ledger
from panini_cli.agent.config import Settings
from panini_cli.agent.loop import Agent
from panini_cli.agent.model import ModelTurn, ScriptedModel


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_prices_cover_dated_ids_local_models_and_unknowns(tmp_path, monkeypatch):
    assert ledger.price("gpt-4.1-2025-04-14") == ledger.PRICES["gpt-4.1"]
    assert ledger.price("ollama/qwen2.5:3b-instruct") == (0.0, 0.0)
    assert ledger.price("some-new-model") is None
    assert ledger.cost("gpt-5.6-luna", {"input_tokens": 1_000_000, "output_tokens": 1_000_000}) == pytest.approx(1.40)
    config = tmp_path / "config.toml"
    config.write_text('[prices]\n"some-new-model" = [1.0, 2.0]\n', encoding="utf-8")
    monkeypatch.setenv("PANINI_CONFIG", str(config))
    assert ledger.price("some-new-model") == (1.0, 2.0)


class PricedModel(ScriptedModel):
    """A scripted model that reports real-looking usage under a priced model id."""

    def __init__(self, steps, model="gpt-5.6-terra", tokens=100_000):
        super().__init__(steps)
        self.model, self.tokens = model, tokens

    async def respond(self, *args, **kwargs):
        turn = await super().respond(*args, **kwargs)
        return ModelTurn(turn.items, turn.text, turn.calls, {"input_tokens": self.tokens, "output_tokens": 0, "total_tokens": self.tokens})


class Host:
    def __init__(self):
        self.notes = []

    async def started(self, item):
        pass

    async def delta(self, *args):
        pass

    async def completed(self, item):
        pass

    async def approve(self, item):
        return "accept"

    async def hook_note(self, text):
        self.notes.append(text)


@pytest.mark.anyio
async def test_each_call_is_priced_recorded_and_summed(tmp_path):
    model = PricedModel([{"text": "hi"}])
    agent = Agent(tmp_path, model, Settings(sandbox="none"), Host())
    result = await agent.turn("hello")
    assert result.cost_usd == pytest.approx(0.2)  # 100k input tokens at $2/M
    assert agent.cost_usd == pytest.approx(0.2)
    rows = ledger.read()
    assert [row["kind"] for row in rows] == ["call", "turn"]
    assert rows[0]["model"] == "gpt-5.6-terra" and rows[1]["status"] == "completed"


@pytest.mark.anyio
async def test_the_budget_stops_the_session(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    steps = [{"calls": [{"name": "read_file", "arguments": {"path": "a.txt"}}]}] * 5 + [{"text": "done"}]
    model = PricedModel(steps, tokens=100_000)  # $0.20 per step
    agent = Agent(tmp_path, model, Settings(sandbox="none", budget_usd=0.5), Host())
    result = await agent.turn("loop")
    assert result.status == "budget" and "budget" in result.error
    assert agent.cost_usd >= 0.5 and result.steps == 3


@pytest.mark.anyio
async def test_unpriced_models_warn_once_when_a_budget_is_set(tmp_path):
    host = Host()
    agent = Agent(tmp_path, PricedModel([{"text": "a"}, {"text": "b"}], model="mystery"), Settings(sandbox="none", budget_usd=1), host)
    first = await agent.turn("one")
    await agent.turn("two")
    assert first.cost_usd is None and agent.unpriced_calls == 2
    assert len(host.notes) == 1 and "no price" in host.notes[0]


def test_summary_reports_local_savings_and_escalations():
    rows = [
        {"kind": "call", "model": "ollama/qwen2.5:3b-instruct", "input_tokens": 1_000_000, "output_tokens": 0, "cost_usd": 0.0},
        {"kind": "call", "model": "gpt-5.6-terra", "input_tokens": 10, "output_tokens": 0, "cost_usd": 0.5},
        {"kind": "turn", "escalated": True},
        {"kind": "turn", "escalated": False},
    ]
    data = ledger.summary(rows, reference_model="gpt-5.6-terra")
    assert data["cost_usd"] == pytest.approx(0.5)
    assert data["saved_usd"] == pytest.approx(2.0)
    assert data["turns"] == 2 and data["escalations"] == 1
