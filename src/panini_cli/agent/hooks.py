"""Hooks: your own commands that run at points in the agent's work.

Files (JSON), both optional:
- ~/.panini/hooks.json              yours; always trusted
- <workspace>/.panini/hooks.json    the project's; runs only after you trust it with
                                    `panini-cli hooks --trust` (a repository must not be
                                    able to run code on your PC just by being opened)

    {"hooks": {
        "PreToolUse":  [{"matcher": "run_command|apply_patch", "command": "python .panini/guard.py"}],
        "PostToolUse": [{"matcher": "edit_file|write_file|apply_patch", "command": "ruff format ."}],
        "TurnEnd":     [{"command": "git diff --stat"}],
        "SessionStart": [{"command": "git status --short"}]
    }}

Each hook gets one JSON object on stdin: {"event", "tool", "arguments", "workspace",
"result"}. Exit code 0 continues (stdout, if any, is shown to the agent as context);
exit code 2 on PreToolUse blocks the tool call and stderr is the reason the agent sees;
any other failure is reported but does not block. Hooks run with the same safe
environment as commands (no credentials) and a timeout (default 30 s, at most 120 s).
"""

import asyncio
import hashlib
import json
import re
from pathlib import Path

from .shell import run_command

EVENTS = ("SessionStart", "PreToolUse", "PostToolUse", "TurnEnd")
TRUST_FILE = Path.home() / ".panini" / "trusted-hooks.json"


def _read(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    hooks = data.get("hooks") if isinstance(data, dict) else None
    return hooks if isinstance(hooks, dict) else {}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def trusted_digests():
    try:
        return set(json.loads(TRUST_FILE.read_text(encoding="utf-8")).get("trusted", []))
    except (OSError, ValueError, AttributeError):
        return set()


def trust(path):
    """Record the exact content of a project hooks file as trusted."""
    known = trusted_digests() | {digest(path)}
    TRUST_FILE.parent.mkdir(parents=True, exist_ok=True)
    TRUST_FILE.write_text(json.dumps({"trusted": sorted(known)}, indent=2), encoding="utf-8")


class Hooks:
    def __init__(self, workspace, *, user_file=None):
        self.workspace = Path(workspace)
        self.user_file = Path(user_file) if user_file else Path.home() / ".panini" / "hooks.json"
        self.project_file = self.workspace / ".panini" / "hooks.json"
        self.entries = {event: [] for event in EVENTS}
        self.untrusted = False
        self.load()

    def load(self):
        self.entries = {event: [] for event in EVENTS}
        sources = [("user", self.user_file)]
        self.untrusted = False
        if self.project_file.is_file():
            if digest(self.project_file) in trusted_digests():
                sources.append(("project", self.project_file))
            else:
                self.untrusted = True  # Present but changed or never trusted: ignored.
        for origin, path in sources:
            for event, items in _read(path).items():
                if event not in EVENTS or not isinstance(items, list):
                    continue
                for item in items[:20]:
                    if (
                        isinstance(item, dict)
                        and isinstance(item.get("command"), str)
                        and item["command"].strip()
                    ):
                        self.entries[event].append(
                            {
                                "origin": origin,
                                "matcher": str(item.get("matcher") or ""),
                                "command": item["command"],
                                "timeout": max(1, min(int(item.get("timeout") or 30), 120)),
                            }
                        )

    def describe(self):
        return [{"event": event, **entry} for event, entries in self.entries.items() for entry in entries]

    def _matches(self, entry, tool):
        if not entry["matcher"] or tool is None:
            return True
        try:
            return re.fullmatch(entry["matcher"], tool) is not None
        except re.error:
            return False

    async def run(self, event, *, tool=None, arguments=None, result=None):
        """Returns (blocked_reason or None, context text from hooks)."""
        payload = json.dumps(
            {
                "event": event,
                "tool": tool,
                "arguments": arguments,
                "workspace": str(self.workspace),
                "result": (result or "")[:20000] if isinstance(result, str) else result,
            },
            ensure_ascii=False,
        )
        notes = []
        for entry in self.entries.get(event, []):
            if not self._matches(entry, tool):
                continue
            outcome = await run_command(
                entry["command"], self.workspace, entry["timeout"], stdin_text=payload, separate_stderr=True
            )
            label = f"{event} hook `{entry['command'][:80]}`"
            if outcome.timed_out:
                notes.append(f"{label} timed out.")
            elif outcome.exit_code == 2 and event == "PreToolUse":
                reason = (outcome.stderr or outcome.output).strip()[:2000] or "Blocked by a hook."
                return reason, "\n".join(notes)
            elif outcome.exit_code not in (0, None):
                notes.append(
                    f"{label} failed (exit {outcome.exit_code}): {(outcome.stderr or '').strip()[:500]}"
                )
            elif outcome.output.strip():
                notes.append(f"{label}:\n{outcome.output.strip()[:4000]}")
        return None, "\n".join(notes)


class NoHooks:
    untrusted = False

    def describe(self):
        return []

    async def run(self, event, **_):
        await asyncio.sleep(0)
        return None, ""
