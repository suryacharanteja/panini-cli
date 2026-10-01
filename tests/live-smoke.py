"""Opt-in live test of PANINI-CLI with the real model, through PANINI's own CodeEngine.

Uses a disposable workspace and approves only the expected file and command.
Costs a few cents of model usage. Run: .venv\\Scripts\\python.exe panini-cli\\tests\\live-smoke.py
"""

import asyncio
import hashlib
import json
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from panini_cli.agent.config import load_panini_env  # noqa: E402
from panini_cli.engine import CodeEngine  # noqa: E402
from panini_cli.verification import FileCondition  # noqa: E402

CONTENT = 'def answer():\n    return 6 * 7\n\n\nif __name__ == "__main__":\n    print(answer())\n'


async def main():
    load_panini_env()
    workspace = Path(tempfile.mkdtemp(prefix="panini-cli-live-"))
    events, decisions = [], []

    async def confirm(review):
        request, item = review["request"], review.get("item", {})
        if review["method"].endswith("fileChange/requestApproval"):
            changes = item.get("changes", [])
            accepted = (
                len(changes) == 1
                and changes[0]["path"] == "answer.py"
                and all(f"+{line}" in changes[0]["diff"] for line in CONTENT.splitlines() if line)
            )
        else:
            command = request.get("command", "")
            accepted = "answer.py" in command and Path(request.get("cwd", "")).resolve() == workspace
        decisions.append({"method": review["method"], "accepted": accepted})
        return accepted

    async def emit(record):
        events.append(record)

    engine = CodeEngine(workspace, workspace.parent / f"{workspace.name}.sqlite3")
    try:
        result = await engine.run(
            str(uuid4()),
            "Create answer.py in the workspace with exactly this content (LF line endings, final newline):\n"
            + CONTENT
            + f"\nThen run it with this Python interpreter: {sys.executable}\n"
            "Report the program's output. Do not create or change any other file.",
            confirm=confirm,
            emit=emit,
            check=lambda: None,
            timeout=240,
            reasoning_effort="low",
            postconditions=[FileCondition("answer.py", hashlib.sha256(CONTENT.encode()).hexdigest())],
        )
    finally:
        await engine.close()
    ran = [c for c in result.commands if c["exit_code"] == 0 and "42" in c["output"]]
    print(json.dumps({"decisions": decisions, "result": asdict(result)}, indent=2))
    assert result.verified, "answer.py does not match the requested content"
    assert ran, "the program was not run successfully"
    print(f"PASS live PANINI-CLI: file verified by SHA-256, program printed 42 ({workspace})")


if __name__ == "__main__":
    asyncio.run(main())
