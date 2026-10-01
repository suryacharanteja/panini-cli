"""Paired evaluation: the same tasks, the same oracle, different agents.

    python evals/run.py --arm auto --arm model:gpt-5.6-terra
    python evals/run.py --arm auto --arm codex --task fix-add --task fizzbuzz

Arms:
    auto            PANINI CLI with routing (`-m auto`)
    model:<id>      PANINI CLI pinned to one model (`-m <id>`), e.g. model:local
    codex           Codex CLI (`codex exec --full-auto`); tokens and cost are not captured

Each task runs in a fresh git repository with auto-approval. A "change" task is solved when its
check command exits 0; a "question" task when the final answer matches its regex. Results go to
evals/results/<timestamp>.json and .md. Real models are called, so this costs money.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = Path(os.getenv("PANINI_EVAL_SOURCE") or HERE.parent / "src")


def load_tasks(selected):
    tasks = json.loads((HERE / "tasks.json").read_text(encoding="utf-8"))
    return [task for task in tasks if not selected or task["id"] in selected]


def prepare(task):
    folder = Path(tempfile.mkdtemp(prefix=f"panini-eval-{task['id']}-"))
    for name, content in task["files"].items():
        (folder / name).parent.mkdir(parents=True, exist_ok=True)
        (folder / name).write_text(content, encoding="utf-8")
    for command in (["git", "init", "-q"], ["git", "add", "-A"],
                    ["git", "-c", "user.name=eval", "-c", "user.email=eval@local", "commit", "-qm", "start"]):
        subprocess.run(command, cwd=folder, check=True, capture_output=True)
    return folder


def run_panini(folder, task, model, sandbox, timeout):
    command = [sys.executable, "-m", "panini_cli.agent", "exec", "--json", "-a", "auto",
               "-C", str(folder), "-m", model, task["prompt"]]
    if sandbox:
        command[5:5] = ["--sandbox", sandbox]
    env = {**task_env(), "PYTHONPATH": str(SOURCE)}
    started = time.monotonic()
    completed = subprocess.run(command, env=env, capture_output=True, text=True, encoding="utf-8",
                               timeout=timeout, stdin=subprocess.DEVNULL)
    events = []
    for line in completed.stdout.splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    final = next((e for e in reversed(events) if e.get("type") == "turn.completed"), {})
    routes = [e["model"] for e in events if e.get("type") == "model.routed"]
    return {
        "seconds": round(time.monotonic() - started, 1),
        "status": final.get("status", f"exit {completed.returncode}"),
        "text": final.get("text", ""),
        "tokens": (final.get("usage") or {}).get("total_tokens"),
        "cost_usd": final.get("cost_usd"),
        "models": sorted(set(routes)),
        "stderr": completed.stderr[-2000:] if completed.returncode not in (0, 1) else "",
    }


def run_codex(folder, task, timeout):
    codex = shutil.which("codex")
    if not codex:
        return {"status": "skipped: codex not installed", "seconds": 0, "text": ""}
    started = time.monotonic()
    completed = subprocess.run([codex, "exec", "--full-auto", "--skip-git-repo-check", "-C", str(folder), task["prompt"]],
                               capture_output=True, text=True, encoding="utf-8", timeout=timeout, stdin=subprocess.DEVNULL,
                               env=task_env())
    return {"seconds": round(time.monotonic() - started, 1), "status": f"exit {completed.returncode}",
            "text": completed.stdout[-4000:], "tokens": None, "cost_usd": None, "models": ["codex"]}


def task_env():
    """Agents and checks see this interpreter (which has pytest) first on PATH as `python`."""
    scripts = str(Path(sys.executable).parent)
    return {**os.environ, "PATH": scripts + os.pathsep + os.environ.get("PATH", "")}


def solved(folder, task, result):
    if task["kind"] == "question":
        return bool(re.search(task["answer_regex"], result.get("text", ""), flags=re.IGNORECASE))
    check = subprocess.run(task["check"], cwd=folder, shell=True, capture_output=True, text=True, timeout=120,
                           env=task_env())
    return check.returncode == 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--arm", action="append", required=True)
    parser.add_argument("--task", action="append", default=[])
    parser.add_argument("--sandbox", default=None, help="sandbox for PANINI arms (default: PANINI's default)")
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()
    tasks = load_tasks(set(args.task))
    rows = []
    for arm in args.arm:
        for task in tasks:
            folder = prepare(task)
            try:
                if arm == "codex":
                    result = run_codex(folder, task, args.timeout)
                else:
                    model = "auto" if arm == "auto" else arm.split(":", 1)[1]
                    result = run_panini(folder, task, model, args.sandbox, args.timeout)
                ok = not result["status"].startswith("skipped") and solved(folder, task, result)
            except subprocess.TimeoutExpired:
                result, ok = {"status": "timeout", "seconds": args.timeout}, False
            finally:
                shutil.rmtree(folder, ignore_errors=True)
            row = {"arm": arm, "task": task["id"], "solved": ok, **result}
            rows.append(row)
            cost = "-" if row.get("cost_usd") is None else f"${row['cost_usd']:.4f}"
            print(f"{arm:24} {task['id']:24} {'PASS' if ok else 'FAIL'}  {row['seconds']:>6}s  {cost:>9}  {','.join(row.get('models') or [])}",
                  flush=True)
    write_results(rows)


def write_results(rows):
    out = HERE / "results"
    out.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    (out / f"{stamp}.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    arms = list(dict.fromkeys(row["arm"] for row in rows))
    lines = [f"# Eval results {stamp}", "", "| Arm | Solved | Cost (priced) | Cost per solve | Mean time |", "| --- | ---: | ---: | ---: | ---: |"]
    for arm in arms:
        mine = [row for row in rows if row["arm"] == arm]
        wins = sum(row["solved"] for row in mine)
        costs = [row.get("cost_usd") for row in mine]
        missing = sum(1 for c in costs if c is None)
        total = sum(c or 0 for c in costs)
        per = f"${total / wins:.4f}" if wins and not missing else (f"≥ ${total / wins:.4f}" if wins else "-")
        mean = sum(row.get("seconds", 0) for row in mine) / max(1, len(mine))
        shown = f"${total:.4f}" + (f" + {missing} unpriced" if missing else "")
        lines.append(f"| {arm} | {wins}/{len(mine)} | {shown} | {per} | {mean:.1f}s |")
    lines += ["", "| Arm | Task | Solved | Models | Cost | Time |", "| --- | --- | --- | --- | ---: | ---: |"]
    for row in rows:
        cost = "-" if row.get("cost_usd") is None else f"${row['cost_usd']:.4f}"
        lines.append(f"| {row['arm']} | {row['task']} | {'✓' if row['solved'] else '✗'} | {', '.join(row.get('models') or [])} | {cost} | {row.get('seconds', 0)}s |")
    (out / f"{stamp}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[:3 + len(arms) + 1]))
    print(f"\nWritten to {out / stamp}.md")


if __name__ == "__main__":
    main()
