"""Optional Docker sandbox for commands: no network, dropped capabilities, bounded resources.

Enabled with PANINI_CLI_SANDBOX=docker (or panini-cli --sandbox docker). Only the
workspace is mounted, at /workspace; the rest of the PC is not visible. Commands run
with sh in a Linux image (PANINI_CLI_SANDBOX_IMAGE, default python:3.12-slim).
File tools are unaffected: they already work only inside the workspace.
"""

import os
import shutil
import subprocess
from secrets import token_hex

DEFAULT_IMAGE = "python:3.12-slim"


def docker():
    """docker on PATH, or Docker Desktop's standard location (PATH updates need a new login)."""
    found = shutil.which("docker")
    if found:
        return found
    if os.name == "nt":
        for base in (os.getenv("ProgramFiles"), os.getenv("ProgramW6432"), r"C:\Program Files"):
            candidate = os.path.join(base or "", "Docker", "Docker", "resources", "bin", "docker.exe")
            if base and os.path.isfile(candidate):
                return candidate
    return None


def environment(base=None):
    """The environment for docker commands: its helpers (credential store) live beside it."""
    env = dict(base if base is not None else os.environ)
    executable = docker()
    if executable:
        folder = os.path.dirname(executable)
        key = next((k for k in env if k.upper() == "PATH"), "PATH")
        if folder.lower() not in env.get(key, "").lower().split(os.pathsep):
            env[key] = folder + os.pathsep + env.get(key, "")
    return env


def _run(args, timeout):
    return subprocess.run(
        [docker(), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=environment(),
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


def available():
    """(ready, reason). Docker must be installed and its engine running."""
    executable = docker()
    if not executable:
        return False, "Docker is not installed. Install Docker Desktop or turn the sandbox off."
    try:
        probe = _run(["info", "--format", "{{.OSType}}"], 15)
    except (OSError, subprocess.TimeoutExpired):
        return False, "Docker did not respond. Start Docker Desktop and retry."
    if probe.returncode != 0:
        return False, "The Docker engine is not running. Start Docker Desktop and retry."
    if probe.stdout.strip() != "linux":
        return False, "Docker must be in Linux containers mode."
    return True, ""


def image():
    return os.getenv("PANINI_CLI_SANDBOX_IMAGE") or DEFAULT_IMAGE


def argv(command, workspace, *, name=None, network=False):
    return [
        docker() or "docker",
        "run",
        "--rm",
        "--name",
        name or new_name(),
        "--network",
        "bridge" if network else "none",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "512",
        "--memory",
        os.getenv("PANINI_CLI_SANDBOX_MEMORY", "2g"),
        "--cpus",
        os.getenv("PANINI_CLI_SANDBOX_CPUS", "2"),
        "-e",
        "PYTHONUNBUFFERED=1",
        "-v",
        f"{workspace}:/workspace",
        "-w",
        "/workspace",
        image(),
        "sh",
        "-c",
        command,
    ]


def new_name():
    return f"panini-cli-{token_hex(6)}"


def remove(name):
    """A killed docker CLI can leave its container running; remove it by name."""
    if docker():
        try:
            _run(["rm", "-f", name], 30)
        except (OSError, subprocess.TimeoutExpired):
            pass


def prepare_image(timeout=600):
    """Pull the sandbox image if it is missing; returns (ok, message)."""
    try:
        if _run(["image", "inspect", image()], 30).returncode == 0:
            return True, ""
        pulled = _run(["pull", "-q", image()], timeout)
    except (OSError, subprocess.TimeoutExpired):
        return False, f"Could not pull {image()} in time."
    if pulled.returncode != 0:
        return False, f"Could not pull {image()}: {pulled.stderr.strip()[:300]}"
    return True, ""
