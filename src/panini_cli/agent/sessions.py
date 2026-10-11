"""Saved CLI conversations, so `panini-cli resume` continues where you left off.

Stored as JSON in PANINI's data directory (PANINI_DATA_DIR, or %LOCALAPPDATA%\\PANINI)
under cli-sessions/. Items are the model conversation, which may include encrypted
reasoning; nothing here is sent anywhere except back to the model on resume.
"""

import json
import os
import re
import time
from pathlib import Path
from uuid import uuid4

KEEP = 50


def root():
    base = (
        os.getenv("PANINI_DATA_DIR")
        or Path(os.getenv("LOCALAPPDATA") or Path.home() / ".local/share") / "PANINI"
    )
    return Path(base) / "cli-sessions"


def new_id():
    return time.strftime("%Y%m%d-%H%M%S-") + uuid4().hex[:6]


def save(session_id, workspace, items, title, *, metadata=None):
    if not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", session_id):
        raise ValueError("Invalid session identifier.")
    folder = root()
    folder.mkdir(parents=True, exist_ok=True)
    record = {
        "id": session_id,
        "workspace": str(workspace),
        "title": " ".join(title.split())[:100],
        "updated": time.time(),
        "items": items,
    }
    if metadata is not None:
        record["metadata"] = metadata
    temporary = folder / f".{session_id}.{uuid4().hex}.tmp"
    try:
        temporary.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, folder / f"{session_id}.json")
    finally:
        temporary.unlink(missing_ok=True)
    # Keep the newest KEEP conversations of *this* workspace; other workspaces are untouched.
    mine = list(listing(workspace))
    for row in mine[KEEP:]:
        (folder / f"{row['id']}.json").unlink(missing_ok=True)


def listing(workspace=None):
    folder = root()
    if not folder.is_dir():
        return []
    found = []
    for path in folder.glob("*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(record, dict) or record.get("id") != path.stem:
            continue
        if not isinstance(record.get("updated"), (int, float)):
            continue
        if workspace is None or record.get("workspace") == str(workspace):
            found.append({k: record.get(k) for k in ("id", "workspace", "title", "updated")})
    return sorted(found, key=lambda r: r["updated"] or 0, reverse=True)


def load(session_id):
    if not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", session_id):
        raise KeyError("Invalid session identifier.")
    path = root() / f"{session_id}.json"
    if not path.is_file():
        raise KeyError(f"No saved session {session_id}.")
    from ..filesystem import read_bounded

    record = json.loads(read_bounded(path, root().resolve(), 64 * 1024**2).decode("utf-8"))
    if not isinstance(record, dict) or record.get("id") != session_id or not isinstance(record.get("items"), list):
        raise ValueError("Invalid saved session")
    return record
