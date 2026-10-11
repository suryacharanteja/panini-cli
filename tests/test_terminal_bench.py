"""Official evaluation preparation must preserve canonical inputs and fail closed."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("harbor")
from evals import terminal_bench as tb  # noqa: E402


def test_maintainer_config_covers_all_tasks_with_default_limits():
    config = tb.evaluation_config("panini.whl", "uv.tar.gz")
    job = tb.JobConfig.model_validate(config)
    assert job.n_attempts == 5 and job.retry.max_retries == 0
    assert job.datasets[0].name == tb.DATASET and job.datasets[0].ref == tb.DATASET_REF
    assert job.datasets[0].task_names is None and job.datasets[0].exclude_task_names is None
    assert job.timeout_multiplier == 1 and job.agent_timeout_multiplier is None
    assert job.verifier_timeout_multiplier is None and job.environment_build_timeout_multiplier is None
    assert job.agents[0].override_timeout_sec is None and job.agents[0].override_setup_timeout_sec is None
    assert job.verifier.override_timeout_sec is None
    assert all(getattr(job.environment, field) is None for field in (
        "override_cpus", "override_memory_mb", "override_storage_mb", "override_gpus", "override_tpu"))
    assert not job.agents[0].env  # The public config never contains credentials.


def test_unverified_runtime_archive_is_rejected_before_install(tmp_path):
    archive = tmp_path / "uv.tar.gz"
    archive.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="verified"):
        tb.TerminalBenchPaniniAgent(tmp_path, uv_archive_path=archive, wheel_path=tmp_path / "panini.whl", model_name="pinned")


def test_failed_inference_does_not_publish_partial_cost_as_total(tmp_path, monkeypatch):
    from harbor.models.agent.context import AgentContext

    archive, wheel = tmp_path / "uv.tar.gz", tmp_path / "panini.whl"
    archive.write_bytes(b"runtime")
    wheel.write_bytes(b"wheel")
    monkeypatch.setattr(tb, "UV_SHA256", tb.digest(archive))
    agent = tb.TerminalBenchPaniniAgent(tmp_path, uv_archive_path=archive, wheel_path=wheel, model_name="pinned")
    (tmp_path / "panini").mkdir()
    (tmp_path / "events.jsonl").write_text(json.dumps({"type": "turn.completed", "status": "failed", "error": "cut off"}))
    (tmp_path / "panini/usage.jsonl").write_text(json.dumps({"kind": "call", "model": "pinned",
        "input_tokens": 3, "output_tokens": 2, "cost_usd": .01}))
    context = AgentContext()
    agent.populate_context_post_run(context)
    assert context.cost_usd is None and context.model_usage is None and context.n_input_tokens is None
    assert context.metadata["reported_cost_lower_bound_usd"] == .01


def test_larger_output_cap_reserves_its_entire_cost():
    proxy = tb.BudgetProxy("pinned", "secret", 1, (0, 1), max_output_tokens=16384)
    body = json.loads(proxy.reserve({"model": "pinned", "max_output_tokens": 100000}))
    assert body["max_output_tokens"] == 16384 and proxy.reserved == pytest.approx(.016384)


@pytest.mark.parametrize("cap", [0, -1, 16385, True, 1.5])
def test_invalid_output_cap_rejected(cap):
    with pytest.raises(ValueError, match="cap"):
        tb.BudgetProxy("pinned", "secret", 1, (.2, 1.2), max_output_tokens=cap)


def test_preflight_rejects_modified_task_content(tmp_path, monkeypatch):
    task = SimpleNamespace(name="task", ref="sha256:canonical", get_name=lambda: "terminal-bench/task")
    metadata = SimpleNamespace(version=tb.DATASET_REF, task_ids=[task])

    async def fetch(_self, _name):
        return metadata

    monkeypatch.setattr(tb, "EXPECTED_TASKS", 1)
    monkeypatch.setattr(tb.PackageDatasetClient, "get_dataset_metadata", fetch)
    monkeypatch.setattr(tb.Packager, "compute_content_hash", lambda _path: ("tampered", []))
    with pytest.raises(ValueError, match="differs"):
        asyncio.run(tb.preflight(tmp_path))


def test_install_preserves_task_runtime_and_quotes_artifact_names(tmp_path, monkeypatch):
    archive = tmp_path / "uv.tar.gz"
    archive.write_bytes(b"verified-test-fixture")
    wheel = tmp_path / "panini_cli-2.1.1b1-py3-none-any.whl"
    wheel.write_bytes(b"wheel-test-fixture")
    monkeypatch.setattr(tb, "UV_SHA256", tb.digest(archive))
    agent = tb.TerminalBenchPaniniAgent(tmp_path, uv_archive_path=archive, wheel_path=wheel, model_name="pinned")
    calls = []

    class Environment:
        os = "linux"

        async def upload_file(self, path, target):
            assert Path(path).is_file()
            calls.append(("upload", target))

        async def exec(self, **kwargs):
            calls.append(("exec", kwargs))
            return SimpleNamespace(return_code=0, stdout="", stderr="")

    asyncio.run(agent.install(Environment()))
    command = calls[-1][1]["command"]
    assert "--python 3.12.12" in command and "sha256sum -c" in command
    assert "--no-deps" in command and "--system-site-packages" not in command
    assert all(call[1].startswith("/installed-agent/") for call in calls if call[0] == "upload")
    assert calls[-1][1]["env"]["UV_PYTHON_PREFERENCE"] == "only-managed"


@pytest.mark.parametrize("model", ["auto", "openai/auto", "unqualified-model"])
def test_evaluation_requires_pinned_provider_model(model):
    with pytest.raises(ValueError, match="pinned"):
        tb.evaluation_config("panini.whl", "uv.tar.gz", model)
