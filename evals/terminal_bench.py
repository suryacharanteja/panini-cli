"""Prepare a maintainer evaluation and run explicitly budgeted Terminal-Bench smoke tests.

Preparation never launches trials. A smoke run is a subset, never a leaderboard score.
The original custom-pilot adapter stays unchanged to preserve its recorded hashes.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import shlex
import shutil
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from harbor.models.job.config import JobConfig
from harbor.models.task.config import TaskConfig
from harbor.publisher.packager import Packager
from harbor.registry.client.package import PackageDatasetClient

from evals.harbor_adapter import PaniniAgent, read_jsonl
from evals.pilot import BudgetProxy, ROOT, trial_row

DATASET = "terminal-bench/terminal-bench"
DATASET_REF = "sha256:39d9f44b40420cde8fdcc087579c0d72a7e14fa3656d603c3f0d22fb35e27732"
EXPECTED_TASKS = 66
UV_URL = "https://github.com/astral-sh/uv/releases/download/0.9.5/uv-x86_64-unknown-linux-gnu.tar.gz"
UV_SHA256 = "2cf10babba653310606f8b49876cfb679928669e7ddaa1fb41fb00ce73e64f66"
PYTHON_VERSION = "3.12.12"


def digest(path):
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


class TerminalBenchPaniniAgent(PaniniAgent):
    """Install a pinned private Python runtime without changing the task's interpreter."""

    def __init__(self, logs_dir, *, uv_archive_path, **kwargs):
        self.uv_archive = Path(uv_archive_path).resolve()
        if not self.uv_archive.is_file() or digest(self.uv_archive) != UV_SHA256:
            raise ValueError("uv_archive_path must contain the pinned, verified uv 0.9.5 Linux x64 archive")
        super().__init__(logs_dir, **kwargs)

    async def install(self, environment):
        if str(environment.os) not in {"linux", "TaskOS.LINUX"}:
            raise ValueError("Terminal-Bench adapter requires Linux x64; Windows MXC is evaluated separately")
        await environment.upload_file(self.uv_archive, "/installed-agent/uv.tar.gz")
        remote_wheel = "/installed-agent/" + self.wheel.name
        await environment.upload_file(self.wheel, remote_wheel)
        await environment.upload_file(ROOT / "requirements-runtime.lock", "/installed-agent/requirements-runtime.lock")
        # No task directory, system interpreter, timeout or resource setting is replaced.
        command = (
            "test \"$(uname -m)\" = x86_64 && "
            f"printf '%s\\n' '{UV_SHA256}  /installed-agent/uv.tar.gz' | sha256sum -c - && "
            f"printf '%s\\n' {shlex.quote(self.wheel_sha256 + '  ' + remote_wheel)} | sha256sum -c - && "
            "mkdir -p /installed-agent/uv && tar -xzf /installed-agent/uv.tar.gz "
            "-C /installed-agent/uv --strip-components=1 && "
            f"/installed-agent/uv/uv venv --python {PYTHON_VERSION} /installed-agent/panini-venv && "
            "/installed-agent/uv/uv pip install --python /installed-agent/panini-venv/bin/python --no-deps "
            f"-r /installed-agent/requirements-runtime.lock {shlex.quote(remote_wheel)} && "
            "chmod -R a+rX /installed-agent/panini-venv /installed-agent/python"
        )
        await self.exec_as_root(environment, command=command, env={
            "UV_PYTHON_INSTALL_DIR": "/installed-agent/python", "UV_CACHE_DIR": "/installed-agent/uv-cache",
            "UV_PYTHON_PREFERENCE": "only-managed", "UV_LINK_MODE": "copy",
        }, timeout_sec=300)

    def populate_context_post_run(self, context):
        super().populate_context_post_run(context)
        final = next((e for e in reversed(read_jsonl(self.logs_dir / "events.jsonl")) if e.get("type") == "turn.completed"), {})
        if final.get("status") == "failed" and final.get("error"):
            # An interrupted/incomplete API response may consume tokens without a
            # recorded usage event. Known earlier calls are not the trial's total.
            context.metadata.update(usage_scope="incomplete; earlier reported calls only",
                                    reported_cost_lower_bound_usd=context.cost_usd,
                                    reported_input_tokens=context.n_input_tokens, reported_output_tokens=context.n_output_tokens)
            context.n_input_tokens = context.n_output_tokens = context.n_cache_tokens = context.cost_usd = None
            context.model_usage = None
            path = self.logs_dir / "trajectory.json"
            if path.exists():
                trajectory = json.loads(path.read_text(encoding="utf-8"))
                trajectory["final_metrics"] = {"total_steps": len(trajectory["steps"])}
                trajectory.setdefault("extra", {}).update(context.metadata)
                path.write_text(json.dumps(trajectory, indent=2) + "\n", encoding="utf-8")


