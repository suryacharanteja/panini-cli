"""Deterministic host readback. Conditions come from the caller, never agent output."""

import hashlib
from dataclasses import dataclass
from pathlib import Path

from .filesystem import read_bounded


@dataclass(frozen=True)
class FileCondition:
    path: str
    sha256: str


def file_receipt(workspace, name):
    root = Path(workspace).resolve(strict=True)
    path = (root / name).resolve(strict=True)
    if not path.is_relative_to(root) or not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("Readback must be a bounded file inside the selected workspace")
    try:
        content = read_bounded(path, root, 4 * 1024 * 1024)
    except OSError as exc:
        raise ValueError(str(exc)) from exc
    return {
        "path": str(path.relative_to(root)),
        "bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def verify_files(workspace, conditions):
    if not conditions or len(conditions) > 30:
        return []
    receipts = []
    for condition in conditions:
        receipt = file_receipt(workspace, condition.path)
        if receipt["sha256"] != condition.sha256:
            return []
        receipts.append(receipt)
    return receipts
