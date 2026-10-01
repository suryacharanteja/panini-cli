"""Saved CLI conversations, so `panini-cli resume` continues where you left off.

Stored as JSON in PANINI's data directory (PANINI_DATA_DIR, or %LOCALAPPDATA%\\PANINI)
under cli-sessions/. Items are the model conversation, which may include encrypted
reasoning; nothing here is sent anywhere except back to the model on resume.
"""

import json
import os
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


def save(session_id, workspace, items, title):
    folder = root()
    folder.mkdir(parents=True, exist_ok=True)
    record = {
        "id": session_id,
        "workspace": str(workspace),
        "title": " ".join(title.split())[:100],
        "updated": time.time(),
        "items": items,
    }
    temporary = folder / f".{session_id}.tmp"
    temporary.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, folder / f"{session_id}.json")
    # Keep the newest KEEP conversations of *this* workspace; other workspaces are untouched.
    mine = [row for row in listing(workspace)]
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
        if workspace is None or record.get("workspace") == str(workspace):
            found.append({k: record.get(k) for k in ("id", "workspace", "title", "updated")})
    return sorted(found, key=lambda r: r["updated"] or 0, reverse=True)


def load(session_id):
    path = root() / f"{Path(session_id).name}.json"
    if not path.is_file():
        raise KeyError(f"No saved session {session_id}.")
    return json.loads(path.read_text(encoding="utf-8"))
