"""Bounded command snapshots outside the workspace, with conflict-aware restoration.

Capture regular project files including untracked files. Secrets and linked files
are never copied. This is file recovery, not rollback of external side effects.
"""

import hashlib
import json
import os
import re
import shutil
import threading
from pathlib import Path
from uuid import uuid4

from . import native_sandbox, tools

MAX_BYTES = 512 * 1024**2
MAX_FILES = 50_000
MAX_STORAGE_BYTES = 2 * 1024**3
MAX_SNAPSHOTS = 20
_CAPTURE_LOCK = threading.Lock()
EXCLUDED = {".git", ".panini"}


def root():
    # Shared between source Python (including Windows Store) and the executable.
    configured = os.getenv("PANINI_DATA_DIR")
    return (Path(configured) if configured else Path.home() / ".panini") / "recovery"


def files(workspace):
    if not workspace.exists() and not workspace.is_symlink():
        return {}
    native_sandbox._reject_unsafe_workspace_links(workspace)
    found = {}
    for directory, dirs, names in os.walk(workspace, followlinks=False):
        dirs[:] = [name for name in dirs if name not in EXCLUDED]
        for name in names:
            path = Path(directory) / name
            if not tools.is_secret(name) and path.is_file() and not path.is_symlink() and path.stat().st_nlink == 1:
                found[path.relative_to(workspace).as_posix()] = path
    return found


def checksum(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


class Snapshot:
    def __init__(self, workspace):
        with _CAPTURE_LOCK:
            self._capture(workspace)

    def _capture(self, workspace):
        self.workspace = Path(workspace).resolve(strict=True)
        self.id = uuid4().hex
        self.path = root().resolve() / self.id
        if self.path.is_relative_to(self.workspace):
            raise OSError("Recovery storage must be outside the writable project.")
        current = files(self.workspace)
        size = sum(p.stat().st_size for p in current.values())
        if len(current) > MAX_FILES or size > MAX_BYTES:
            raise OSError("Recovery snapshot exceeds 50,000 files or 512 MiB; command was not started.")
        manifests = list(root().glob("*/manifest.json"))
        stored = sum(json.loads(path.read_text(encoding="utf-8")).get("bytes", 0) for path in manifests)
        if len(manifests) >= MAX_SNAPSHOTS or stored + size > MAX_STORAGE_BYTES:
            raise OSError("Recovery storage reached 20 snapshots or 2 GiB; archive old snapshots before retrying.")
        self.path.mkdir(parents=True)
        before = {}
        for label, source in current.items():
            target = self.path / "files" / label
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            before[label] = checksum(target)
        self.manifest = {"workspace": str(self.workspace), "bytes": size, "before": before, "after": None}
        self._save()

    def _save(self):
        target = self.path / "manifest.json"
        staging = target.with_suffix(".tmp")
        staging.write_text(json.dumps(self.manifest, sort_keys=True), encoding="utf-8")
        staging.replace(target)

    def finish(self):
        self.manifest["after"] = {label: checksum(path) for label, path in files(self.workspace).items()}
        self._save()


def restore(snapshot_id, workspace):
    if not re.fullmatch(r"[a-f0-9]{32}", snapshot_id):
        raise ValueError("Invalid recovery snapshot id.")
    workspace = Path(workspace).resolve(strict=False)
    snapshot = root().resolve() / snapshot_id
    if snapshot.is_relative_to(workspace):
        raise OSError("Recovery storage cannot be inside the writable project.")
    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    if os.path.normcase(manifest["workspace"]) != os.path.normcase(str(workspace)):
        raise OSError("Recovery snapshot belongs to a different workspace.")
    if manifest["after"] is None:
        raise OSError("Snapshot has no completion record; automatic restoration cannot prove which files changed.")
    workspace.mkdir(parents=True, exist_ok=True)
    restored, conflicts = [], []
    before, after = manifest["before"], manifest["after"]
    native_sandbox._reject_unsafe_workspace_links(workspace)
    for label in sorted(set(before) | set(after)):
        target = (workspace / label).resolve()
        if not target.is_relative_to(workspace) or tools.is_secret(target.name):
            raise OSError("Unsafe path in recovery manifest.")
        if before.get(label) == after.get(label):
            continue
        if target.exists() and not target.is_file():
            conflicts.append(label)
            continue
        actual = checksum(target) if target.exists() else None
        if actual != after.get(label):
            conflicts.append(label)
            continue
        if label in before:
            backup = snapshot / "files" / label
            if checksum(backup) != before[label]:
                raise OSError("Recovery backup integrity check failed.")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup, target)
        else:
            target.unlink()
        restored.append(label)
    return restored, conflicts
