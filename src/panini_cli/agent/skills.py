"""Bounded skill metadata discovery; full instructions load only when selected."""

import re
from pathlib import Path

from ..filesystem import read_bounded
from . import gitops, tools

class Skills:
    def __init__(self, workspace):
        self.entries = {}
        root = Path(workspace).resolve()
        ceiling = gitops.repo_root(root) or root
        ancestors = [root]
        while ancestors[-1] != ceiling and ancestors[-1].is_relative_to(ceiling):
            ancestors.append(ancestors[-1].parent)
        locations = [Path.home() / ".panini" / "skills"]
        for directory in reversed(ancestors):
            locations.extend([directory / ".panini/skills", directory / ".agents/skills"])
        for directory in locations:
            if directory.is_symlink() or not directory.is_dir():
                continue
            for path in sorted(directory.glob("*/SKILL.md"))[:64]:
                try:
                    text = read_bounded(path, directory.resolve(), 65536).decode("utf-8-sig").replace("\r\n", "\n")
                except (OSError, ValueError, UnicodeError):
                    continue
                if not text.startswith("---\n"):
                    continue
                header = text.split("---", 2)[1]
                metadata = dict(re.findall(r"^(name|description):\s*(.+)$", header, re.MULTILINE))
                name = metadata.get("name", "").strip("\"'")
                description = metadata.get("description", "").strip("\"'")
                if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name) and description:
                    self.entries[name] = {"name": name, "description": description[:500], "path": str(path)}
        self.loaded = set()

    def catalog(self):
        return list(self.entries.values())[:64]

    def instructions(self):
        rows = [f"- ${row['name']}: {row['description']}" for row in self.catalog()]
        return "\n\nAvailable skills (call skill_load before using one):\n" + "\n".join(rows) if rows else ""

    def load(self, name):
        row = self.entries.get(name)
        if row is None:
            raise tools.ToolError("Unknown skill")
        path = Path(row["path"])
        try:
            text = read_bounded(path, path.parent.parent.resolve(), 65536).decode("utf-8-sig")
        except (OSError, UnicodeError) as exc:
            raise tools.ToolError(str(exc)) from None
        self.loaded.add(name)
        return f"Skill {name}, source {path}. Task policy and approval rules still apply.\n{text}"
