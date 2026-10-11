"""Git checkpoints: every turn is bracketed by snapshots, so /undo can revert what a turn changed —
including files changed by commands, not only by the file tools — and /diff can show the session.

A snapshot is a commit object built from a private index (the user's index and branches are never
touched) and kept under refs/panini/. Ignored files are not captured. Undo restores only paths
that changed during the turn and still hold exactly what the turn left there; anything edited
since is skipped rather than overwritten.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .shell import safe_environment

# A repository's own config must not run programs during PANINI's bookkeeping.
SAFE = ["-c", "core.fsmonitor=false", "-c", "core.hooksPath=" + os.devnull, "-c", "diff.external="]


class GitError(Exception):
    pass


def _git(root, *args, env=None, check=True, input_text=None):
    environment = {**safe_environment(), **(env or {})}
    # Git add/restore invoke repository-selected clean/smudge/process filters.
    # Query their names with a fixed read-only command and override them before
    # any operation. Do not cache: repo config can change between checkpoints.
    configured = subprocess.run(
        ["git", *SAFE, "-C", str(root), "config", "--null", "--name-only", "--get-regexp", r"^filter\."],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=environment, timeout=30,
    )
    if configured.returncode not in (0, 1):
        raise GitError("Cannot inspect Git filter configuration safely.")
    filters = sorted({key.rsplit(".", 1)[0] for key in configured.stdout.split("\0") if key and "." in key})
    overrides = [argument for driver in filters for suffix, value in (("clean", ""), ("smudge", ""), ("process", ""), ("required", "false"))
                 for argument in ("-c", f"{driver}.{suffix}={value}")]
    if args and args[0] == "diff":
        args = ("diff", "--no-textconv", *args[1:])
    completed = subprocess.run(
        ["git", *SAFE, "-c", "commit.gpgsign=false", *overrides, "-C", str(root), *args],
        # Never inherit stdin: in app-server mode it is the protocol pipe, and on Windows starting a
        # child that inherits a pipe another thread is blocked reading hangs process creation.
        stdin=subprocess.DEVNULL if input_text is None else None,
        input=input_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
        timeout=120,
    )
    if check and completed.returncode != 0:
        raise GitError(completed.stderr.strip()[:300] or f"git {args[0]} failed")
    return completed.stdout


def repo_root(workspace):
    try:
        return Path(_git(workspace, "rev-parse", "--show-toplevel").strip()).resolve()
    except (GitError, OSError, subprocess.SubprocessError):
        return None


def enabled():
    return os.getenv("PANINI_CLI_CHECKPOINTS", "1").lower() not in ("0", "false", "no", "off")


def snapshot(workspace, ref=None):
    """Commit the working tree as it is now (tracked + untracked, minus ignored). Returns the sha."""
    root = repo_root(workspace)
    if root is None:
        return None
    with tempfile.TemporaryDirectory(prefix="panini-index-") as scratch:
        env = {"GIT_INDEX_FILE": str(Path(scratch) / "index")}
        has_head = _git(root, "rev-parse", "--verify", "-q", "HEAD", check=False).strip()
        if has_head:
            _git(root, "read-tree", "HEAD", env=env)
        _git(root, "add", "-A", "--", ".", env=env)
        tree = _git(root, "write-tree", env=env).strip()
    parents = ["-p", has_head] if has_head else []
    commit = _git(
        root,
        "-c", "user.name=PANINI", "-c", "user.email=panini@localhost",
        "commit-tree", tree, *parents, "-m", "PANINI checkpoint",
    ).strip()
    if ref:
        _git(root, "update-ref", f"refs/panini/{ref}", commit)
    return commit


def _scope(root, workspace):
    relative = Path(workspace).resolve().relative_to(root).as_posix()
    return relative or "."


def changed_paths(workspace, old, new):
    """Repository-relative paths that differ between two snapshots, inside the workspace."""
    root = repo_root(workspace)
    if root is None or not old or not new:
        return []
    out = _git(root, "diff", "--no-ext-diff", "--name-only", "--no-renames", old, new, "--", _scope(root, workspace))
    return [line for line in out.splitlines() if line]


def diff(workspace, old, new=None):
    root = repo_root(workspace)
    if root is None or not old:
        return ""
    new = new or snapshot(workspace)
    return _git(root, "diff", "--no-ext-diff", "--stat", "--patch", old, new, "--", _scope(root, workspace))


def _blob(root, commit, path):
    out = _git(root, "rev-parse", "-q", "--verify", f"{commit}:{path}", check=False).strip()
    return out or None


@dataclass
class UndoResult:
    restored: list
    removed: list
    skipped: list


def undo(workspace, start, end):
    """Put back what the turn between `start` and `end` changed, where nothing changed since."""
    root = repo_root(workspace)
    if root is None:
        raise GitError("not a git repository")
    now = snapshot(workspace)
    edited_since = set(changed_paths(workspace, end, now))
    restored, removed, skipped = [], [], []
    for path in changed_paths(workspace, start, end):
        if path in edited_since:
            skipped.append(path)
            continue
        target = root / path
        if _blob(root, start, path) is None:
            target.unlink(missing_ok=True)  # Created during the turn.
            removed.append(path)
        else:
            _git(root, "restore", "--source", start, "--worktree", "--", path)
            restored.append(path)
    return UndoResult(restored, removed, skipped)