def evaluation_config(wheel, uv_archive, model="openai/gpt-5.6-luna"):
    if not model.startswith("openai/") or model == "openai/auto":
        raise ValueError("Use a pinned openai/model identifier")
    return {
        "job_name": "panini-terminal-bench-4-maintainer", "jobs_dir": "jobs", "n_attempts": 5,
        "n_concurrent_trials": 1, "retry": {"max_retries": 0}, "environment": {"type": "modal"},
        "datasets": [{"name": DATASET, "ref": DATASET_REF}],
        "agents": [{"import_path": "evals.terminal_bench:TerminalBenchPaniniAgent", "model_name": model,
                    "kwargs": {"wheel_path": str(wheel), "uv_archive_path": str(uv_archive),
                               "budget_usd": 1.0, "max_steps": 60}}],
    }


async def preflight(task_dir):
    metadata = await PackageDatasetClient().get_dataset_metadata(f"{DATASET}@{DATASET_REF}")
    if metadata.version != DATASET_REF or len(metadata.task_ids) != EXPECTED_TASKS:
        raise ValueError("Canonical dataset identity or task count changed; recheck upstream submission rules")
    tasks = []
    for task in metadata.task_ids:
        path = task_dir / task.name
        actual, _ = Packager.compute_content_hash(path)
        if "sha256:" + actual != task.ref:
            raise ValueError(f"Task content differs from the official version: {task.get_name()}")
        config = TaskConfig.model_validate_toml((path / "task.toml").read_text(encoding="utf-8"))
        tasks.append({"name": task.get_name(), "sha256": actual, "gpus": config.environment.gpus,
                      "agent_timeout_sec": config.agent.timeout_sec,
                      "steps": len(config.steps or [])})
    return {"dataset": DATASET, "dataset_ref": DATASET_REF, "tasks": tasks, "task_count": len(tasks),
            "required_trials": EXPECTED_TASKS * 5, "canonical_hashes_verified": True,
            "checked_at": datetime.now(timezone.utc).isoformat()}


def prepare_artifacts(wheel, folder, uv_source=None):
    folder.mkdir(parents=True, exist_ok=False)
    frozen = folder / wheel.name
    shutil.copyfile(wheel, frozen)
    uv_archive = folder / "uv-x86_64-unknown-linux-gnu.tar.gz"
    if uv_source:
        if digest(uv_source) != UV_SHA256:
            raise ValueError("Cached uv archive does not match its pinned SHA256")
        shutil.copyfile(uv_source, uv_archive)
    else:
        with urllib.request.urlopen(UV_URL, timeout=120) as response, uv_archive.open("wb") as target:
            shutil.copyfileobj(response, target)
    if digest(uv_archive) != UV_SHA256:
        raise ValueError("Downloaded uv archive does not match its pinned SHA256")
    return frozen, uv_archive


