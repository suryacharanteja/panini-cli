"""Optional native Windows command isolation via the installed Codex CLI.

This adapter invokes Codex's stable sandbox command, not its agent or model.
PANINI remains responsible for approval, process lifetime, and output receipts.
"""

import os
import shutil
import subprocess

from .shell import safe_environment, shell_argv


def executable():
    return os.getenv("PANINI_CLIX_BIN") or shutil.which("codex")


def argv(command, workspace):
    binary = executable()
    if not binary:
        raise RuntimeError("Codex CLI was not found. Install it or choose local/Docker command mode.")
    return [binary, "sandbox", "-P", ":workspace", "-C", str(workspace), "--", *shell_argv(command)]


def available(workspace):
    if os.name != "nt":
        return False, "Codex native sandbox mode is supported by PANINI only on Windows."
    if not executable():
        return False, "Codex CLI was not found."
    try:
        result = subprocess.run(
            argv("exit 0", workspace),
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW,
            env=safe_environment(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)[:200]
    if result.returncode:
        return False, (result.stderr or result.stdout or "Codex sandbox probe failed")[:200]
    return True, "Codex native Windows workspace sandbox is ready."
