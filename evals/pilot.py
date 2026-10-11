"""Budgeted, sequential Harbor engineering pilot. All attempts remain in the report.

python -m evals.pilot --wheel build/beta/panini_cli-2.1.1b1-py3-none-any.whl --budget-usd 5 --repeats 3
This custom task suite is NOT Terminal-Bench and does not test Windows MXC.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import html
import json
import math
import os
import secrets
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
TASK_IDS = ("fix-add", "fizzbuzz", "pagination-off-by-one", "parse-config", "rename-across-files", "verify-after-diagnostic")


class BudgetProxy:
    """Only one pinned Responses model; reserves bounded request cost before forwarding.

    Real credentials stay in this host process. A trial receives an expiring proxy token.
    Failed/partial calls keep their reservation, so unknown spend is never counted as zero.
    Bounds use UTF-8 payload bytes + 8192 input tokens and 2048 output tokens. Costs use
    the configured price table, not provider invoices; provider billing remains authoritative.
    """

    def __init__(self, model, key, budget, prices, upstream="https://api.openai.com/v1", journal=None, *, max_output_tokens=2048):
        if not math.isfinite(budget) or budget <= 0 or len(prices) != 2 or any(not math.isfinite(p) or p < 0 for p in prices):
            raise ValueError("Finite positive budget and finite nonnegative prices required")
        if not upstream.startswith("https://"):
            raise ValueError("The upstream model endpoint must use HTTPS")
        if isinstance(max_output_tokens, bool) or not isinstance(max_output_tokens, int) or not 1 <= max_output_tokens <= 16384:
            raise ValueError("Output token cap must be an integer from 1 to 16384")
        self.model, self.key, self.limit, self.prices, self.upstream = model, key, budget, prices, upstream.rstrip("/")
        self.max_output_tokens = max_output_tokens
        self.token = "panini-pilot-" + secrets.token_hex(24)
        self.lock = threading.Lock()
        self.reserved = 0.0
        self.calls = 0
        self.denied = 0
        self.server = None
        self.thread = None
        self.journal = journal

    def reserve(self, payload):
        payload["max_output_tokens"] = self.max_output_tokens
        payload["store"] = False
        if payload.get("model") != self.model:
            raise ValueError("The pilot only permits its pinned model")
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        estimate = ((len(body) + 8192) * self.prices[0] + self.max_output_tokens * self.prices[1]) / 1e6
        with self.lock:
            if self.reserved + estimate > self.limit:
                self.denied += 1
                raise ValueError("Pilot reservation budget exhausted")
            self.reserved += estimate
            self.calls += 1
            if self.journal:
                temporary = self.journal.with_suffix(".tmp")
                temporary.write_text(json.dumps({"reserved_cost_upper_bound_usd": self.reserved,
                    "forwarded_api_calls": self.calls, "limit_usd": self.limit}), encoding="utf-8")
                os.replace(temporary, self.journal)
        return body

    def start(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass  # Never log a credential, task body or authorization header.

            def reject(self, status, message):
                body = json.dumps({"error": {"message": message, "type": "pilot_error"}}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)
                self.close_connection = True

            def do_POST(self):
                self.connection.settimeout(30)
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 1 <= size <= 512 * 1024:
                        return self.reject(413, "Request size exceeded")
                    raw = self.rfile.read(size)
                except (ValueError, OSError):
                    return self.reject(400, "Invalid request size or body")
                if self.path != "/v1/responses" or not secrets.compare_digest(
                        self.headers.get("Authorization", ""), "Bearer " + owner.token):
                    return self.reject(403, "Pilot endpoint or authorization denied")
                try:
                    payload = json.loads(raw)
                    if not isinstance(payload, dict):
                        raise ValueError("Invalid request object")
                    body = owner.reserve(payload)
                except (ValueError, OSError):
                    return self.reject(429, "Pinned model or reservation budget rejected request")
                headers_sent = False
                try:
                    with httpx.Client(timeout=180, trust_env=False) as client:
                        with client.stream("POST", owner.upstream + "/responses", content=body, headers={
                                "Authorization": "Bearer " + owner.key, "Content-Type": "application/json"}) as response:
                            self.send_response(response.status_code)
                            self.send_header("Content-Type", response.headers.get("Content-Type", "application/json"))
                            self.send_header("Connection", "close")
                            self.end_headers()
                            headers_sent = True
                            for chunk in response.iter_bytes():
                                self.wfile.write(chunk)
                                self.wfile.flush()
                except (httpx.HTTPError, OSError):
                    if not headers_sent:
                        self.reject(502, "Upstream model transport failed")
                finally:
                    self.close_connection = True

        class Server(ThreadingHTTPServer):
            slots = threading.BoundedSemaphore(8)

            def process_request(self, request, client_address):
                if not self.slots.acquire(blocking=False):
                    self.shutdown_request(request)
                    return
                try:
                    super().process_request(request, client_address)
                except BaseException:
                    self.slots.release()
                    raise

            def process_request_thread(self, request, client_address):
                try:
                    super().process_request_thread(request, client_address)
                finally:
                    self.slots.release()

        self.server = Server(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    @property
    def container_url(self):
        return f"http://host.docker.internal:{self.server.server_port}/v1"

    def close(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=5)
        self.key = ""


def docker(*args, timeout=600):
    return subprocess.run(["docker", *args], cwd=ROOT, check=True, capture_output=True, text=True, timeout=timeout).stdout.strip()


def prepare_tasks(folder):
    tasks = {t["id"]: t for t in json.loads((ROOT / "evals/tasks.json").read_text(encoding="utf-8"))}
    digest = hashlib.sha256((ROOT / "requirements-runtime.lock").read_bytes()).hexdigest()[:12]
    base = f"panini-pilot-runtime:{digest}"
    image = folder / "runtime"
    image.mkdir(parents=True)
    (image / "requirements-runtime.lock").write_bytes((ROOT / "requirements-runtime.lock").read_bytes())
    base_image = docker("image", "inspect", "python:3.12-slim", "--format", "{{index .RepoDigests 0}}")
    (image / "Dockerfile").write_text(f"FROM {base_image}\nCOPY requirements-runtime.lock /tmp/requirements-runtime.lock\n"
        "RUN python -m pip install --disable-pip-version-check -r /tmp/requirements-runtime.lock pytest==9.1.1\n"
        "WORKDIR /app\n", encoding="utf-8")
    docker("build", "-q", "-t", base, str(image))
    for task_id in TASK_IDS:
        task = tasks[task_id]
        destination = folder / "tasks" / task_id
        for name, text in task["files"].items():
            path = destination / "environment" / "files" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="\n")
        (destination / "environment/Dockerfile").write_text(f"FROM {base}\nCOPY files/ /app/\nWORKDIR /app\n", encoding="utf-8")
        prompt = task["prompt"]
        if task_id == "verify-after-diagnostic":
            prompt += "\nFirst run python -m pytest -q to reproduce the failure before changing code, then rerun it after the fix."
        (destination / "instruction.md").write_text(prompt, encoding="utf-8")
        (destination / "task.toml").write_text('version = "1.0"\n[agent]\ntimeout_sec = 240\n'
            '[verifier]\ntimeout_sec = 60\n[environment]\ncpus = 2\nmemory_mb = 2048\n', encoding="utf-8")
        checks = destination / "tests"
        checks.mkdir()
        # The independent verifier lives outside /app; original tests cannot be weakened for a pass.
        originals = {name: hashlib.sha256(text.encode()).hexdigest() for name, text in task["files"].items()
                     if name.startswith("test_")}
        guard = "import hashlib,pathlib\n" + "\n".join(
            f"assert hashlib.sha256(pathlib.Path({name!r}).read_bytes()).hexdigest()=={sha!r}" for name, sha in originals.items())
        (checks / "unchanged-tests.py").write_text(guard, encoding="utf-8", newline="\n")
        (checks / "test.sh").write_text("#!/bin/bash\ncd /app\nmkdir -p /logs/verifier\n"
            f"if python /tests/unchanged-tests.py && {task['check']}; then\n"
            "  echo 1 > /logs/verifier/reward.txt\nelse\n  echo 0 > /logs/verifier/reward.txt\nfi\n", encoding="utf-8", newline="\n")
    return {"base_image": base_image, "runtime_image_id": docker("image", "inspect", base, "--format", "{{.Id}}"),
            "requirements_sha256": hashlib.sha256((ROOT / "requirements-runtime.lock").read_bytes()).hexdigest(),
            "tasks_sha256": hashlib.sha256((ROOT / "evals/tasks.json").read_bytes()).hexdigest(),
            "fixture_revision": 2, "pilot_driver_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}


def trial_row(result, task_id, repetition, duration):
    context = result.get("agent_result") or {}
    metadata = context.get("metadata") or {}
    reward = ((result.get("verifier_result") or {}).get("rewards") or {}).get("reward", 0)
    timing = result.get("agent_execution") or {}
    agent_seconds = None
    if timing.get("started_at") and timing.get("finished_at"):
        agent_seconds = (datetime.fromisoformat(timing["finished_at"]) - datetime.fromisoformat(timing["started_at"])).total_seconds()
    recovery = metadata.get("recovery") or {}
    solved = reward == 1 and not result.get("exception_info")
    return {"task": task_id, "repetition": repetition, "solved": solved, "reward": reward,
        "cost_usd_estimated": context.get("cost_usd"), "input_tokens": context.get("n_input_tokens"),
        "output_tokens": context.get("n_output_tokens"), "agent_seconds": agent_seconds,
        "total_seconds": round(duration, 3), "turn_status": metadata.get("turn_status", "missing-final-event"),
        "exception_type": (result.get("exception_info") or {}).get("exception_type"), "recovery": recovery,
        "wheel_sha256": metadata.get("wheel_sha256"),
        "task_checksum": result.get("task_checksum"),
        "task_recovered_after_command_failure": solved and recovery.get("failures_with_successful_same_command_retry", 0) > 0}


def publish_report(folder, manifest, rows):
    priced = [r["cost_usd_estimated"] for r in rows if r.get("cost_usd_estimated") is not None]
    times = [r["agent_seconds"] for r in rows if r.get("agent_seconds") is not None]
    solved_count = sum(r["solved"] for r in rows)
    summary = {"attempted": len(rows), "solved": sum(r["solved"] for r in rows), "failed": sum(not r["solved"] for r in rows),
        "estimated_cost_usd": sum(priced), "unpriced_trials": len(rows) - len(priced),
        "mean_agent_seconds": sum(times) / len(times) if times else None,
        "trials_missing_agent_time": len(rows) - len(times),
        "estimated_cost_per_solved_task_usd": sum(priced) / solved_count if solved_count and len(priced) == len(rows) else None,
        "mean_total_seconds": sum(r["total_seconds"] for r in rows) / max(1, len(rows)),
        "trials_with_failed_commands": sum(r.get("recovery", {}).get("failed_commands", 0) > 0 for r in rows),
        "cli_completed": sum(r.get("turn_status") == "completed" for r in rows),
        "tasks_recovered_after_command_failure": sum(r.get("task_recovered_after_command_failure", False) for r in rows)}
    report = {"manifest": manifest, "summary": summary, "trials": rows}
    (folder / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    table = "".join("<tr>" + "".join(f"<td>{html.escape(str(value))}</td>" for value in (
        r["task"], r["repetition"], "PASS" if r["solved"] else "FAIL", r.get("turn_status"),
        r.get("cost_usd_estimated"), r.get("agent_seconds"), r.get("recovery", {}).get("failed_commands", 0),
        r.get("task_recovered_after_command_failure", False), r.get("exception_type") or "")) + "</tr>" for r in rows)
    (folder / "report.html").write_text('<!doctype html><html lang="en"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width"><title>PANINI Harbor pilot</title>'
        '<style>body{font:16px system-ui;margin:3em;color:#182330}table{border-collapse:collapse}td,th{padding:.5em;border:1px solid #ccd}pre{white-space:pre-wrap}</style>'
        '<h1>PANINI Harbor engineering pilot</h1><p>Custom small tasks. Not an official leaderboard score or a Windows MXC security assessment. All attempted trials, including failures, are shown.</p>'
        f'<pre>{html.escape(json.dumps(summary, indent=2))}</pre><table><tr><th>Task</th><th>Repeat</th><th>Result</th><th>CLI status</th>'
        '<th>Estimated USD</th><th>Agent seconds</th><th>Failed commands</th><th>Task recovered</th><th>Exception</th></tr>'
        + table + '</table><h2>Reproduction manifest</h2><pre>' + html.escape(json.dumps(manifest, indent=2)) + '</pre></html>', encoding="utf-8")
    return summary


async def run(args):
    from harbor.job import Job
    from harbor.models.job.config import JobConfig
    from panini_cli.agent import VERSION, ledger
    from panini_cli.agent.cli import load_panini_env

    load_panini_env()
    prices = ledger.price(args.model)
    key = os.environ.get("OPENAI_API_KEY")
    if not key or prices is None:
        raise ValueError("A configured API key and a priced pinned model are required")
    folder = ROOT / "build/harbor" / ("pilot-" + time.strftime("%Y%m%d-%H%M%S"))
    folder.mkdir(parents=True)
    public = ROOT / "evals/pilots" / folder.name
    public.mkdir(parents=True)
    frozen_wheel = folder / args.wheel.name
    shutil.copyfile(args.wheel, frozen_wheel)
    args.wheel = frozen_wheel  # Setup/build changes cannot mutate the evaluated artifact.
    images = prepare_tasks(folder)
    manifest = {"agent_version": VERSION, "harbor_version": "0.24.0", "model": args.model,
        "budget_usd": args.budget_usd, "repeats": args.repeats, "planned_trials": len(TASK_IDS) * args.repeats,
        "max_steps": 16, "max_output_tokens_per_call": 2048, "price_per_million_tokens": list(prices),
        "price_source": "PANINI ledger; estimates, not provider invoices", "containment": "Harbor Linux Docker environments",
        "windows_mxc_tested": False, "dataset": "PANINI custom engineering tasks; not Terminal-Bench", "task_ids": list(TASK_IDS),
        "wheel_sha256": hashlib.sha256(args.wheel.read_bytes()).hexdigest(), "created_at": datetime.now(timezone.utc).isoformat(),
        "adapter_sha256": hashlib.sha256((ROOT / "evals/harbor_adapter.py").read_bytes()).hexdigest(), **images}
    proxy = BudgetProxy(args.model, key, args.budget_usd, prices,
        os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"), folder / "budget-journal.json").start()
    rows = []
    try:
        for repetition in range(1, args.repeats + 1):
            for task_id in TASK_IDS:
                name = f"{task_id}-{repetition}"
                started = time.monotonic()
                config = JobConfig.model_validate({"job_name": name, "jobs_dir": str(folder / "jobs"), "n_concurrent_trials": 1,
                    "retry": {"max_retries": 0}, "tasks": [{"path": str(folder / "tasks" / task_id)}],
                    "agents": [{"import_path": "evals.harbor_adapter:PaniniAgent", "model_name": args.model,
                        "kwargs": {"wheel_path": str(args.wheel.resolve()), "workspace": "/app", "budget_usd": 0.20},
                        "env": {"OPENAI_API_KEY": proxy.token, "OPENAI_BASE_URL": proxy.container_url}}]})
                error = None
                try:
                    job = await Job.create(config)
                    await job.run()
                except Exception as exc:  # Keep infrastructure failures as explicit failed attempts.
                    error = type(exc).__name__
                trials = list((folder / "jobs" / name).glob("*/result.json"))
                result = json.loads(trials[0].read_text(encoding="utf-8")) if trials else {"exception_info": {"exception_type": error or "MissingTrialResult"}}
                row = trial_row(result, task_id, repetition, time.monotonic() - started)
                rows.append(row)
                if trials:
                    trace = trials[0].parent / "agent/trajectory.json"
                    if trace.exists():
                        text = trace.read_text(encoding="utf-8").replace(proxy.token, "[expired-pilot-token]")
                        (public / f"{name}.trajectory.json").write_text(text, encoding="utf-8")
                manifest.update(reserved_cost_upper_bound_usd=proxy.reserved, forwarded_api_calls=proxy.calls, denied_api_calls=proxy.denied)
                publish_report(public, manifest, rows)
                print(f"{name}: {'PASS' if row['solved'] else 'FAIL'}; estimated USD {row['cost_usd_estimated']}; {row['total_seconds']}s", flush=True)
                if proxy.denied:
                    manifest["stopped_reason"] = "reservation budget exhausted"
                    return publish_report(public, manifest, rows), public
    finally:
        proxy.close()
        manifest.update(reserved_cost_upper_bound_usd=proxy.reserved, forwarded_api_calls=proxy.calls, denied_api_calls=proxy.denied)
        publish_report(public, manifest, rows)
    return publish_report(public, manifest, rows), public


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--budget-usd", type=float, default=5)
    parser.add_argument("--model", default="gpt-5.6-luna")
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if not args.wheel.is_file() or not math.isfinite(args.budget_usd) or args.budget_usd <= 0 or not 1 <= args.repeats <= 10:
        parser.error("Built wheel, finite positive budget and 1..10 repeats required")
    summary, public = asyncio.run(run(args))
    print(json.dumps(summary, indent=2))
    print(f"Public report: {public / 'report.html'}")


if __name__ == "__main__":
    main()
