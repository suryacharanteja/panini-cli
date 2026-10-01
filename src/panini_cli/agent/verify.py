"""Verification-first completion: find how this project checks itself, and hold the agent to it.

The detected commands are added to the agent's instructions. If a turn changes files and the
agent tries to finish without running any command afterwards, it is sent back once to run the
checks (or say why it cannot). PANINI_CLI_AUTO_VERIFY=0 turns the reminder off.
"""

from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path


def enabled():
    return os.getenv("PANINI_CLI_AUTO_VERIFY", "1").lower() not in ("0", "false", "no", "off")


def commands(workspace):
    """Likely test/lint/type-check commands for the project rooted at `workspace`."""
    root = Path(workspace)
    found = []
    pyproject = root / "pyproject.toml"
    data = {}
    if pyproject.is_file():
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            data = {}
    tool = data.get("tool", {}) if isinstance(data, dict) else {}
    has_python_tests = (
        "pytest" in tool
        or (root / "pytest.ini").is_file()
        or (root / "tests").is_dir()
        or any(root.glob("test_*.py"))
    )
    if has_python_tests:
        found.append("python -m pytest -q")
    if "ruff" in tool or (root / "ruff.toml").is_file():
        found.append("ruff check .")
    package = root / "package.json"
    if package.is_file():
        try:
            scripts = json.loads(package.read_text(encoding="utf-8")).get("scripts", {}) or {}
        except (OSError, ValueError):
            scripts = {}
        test = scripts.get("test", "")
        if test and "no test specified" not in test:
            found.append("npm test")
        if "lint" in scripts:
            found.append("npm run lint")
        if (root / "tsconfig.json").is_file() and "typecheck" not in scripts:
            found.append("npx tsc --noEmit")
        elif "typecheck" in scripts:
            found.append("npm run typecheck")
    if (root / "Cargo.toml").is_file():
        found.append("cargo test")
    if (root / "go.mod").is_file():
        found.append("go test ./...")
    if any(root.glob("*.sln")) or any(root.glob("*.csproj")):
        found.append("dotnet test")
    return found


def instructions(workspace):
    found = commands(workspace)
    if not found:
        return ""
    return (
        "\n\nVerification\n- This project's checks: "
        + ", ".join(f"`{command}`" for command in found)
        + ". After changing code, run the relevant ones and report their result. Never say the work is "
        "verified unless a check you ran passed."
    )


def reminder(changed, found):
    files = ", ".join(changed[:8]) + (" …" if len(changed) > 8 else "")
    return (
        "[PANINI verification check] You changed " + files + " but have not run any command since. "
        "Run the relevant check (" + ", ".join(f"`{c}`" for c in found) + ") and report the result, "
        "or explain why it cannot be run, before finishing."
    )
