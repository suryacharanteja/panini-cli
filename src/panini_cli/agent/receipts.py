"""Durable, hash-chained metadata for agent tool calls.

Receipts deliberately contain digests rather than raw command arguments or output.
The conversation stores those payloads separately; the receipt log can establish
their order and detect accidental or deliberate modification of this log.
"""

import hashlib
import json
import os
import re
import time
from pathlib import Path

from .sessions import root as sessions_root

_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
GENESIS = "0" * 64


def digest(value):
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def path_for(session_id):
    if not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id):
        raise ValueError("Invalid receipt session id")
    return sessions_root() / "receipts" / f"{session_id}.jsonl"


def _line(record):
    return json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"


class ReceiptLog:
    def __init__(self, session_id, workspace):
        self.path = path_for(session_id)
        self.workspace_digest = digest(str(Path(workspace).resolve()))
        self.sequence, self.previous = verify(self.path)

    def append(self, *, call_id, tool, phase, arguments=None, output=None, approval=None, error=None, sandbox=None):
        record = {
            "version": 1,
            "sequence": self.sequence + 1,
            "time_ns": time.time_ns(),
            "workspace_digest": self.workspace_digest,
            "call_id": str(call_id),
            "tool": str(tool),
            "phase": phase,
            "previous": self.previous,
        }
        if arguments is not None:
            record["arguments_sha256"] = digest(arguments)
        if output is not None:
            record["output_sha256"] = digest(output)
        if approval is not None:
            record["approval"] = approval
        if error is not None:
            record["error_type"] = type(error).__name__
        if sandbox is not None:
            record["sandbox"] = sandbox
        record["sha256"] = digest(record)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # One append and fsync per event: a killed agent retains the last complete event.
        with self.path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(_line(record))
            stream.flush()
            os.fsync(stream.fileno())
        self.sequence, self.previous = record["sequence"], record["sha256"]
        return record


def verify(path):
    path = Path(path)
    if not path.exists():
        return 0, GENESIS
    sequence, previous = 0, GENESIS
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Receipt line {line_number} is incomplete or invalid") from exc
            claimed = record.pop("sha256", None)
            if (record.get("version") != 1 or record.get("sequence") != sequence + 1
                    or record.get("previous") != previous or claimed != digest(record)):
                raise ValueError(f"Receipt chain is invalid at line {line_number}")
            sequence, previous = record["sequence"], claimed
    return sequence, previous


def report(path, session_id, head):
    """A readable Markdown account of a verified receipt chain, joined with the saved conversation
    (when available) so each call shows what was asked, not only its digest."""
    from . import sessions

    calls = {}
    try:
        for item in sessions.load(session_id).get("items", []):
            if item.get("type") == "function_call":
                calls[item.get("call_id")] = (item.get("name"), str(item.get("arguments", ""))[:300])
    except (KeyError, OSError, ValueError):
        pass
    rows = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
    events = {}
    for row in rows:
        events.setdefault(row["call_id"], []).append(row)
    lines = [
        f"# PANINI CLI receipts · session {session_id}",
        "",
        f"- Events: {len(rows)} · tool calls: {len(events)}",
        f"- Chain head (SHA-256): `{head}` — verified intact",
        "",
        "| # | Time (UTC) | Tool | Sandbox | Approval | Outcome | Arguments |",
        "| ---: | --- | --- | --- | --- | --- | --- |",
    ]
    for index, (call_id, group) in enumerate(events.items(), 1):
        first, last = group[0], group[-1]
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(first["time_ns"] / 1e9))
        approval = next((g["approval"] for g in group if g.get("approval")), "—")
        outcome = last["phase"] + (f" ({last['error_type']})" if last.get("error_type") else "")
        arguments = calls.get(call_id, (None, "(digest only)"))[1].replace("|", r"\|").replace("\n", " ")
        lines.append(
            f"| {index} | {stamp} | {first['tool']} | {first.get('sandbox', '—')} | {approval} | {outcome} | `{arguments}` |"
        )
    lines += ["", "Receipts store SHA-256 digests of arguments and outputs; the chain detects edits or deletions."]
    return "\n".join(lines) + "\n"
