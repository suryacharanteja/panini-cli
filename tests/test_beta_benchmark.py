"""Beta packaging and benchmark controls; no real model calls."""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from evals.pilot import BudgetProxy, publish_report, trial_row


def test_proxy_concurrent_requests_cannot_over_reserve():
    proxy = BudgetProxy("model", "host-secret", .025, (.2, 1.2))

    def attempt(_):
        try:
            proxy.reserve({"model": "model", "input": "test"})
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=16) as pool:
        accepted = sum(pool.map(attempt, range(100)))
    assert 0 < accepted < 100
    assert proxy.reserved <= proxy.limit
    assert proxy.calls == accepted
    assert proxy.denied == 100 - accepted


def test_proxy_restricts_model_and_bounded_output():
    proxy = BudgetProxy("pinned", "host-secret", 1, (.2, 1.2))
    with pytest.raises(ValueError, match="pinned"):
        proxy.reserve({"model": "unpriced"})
    body = json.loads(proxy.reserve({"model": "pinned", "max_output_tokens": 100000, "store": True}))
    assert body["max_output_tokens"] == 2048 and body["store"] is False
    assert "host-secret" not in json.dumps(body)


def test_proxy_refuses_unauthorized_requests_without_spend():
    proxy = BudgetProxy("pinned", "host-secret", 1, (.2, 1.2)).start()
    try:
        response = httpx.post(f"http://127.0.0.1:{proxy.server.server_port}/v1/responses", json={"model": "pinned"}, trust_env=False)
        assert response.status_code == 403
        assert proxy.calls == 0 and proxy.reserved == 0
    finally:
        proxy.close()
    assert proxy.key == ""


@pytest.mark.parametrize("budget", [0, -1, float("nan"), float("inf")])
def test_proxy_rejects_invalid_budget(budget):
    with pytest.raises(ValueError):
        BudgetProxy("pinned", "secret", budget, (.2, 1.2))


def test_failures_and_unknown_cost_survive_public_report(tmp_path):
    good = trial_row({"agent_result": {"cost_usd": .01, "metadata": {"turn_status": "completed",
        "recovery": {"failed_commands": 2, "failures_with_successful_same_command_retry": 1}}}, "verifier_result": {"rewards": {"reward": 1}}}, "good", 1, 5)
    bad = trial_row({"exception_info": {"exception_type": "TimeoutError"}}, "bad", 1, 10)
    summary = publish_report(tmp_path, {"model": "pinned"}, [good, bad])
    assert summary["solved"] == 1 and summary["failed"] == 1 and summary["unpriced_trials"] == 1
    assert summary["tasks_recovered_after_command_failure"] == 1
    assert json.loads((tmp_path / "report.json").read_text())["trials"][1]["exception_type"] == "TimeoutError"
    assert "FAIL" in (tmp_path / "report.html").read_text()


def test_html_report_escapes_model_or_task_text(tmp_path):
    row = trial_row({}, "<script>alert(1)</script>", 1, 1)
    publish_report(tmp_path, {"model": "<script>"}, [row])
    output = (tmp_path / "report.html").read_text()
    assert "<script>" not in output and "&lt;script&gt;" in output


def test_licenses_does_not_load_credentials(monkeypatch, capsys):
    from panini_cli.agent import cli
    monkeypatch.setattr(cli, "load_panini_env", lambda: pytest.fail("Licenses must not require credentials"))
    assert cli.main(["licenses"]) == 0
    assert "Microsoft MXC" in capsys.readouterr().out


def test_harbor_partial_usage_and_trajectory(tmp_path):
    pytest.importorskip("harbor")
    from evals.harbor_adapter import PaniniAgent, recovery_metrics
    from harbor.models.agent.context import AgentContext

    wheel = tmp_path / "panini.whl"
    wheel.write_bytes(b"fixture")
    agent = PaniniAgent(tmp_path, wheel_path=wheel, model_name="openai/pinned")
    data = tmp_path / "panini"
    (data / "cli-sessions").mkdir(parents=True)
    (data / "usage.jsonl").write_text(json.dumps({"kind": "call", "model": "pinned", "input_tokens": 10,
        "output_tokens": 5, "cost_usd": None}) + "\n", encoding="utf-8")
    (data / "cli-sessions/s.json").write_text(json.dumps({"id": "s", "items": [
        {"role": "user", "content": "Fix it"}, {"type": "function_call", "call_id": "a", "name": "run_command", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "a", "output": "Exit code: 1"},
        {"type": "reasoning", "encrypted_content": "private-ciphertext"}]}), encoding="utf-8")
    context = AgentContext()
    agent.populate_context_post_run(context)
    assert context.n_input_tokens == 10 and context.cost_usd is None
    assert context.metadata["turn_status"] == "missing-final-event"
    assert "private-ciphertext" not in (tmp_path / "trajectory.json").read_text()
    assert recovery_metrics([{"type": "item.completed", "item": {"type": "commandExecution", "status": "failed"}},
        {"type": "item.completed", "item": {"type": "commandExecution", "exitCode": 0}}])["followed_by_successful_command"] == 1


def test_harbor_prompt_is_uploaded_not_shell_interpolated(tmp_path):
    pytest.importorskip("harbor")
    import asyncio
    from evals.harbor_adapter import PaniniAgent
    from harbor.models.agent.context import AgentContext

    wheel = tmp_path / "panini.whl"
    wheel.write_bytes(b"fixture")
    agent = PaniniAgent(tmp_path, wheel_path=wheel, model_name="pinned")
    calls = []

    class Environment:
        async def upload_file(self, path, target):
            calls.append(("upload", Path(path).read_text(), target))

        async def exec(self, **kwargs):
            calls.append(("exec", kwargs))
            return SimpleNamespace(return_code=0, stdout="", stderr="")

    context = AgentContext()
    asyncio.run(agent.run("echo $(touch /danger); `false`", Environment(), context))
    assert calls[0][1] == "echo $(touch /danger); `false`"
    assert "touch /danger" not in calls[1][1]["command"]
    assert context.is_empty()  # Framework must backfill after syncing logs.