async def smoke(config, folder, task, budget):
    from harbor.job import Job
    from panini_cli.agent import ledger
    from panini_cli.agent.cli import load_panini_env

    load_panini_env()
    model = config["agents"][0]["model_name"].removeprefix("openai/")
    prices = ledger.price(model)
    key = os.environ.get("OPENAI_API_KEY")
    if not key or not prices:
        raise ValueError("A configured OpenAI credential and a priced pinned model are required")
    proxy = BudgetProxy(model, key, budget, prices, os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
                        folder / "budget-journal.json", max_output_tokens=16384).start()
    # Use the canonical package dataset so Hub records its official source and digest.
    config = json.loads(json.dumps(config))
    config.update(job_name="panini-terminal-bench-4-smoke", jobs_dir=str((folder / "jobs").resolve()), n_attempts=1)
    config["environment"] = {"type": "docker"}
    config["datasets"][0]["task_names"] = [f"terminal-bench/{task}"]
    config["agents"][0]["env"] = {"OPENAI_API_KEY": proxy.token, "OPENAI_BASE_URL": proxy.container_url}
    error, started = None, time.monotonic()
    try:
        job = await Job.create(JobConfig.model_validate(config))
        await job.run()
    except Exception as exc:
        error = type(exc).__name__
    finally:
        proxy.close()
    trials = list((folder / "jobs" / config["job_name"]).glob("*/result.json"))
    result = json.loads(trials[0].read_text(encoding="utf-8")) if trials else {
        "exception_info": {"exception_type": error or "MissingTrialResult"}}
    return {"scope": "One preselected official task, one attempt; not an official leaderboard score",
            "execution": "Local Harbor Docker; public Hub upload is separate", "dataset_ref": DATASET_REF,
            "trial": trial_row(result, task, 1, time.monotonic() - started),
            "reserved_cost_upper_bound_usd": proxy.reserved, "reservation_limit_usd": budget,
            "forwarded_api_calls": proxy.calls, "denied_api_calls": proxy.denied,
            "model": model, "price_per_million_tokens": list(prices), "price_scope": "Estimates, not provider invoices",
            "max_output_tokens_per_call": proxy.max_output_tokens, "adapter_sha256": digest(Path(__file__)),
            "proxy_source_sha256": digest(ROOT / "evals/pilot.py"),
            "wheel_sha256": digest(config["agents"][0]["kwargs"]["wheel_path"]), "max_steps": 60,
            "default_task_timeouts_and_resources": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--uv-archive", type=Path, help="Reuse a previously downloaded archive after checksum verification")
    parser.add_argument("--task-dir", type=Path, default=ROOT / "build/harbor/official-dataset/terminal-bench")
    parser.add_argument("--model", default="openai/gpt-5.6-luna")
    parser.add_argument("--smoke-task", help="Explicitly run one canonical task; omitted means preparation only")
    parser.add_argument("--budget-usd", type=float, help="Required positive reservation cap for a smoke run")
    args = parser.parse_args()
    if not args.wheel.is_file() or args.wheel.suffix != ".whl":
        parser.error("--wheel must be a built PANINI wheel")
    if args.smoke_task and (args.budget_usd is None or not math.isfinite(args.budget_usd) or args.budget_usd <= 0):
        parser.error("--smoke-task requires a finite positive --budget-usd; account for earlier pilot spending")
    config = evaluation_config(args.wheel, args.output / "uv-x86_64-unknown-linux-gnu.tar.gz", args.model)
    readiness = asyncio.run(preflight(args.task_dir))
    if args.smoke_task and f"terminal-bench/{args.smoke_task}" not in {t["name"] for t in readiness["tasks"]}:
        parser.error("--smoke-task must identify an official task")
    wheel, uv_archive = prepare_artifacts(args.wheel, args.output, args.uv_archive)
    config["agents"][0]["kwargs"].update(wheel_path=str(wheel.resolve()), uv_archive_path=str(uv_archive.resolve()))
    JobConfig.model_validate(config)
    readiness.update(wheel_sha256=digest(wheel), uv_sha256=UV_SHA256, python=PYTHON_VERSION,
                     adapter_sha256=digest(Path(__file__)), official_run_completed=False,
                     proxy_source_sha256=digest(ROOT / "evals/pilot.py"),
                     maintainer_acceptance_required=True, hosted_acp_supported=False)
    for name, content in (("maintainer-config.json", config), ("preflight.json", readiness)):
        (args.output / name).write_text(json.dumps(content, indent=2) + "\n", encoding="utf-8")
    print(f"Verified {EXPECTED_TASKS} canonical task hashes; prepared {EXPECTED_TASKS * 5} trials. No full run launched.", flush=True)
    if args.smoke_task:
        result = asyncio.run(smoke(config, args.output, args.smoke_task, args.budget_usd))
        (args.output / "smoke.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
