"""Interactive input: history, multiline, `/` command and `@file` completion, and a status bar.

Uses prompt_toolkit when stdin is a real console; otherwise falls back to a plain prompt.
Enter sends; Alt+Enter (or Esc then Enter) inserts a newline; ↑/↓ browse history; Ctrl+R searches it.
`@path` in a message attaches that workspace file (read through the same secret-file rules as
the agent's read_file tool).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from . import tools

COMMANDS = [
    "/help", "/new", "/diff", "/review", "/commit", "/compact", "/undo", "/plan", "/model", "/approvals",
    "/image", "/power", "/exit", "/doctor", "/sessions",
]
MAX_INDEXED_FILES = 5000
MAX_ATTACH_LINES = 400
_MENTION = re.compile(r"(?<![\w@])@([\w./\\-]+)")


def workspace_files(root):
    found = []
    for directory, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in tools.SKIP_DIRS and not d.startswith("."))
        for name in sorted(files):
            found.append((Path(directory) / name).relative_to(root).as_posix())
            if len(found) >= MAX_INDEXED_FILES:
                return found
    return found


def expand_mentions(text, workspace):
    """Append the contents of @-mentioned workspace files. Unknown mentions are left alone."""
    ws = tools.Workspace(workspace)
    attached = []
    for name in dict.fromkeys(_MENTION.findall(text)):
        path = (ws.root / name)
        if not path.is_file():
            continue
        try:
            body = tools.read_file(ws, name, 1, MAX_ATTACH_LINES)
        except tools.ToolError as exc:
            attached.append(f"[@{name} not attached: {exc}]")
            continue
        attached.append(f"[Attached file @{name}]\n{body}")
    if not attached:
        return text
    return text + "\n\n" + "\n\n".join(attached)


class PlainInput:
    def __init__(self, console):
        self.console = console

    def ask(self, status=""):
        return self.console.input("\n[bold cyan]›[/] ")


class RichInput:
    def __init__(self, workspace, history_path):
        from prompt_toolkit import PromptSession
        from prompt_toolkit.completion import Completer, Completion
        from prompt_toolkit.history import FileHistory
        from prompt_toolkit.key_binding import KeyBindings

        files = workspace_files(workspace)

        class _Complete(Completer):
            def get_completions(self, document, complete_event):
                word = document.get_word_before_cursor(WORD=True)
                if word.startswith("/") and document.text_before_cursor.strip() == word:
                    for command in COMMANDS:
                        if command.startswith(word):
                            yield Completion(command, start_position=-len(word))
                elif word.startswith("@"):
                    needle = word[1:].lower()
                    shown = 0
                    for name in files:
                        if needle in name.lower():
                            yield Completion("@" + name, start_position=-len(word))
                            shown += 1
                            if shown >= 50:
                                break

        keys = KeyBindings()

        @keys.add("escape", "enter")
        def _newline(event):
            event.current_buffer.insert_text("\n")

        history_path.parent.mkdir(parents=True, exist_ok=True)
        self.session = PromptSession(
            history=FileHistory(str(history_path)),
            completer=_Complete(),
            complete_while_typing=True,
            key_bindings=keys,
            multiline=False,
        )

    def ask(self, status=""):
        from prompt_toolkit.formatted_text import HTML

        def toolbar():
            return HTML(f"<b>PANINI</b> {status}") if status else ""

        return self.session.prompt("› ", bottom_toolbar=toolbar)


def make_input(console, workspace, history_path, interactive):
    if interactive and os.getenv("PANINI_CLI_PLAIN_INPUT", "") not in ("1", "true"):
        try:
            return RichInput(workspace, history_path)
        except Exception:  # noqa: BLE001 -- any terminal incompatibility falls back to plain input.
            pass
    return PlainInput(console)
