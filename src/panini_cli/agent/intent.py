"""Small local intent hints for machine-control requests.

These hints do not execute anything and do not replace the model. They keep common
Windows tasks from being misread as workspace-only code tasks.
"""

from __future__ import annotations

import re


KNOWN_FOLDER_HINTS = {
    "downloads": "%USERPROFILE%\\Downloads",
    "download": "%USERPROFILE%\\Downloads",
    "desktop": "%USERPROFILE%\\Desktop",
    "documents": "%USERPROFILE%\\Documents",
    "document": "%USERPROFILE%\\Documents",
    "pictures": "%USERPROFILE%\\Pictures",
    "picture": "%USERPROFILE%\\Pictures",
    "videos": "%USERPROFILE%\\Videos",
    "music": "%USERPROFILE%\\Music",
}

SYSTEM_TERMS = (
    "ram",
    "rom",
    "memory",
    "storage",
    "disk",
    "cpu",
    "processor",
    "gpu",
    "system spec",
    "system specs",
    "installed apps",
    "process",
    "processes",
)


def needs_stronger_model(text: str) -> bool:
    lowered = text.casefold()
    if any(term in lowered for term in SYSTEM_TERMS):
        return True
    return any(re.search(rf"\b{name}\b", lowered) for name in KNOWN_FOLDER_HINTS)


def augment(text: str) -> str:
    """Append operational guidance for common local-machine intents."""
    lowered = text.casefold()
    hints = []
    folders = sorted(
        {
            path
            for name, path in KNOWN_FOLDER_HINTS.items()
            if re.search(rf"\b{name}\b", lowered)
        }
    )
    if folders:
        hints.append(
            "The user is likely referring to a Windows known folder, not a workspace-relative "
            f"directory. Use a host-scoped PowerShell command to inspect: {', '.join(folders)}."
        )
    if any(term in lowered for term in SYSTEM_TERMS):
        hints.append(
            "The user is likely asking about the local Windows machine. Use host-scoped "
            "PowerShell commands for real evidence, such as Get-CimInstance, Get-ComputerInfo, "
            "Get-PSDrive, Get-Volume, or Get-Process as appropriate."
        )
    if not hints:
        return text
    return text + "\n\n[PANINI local-machine hint]\n" + "\n".join(f"- {hint}" for hint in hints)
