"""One command for managed coding, replay and the upstream native interfaces."""

import argparse
import asyncio
import json
import os
import subprocess
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from .engine import CodeEngine, find_panini_cli, runtime_environment
from .progress import EventJournal


def data_path():
    data_root = os.getenv("PANINI_CLI_DATA") or os.getenv("JEV_REV_CODE_DATA")
    return Path(data_root or Path.home() / ".panini-cli") / "events.sqlite3"


async def execute(args):
    engine = CodeEngine(args.workspace, data_path())
    try:
        if args.command == "doctor":
            print(json.dumps(await engine.doctor(), indent=2))
            return 0
        run_id = str(uuid4())
        print(json.dumps({"type": "run.accepted", "run_id": run_id}))

        async def confirm(review):
            # Noninteractive consumers must supply the coordinator API, never auto-approve.
            if not args.interactive or not __import__("sys").stdin.isatty():
                return False
            print(json.dumps(review, indent=2))
            answer = await asyncio.to_thread(input, "Approve this one action? [y/N] ")
            return answer.strip().lower() == "y"

        async def emit(record):
            print(json.dumps(record, ensure_ascii=False), flush=True)

        schema = (
            json.loads(Path(args.output_schema).read_text(encoding="utf-8")) if args.output_schema else None
        )
        result = await engine.run(
            run_id,
            args.instruction,
            confirm=confirm,
            emit=emit,
            check=lambda: None,
            model=args.model,
            output_schema=schema,
        )
        print(json.dumps({"run_id": run_id, **asdict(result)}, ensure_ascii=False, indent=2))
        return 0 if result.status in {"partial", "verified_complete"} else 1
    finally:
        await engine.close()


def main():
    parser = argparse.ArgumentParser(
        description="PANINI coding capability with supervised execution and recovery"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor", help="Check real native app-server handshake without a model call")
    doctor.add_argument("--workspace", default=".")
    run = commands.add_parser("run", help="Run a workspace-scoped coding turn")
    run.add_argument("instruction")
    run.add_argument("--workspace", required=True)
    run.add_argument("--interactive", action="store_true")
    run.add_argument("--model")
    run.add_argument("--output-schema")
    replay = commands.add_parser("events", help="Replay persisted events; never redispatch a task")
    replay.add_argument("run_id")
    replay.add_argument("--after", type=int, default=0)
    native = commands.add_parser(
        "native",
        help="Run the coding runtime's own CLI directly (panini-cli, or the configured runtime)",
    )
    native.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.command == "native":
        values = args.arguments[1:] if args.arguments[:1] == ["--"] else args.arguments
        command = find_panini_cli()
        raise SystemExit(subprocess.call([*command, *values], env=runtime_environment(command)))
    if args.command == "events":
        journal = EventJournal(data_path())
        try:
            print(
                json.dumps(
                    journal.replay(args.run_id, args.after),
                    ensure_ascii=False,
                    indent=2,
                )
            )
        finally:
            journal.close()
        return
    try:
        raise SystemExit(asyncio.run(execute(args)))
    except KeyboardInterrupt:
        raise SystemExit(130) from None
