"""Which commands may run without asking.

A command is *trusted* when every part of it only reads, and only reads inside the
workspace: `git status`, `git diff`, `ls`, `Get-Content src/app.py`, `rg TODO` and the
like. Anything that could write, delete, reach the network, run arbitrary code (script
blocks, subexpressions, redirection) or read outside the workspace (absolute paths, `..`,
`~`, environment variables) is not trusted and still needs approval.

The user can also approve an exact command for the rest of the session.
"""

from __future__ import annotations

import re
import shlex

# Characters that can write files or run code inside an otherwise harmless command.
_UNSAFE = re.compile(r"[<>{}`]|\$\(|@\(|\$env:|%[A-Za-z_]+%|\$\{|\$[A-Za-z_]")
_SEPARATORS = re.compile(r"\s*(?:\|\||&&|\||;)\s*")

READ_ONLY = {
    # PowerShell
    "get-childitem", "gci", "dir", "ls", "get-content", "gc", "cat", "type", "get-item", "gi",
    "get-location", "pwd", "test-path", "resolve-path", "select-string", "sls", "measure-object",
    "measure", "select-object", "select", "sort-object", "sort", "format-table", "ft", "format-list",
    "fl", "out-string", "get-command", "gcm", "where.exe", "write-output", "echo", "get-filehash",
    "get-date",
    # POSIX / cross-platform
    "head", "tail", "wc", "grep", "rg", "find", "tree", "which", "file", "stat", "du", "findstr",
}
GIT_READ_ONLY = {"status", "diff", "log", "show", "rev-parse", "ls-files", "blame", "shortlog", "describe", "grep"}
GIT_BRANCH_READ = {"-a", "-v", "-vv", "--list", "-r", "--show-current", "--all", "--remotes"}
VERSION_FLAGS = {"--version", "-v", "-V", "version", "--help", "-h"}
# Options that make an otherwise read-only command write or execute.
_DANGEROUS_OPTIONS = ("--output", "-o", "--exec", "--pre", "-delete", "-exec", "-execdir", "-fprint", "--ext-diff")


def _segments(command):
    return [part for part in _SEPARATORS.split(command.strip()) if part]


def _words(segment):
    try:
        return shlex.split(segment, posix=False)
    except ValueError:
        return None


def _path_ok(word):
    """Arguments that look like paths must stay inside the workspace."""
    text = word.strip("'\"")
    if not text or text.startswith("-"):
        return True
    if text.startswith(("~", "\\\\", "/")) or re.match(r"^[A-Za-z]:", text):
        return False
    return ".." not in re.split(r"[\\/]", text)


def _segment_trusted(segment):
    words = _words(segment)
    if not words:
        return False
    name = words[0].strip("'\"").lower()
    if name.endswith(".exe"):
        name = name[:-4]
    args = words[1:]
    if any(arg.lower().startswith(_DANGEROUS_OPTIONS) for arg in args):
        return False
    if not all(_path_ok(arg) for arg in args):
        return False
    if name == "git":
        if not args:
            return False
        sub = args[0].lower()
        if sub in GIT_READ_ONLY:
            return True
        if sub == "branch":
            return all(arg in GIT_BRANCH_READ for arg in args[1:])
        if sub == "remote":
            return args[1:] in ([], ["-v"])
        return False
    if name in READ_ONLY:
        return True
    # `python --version`, `node -v`, `npm --version`, ...
    return len(args) == 1 and args[0] in VERSION_FLAGS


def is_trusted(command: str) -> bool:
    """True when every part of `command` only reads inside the workspace."""
    if not isinstance(command, str) or not command.strip() or "\n" in command.strip():
        return False
    if _UNSAFE.search(command):
        return False
    segments = _segments(command)
    return bool(segments) and all(_segment_trusted(segment) for segment in segments)


class SessionRules:
    """Exact commands the user approved for the rest of this session."""

    def __init__(self):
        self.commands = set()

    def allow(self, command):
        self.commands.add(command.strip())

    def allowed(self, command):
        return command.strip() in self.commands
