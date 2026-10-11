"""Installed PANINI agent for Harbor 0.24.0. Isolation belongs to the task container.

Use -a evals.harbor_adapter:PaniniAgent --ak wheel_path=/absolute/panini.whl.
Requires Python >=3.12 in a Linux task environment; does not evaluate Windows MXC.
"""

from __future__ import annotations

import hashlib
import json
import math
import shlex
import tempfile
from pathlib import Path

from harbor.agents.capabilities import AgentCapabilities
from harbor.agents.installed.base import BaseInstalledAgent, with_prompt_template
from harbor.models.agent.context import AgentContext, ModelUsage
from harbor.models.trajectories import Agent, FinalMetrics, Observation, ObservationResult, Step, ToolCall, Trajectory


def read_jsonl(path):
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue  # A killed process may leave the last event incomplete.
        if isinstance(row, dict):
            rows.append(row)
    return rows


def recovery_metrics(events):
    """Command failures followed by a successful command; task recovery is scored separately."""
    failures, recovered, pending, retries = 0, 0, 0, 0
    failed_by_command = {}
    for event in events:
        item = event.get("item", {})
        if event.get("type") != "item.completed" or item.get("type") != "commandExecution":
            continue
        if item.get("status") == "failed":
            failures += 1
            pending += 1
            command = item.get("command", "")
            if command:
                failed_by_command[command] = failed_by_command.get(command, 0) + 1
        elif item.get("exitCode") == 0 and pending:
            recovered += pending
            pending = 0
        if item.get("exitCode") == 0:
            retries += failed_by_command.pop(item.get("command", ""), 0)
    return {"failed_commands": failures, "followed_by_successful_command": recovered,
            "unrecovered_commands": pending, "failures_with_successful_same_command_retry": retries}


class PaniniAgent(BaseInstalledAgent):
    capabilities = AgentCapabilities(atif=True)

    def __init__(self, logs_dir, *, wheel_path, workspace=None, python_executable="python3",
                 budget_usd=0.20, max_steps=16, **kwargs):
        self.wheel = Path(wheel_path).resolve()
        if not self.wheel.is_file() or self.wheel.suffix != ".whl":
            raise ValueError("wheel_path must identify a built PANINI wheel")
        self.wheel_sha256 = hashlib.sha256(self.wheel.read_bytes()).hexdigest()
        self.workspace = workspace
        self.python_executable = python_executable
        self.budget = float(budget_usd)
        self.max_steps = int(max_steps)
        if not math.isfinite(self.budget) or self.budget <= 0 or not 1 <= self.max_steps <= 60:
            raise ValueError("Positive finite budget and 1..60 max_steps required")
        super().__init__(logs_dir, **kwargs)
        if not self.model_name or self.model_name == "auto":
            raise ValueError("Choose a pinned model for reproducible benchmark trials")
        self.model_id = self.model_name.removeprefix("openai/")
        if self.mcp_servers or self.skills_dir:
            raise ValueError("This adapter does not yet support Harbor-provided MCP servers or skills")

    @staticmethod
    def name():
        return "panini-cli"

    def get_version_command(self):
        return "/installed-agent/panini-venv/bin/panini-cli --version"

    async def install(self, environment):
        if str(environment.os) not in {"linux", "TaskOS.LINUX"}:
            raise ValueError("PANINI Harbor adapter currently requires Linux; MXC is tested separately on Windows")
        remote_wheel = "/installed-agent/" + self.wheel.name
        await environment.upload_file(self.wheel, remote_wheel)
        lock = Path(__file__).resolve().parents[1] / "requirements-runtime.lock"
        await environment.upload_file(lock, "/installed-agent/requirements-runtime.lock")
        python = shlex.quote(self.python_executable)
        checksum = f"import hashlib; assert hashlib.sha256(open({remote_wheel!r},'rb').read()).hexdigest()=={self.wheel_sha256!r}"
        await self.exec_as_root(environment, command=f"{python} -c {shlex.quote(checksum)} && "
            f"{python} -m venv --system-site-packages /installed-agent/panini-venv && "
            "/installed-agent/panini-venv/bin/python -m pip install --disable-pip-version-check "
            f"-r /installed-agent/requirements-runtime.lock {shlex.quote(remote_wheel)}", timeout_sec=300)

    @with_prompt_template
    async def run(self, instruction, environment, context):
        logs = str(self.environment_logs_dir)
        prompt = logs + "/instruction.txt"
        with tempfile.TemporaryDirectory(prefix="panini-harbor-") as folder:
            path = Path(folder) / "instruction.txt"
            path.write_text(instruction, encoding="utf-8")
            await environment.upload_file(path, prompt)
        q = shlex.quote
        command = "/installed-agent/panini-venv/bin/panini-cli exec --json --sandbox none -a auto "
        if self.workspace:
            command += f"-C {q(self.workspace)} "
        command += f"-m {q(self.model_id)} - < {q(prompt)} > {q(logs + '/events.jsonl')} 2> {q(logs + '/stderr.log')}; "
        command += f"code=$?; printf '%s\\n' \"$code\" > {q(logs + '/exit-code.txt')}"
        # Nonzero CLI exits are retained and verified, never converted into missing trials.
        await self.exec_as_agent(environment, command=command, env={
            "PANINI_DATA_DIR": logs + "/panini", "PANINI_CONFIG": logs + "/empty-config.toml",
            "PANINI_CLI_BUDGET_USD": str(self.budget), "PANINI_CLI_MAX_STEPS": str(self.max_steps),
            "PANINI_CLI_CONTEXT_TOKENS": "20000", "PANINI_CLI_LOCAL": "off",
            "PANINI_CLI_CHECKPOINTS": "off", "PANINI_CLI_EXPLORE": "off",
        })
        # Harbor only calls populate_context_post_run for an empty context; leave it
        # empty until synced logs can supply complete usage, even after interruption.

    def populate_context_post_run(self, context: AgentContext):
        events = read_jsonl(self.logs_dir / "events.jsonl")
        calls = [r for r in read_jsonl(self.logs_dir / "panini" / "usage.jsonl") if r.get("kind") == "call"]
        final = next((r for r in reversed(events) if r.get("type") == "turn.completed"), {})
        usage = final.get("usage") or {}
        if calls:
            context.n_input_tokens = sum(r.get("input_tokens", 0) for r in calls)
            context.n_output_tokens = sum(r.get("output_tokens", 0) for r in calls)
            context.cost_usd = sum(r["cost_usd"] for r in calls) if all(r.get("cost_usd") is not None for r in calls) else None
            context.model_usage = {}
            for model in sorted({r["model"] for r in calls}):
                mine = [r for r in calls if r["model"] == model]
                context.model_usage[model] = ModelUsage(n_input_tokens=sum(r.get("input_tokens", 0) for r in mine),
                    n_output_tokens=sum(r.get("output_tokens", 0) for r in mine),
                    cost_usd=sum(r["cost_usd"] for r in mine) if all(r.get("cost_usd") is not None for r in mine) else None)
        else:
            context.n_input_tokens = usage.get("input_tokens")
            context.n_output_tokens = usage.get("output_tokens")
            context.cost_usd = final.get("cost_usd")
        context.n_cache_tokens = usage.get("cached_tokens")  # Unknown remains unknown.
        context.metadata = {**(context.metadata or {}), "turn_status": final.get("status", "missing-final-event"),
            "recovery": recovery_metrics(events), "wheel_sha256": self.wheel_sha256,
            "containment": "Harbor task environment", "windows_mxc_tested": False}
        trajectory = self.convert_trajectory(self.logs_dir)
        if trajectory:
            trajectory.final_metrics = FinalMetrics(total_prompt_tokens=context.n_input_tokens,
                total_completion_tokens=context.n_output_tokens, total_cached_tokens=context.n_cache_tokens,
                total_cost_usd=context.cost_usd, total_steps=len(trajectory.steps))
            (self.logs_dir / "trajectory.json").write_text(trajectory.model_dump_json(indent=2, exclude_none=True), encoding="utf-8")

    def convert_trajectory(self, logs_dir):
        records = list((logs_dir / "panini" / "cli-sessions").glob("*.json"))
        if not records:
            return None
        record = json.loads(max(records, key=lambda p: p.stat().st_mtime_ns).read_text(encoding="utf-8"))
        steps, owners = [], {}
        for item in record.get("items", []):
            if item.get("type") == "function_call":
                try:
                    arguments = json.loads(item["arguments"])
                except (ValueError, KeyError):
                    arguments = {"raw": item.get("arguments", "")}
                if not isinstance(arguments, dict):
                    arguments = {"raw": arguments}
                step = Step(step_id=len(steps) + 1, source="agent", message="", tool_calls=[ToolCall(
                    tool_call_id=item["call_id"], function_name=item["name"], arguments=arguments)])
                owners[item["call_id"]] = step
                steps.append(step)
            elif item.get("type") == "function_call_output":
                owner = owners.get(item.get("call_id"))
                if owner:
                    owner.observation = Observation(results=[ObservationResult(source_call_id=item["call_id"],
                        content=str(item.get("output", "")))])
            elif item.get("role") in {"user", "developer", "assistant"}:
                content = item.get("content", "")
                if isinstance(content, list):
                    content = "\n".join(c.get("text", "") for c in content if isinstance(c, dict))
                steps.append(Step(step_id=len(steps) + 1, source={"user": "user", "developer": "system",
                    "assistant": "agent"}[item["role"]], message=str(content)))
        if not steps:
            return None
        return Trajectory(session_id=record.get("id"), agent=Agent(name=self.name(), version=self.version() or "unknown",
            model_name=self.model_id), steps=steps,
            notes="Exported native dialogue and tool actions. Encrypted reasoning is omitted; per-inference usage is unavailable.",
            extra={"wheel_sha256": self.wheel_sha256})
