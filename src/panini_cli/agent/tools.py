"""Workspace tools. Paths never leave the workspace; secret files are never read or overwritten.

Reads run without approval. A file change is prepared first (so the user can review
the exact diff) and applied only after approval, and only if the file is unchanged.
"""

import codecs
import difflib
import fnmatch
import hashlib
import json
import os
import re
import secrets
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

# Dependency and build trees are skipped while walking; an explicit path still works.
SKIP_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".tox",
        ".nox",
        "dist",
        "build",
        "bin",
        "obj",
        ".next",
        ".nuxt",
        ".svelte-kit",
        "target",
        "coverage",
        ".gradle",
        ".idea",
    }
)
_SECRET = re.compile(
    r"(?i)^(\.env(\..*)?|.*\.(pem|key|p12|pfx|jks|keystore|kdbx|ppk)|id_(rsa|dsa|ecdsa|ed25519)"
    r"|\.npmrc|\.pypirc|\.netrc|\.git-credentials|credentials(\.json)?|secrets?\.(json|ya?ml|toml))$"
)
_TEMPLATE = re.compile(r"(?i)^\.env\.(example|sample|template|dist|defaults)$")

MAX_READ_LINES = 1000
MAX_READ_CHARS = 50_000  # Larger files are read in line ranges.
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_WRITE_BYTES = 2 * 1024 * 1024
MAX_LIST_ENTRIES = 400
MAX_MATCHES = 200
SEARCH_BYTES = 64 * 1024 * 1024
SEARCH_SECONDS = 15
CODE_SEARCH_MAX_RESULTS = 20
# Lines that define a symbol rank above lines that merely mention it.
_DEFINITION = re.compile(
    r"^\s*(?:export\s+(?:default\s+)?)?(?:async\s+)?(?:def|class|function|fn|pub\s+fn|func|interface|type|struct|"
    r"enum|impl|trait|const|let|var|module|namespace|public\s+(?:static\s+)?(?:class|interface|\w+\s+\w+\s*\())\b"
)
CODE_SEARCH_MAX_CHARS = 12_000


class ToolError(Exception):
    """A failure the model can act on; the message is returned to it as the tool result."""


def is_secret(name):
    return bool(_SECRET.match(name)) and not _TEMPLATE.match(name)


def _binary(data):
    return b"\0" in data[:8192]


class Workspace:
    def __init__(self, root):
        self.root = Path(root).resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("The workspace must be an existing directory")

    def resolve(self, value, *, must_exist=True):
        if not isinstance(value, str) or not value.strip():
            raise ToolError("A path is required.")
        raw = value.strip()
        candidate = Path(raw)
        path = (candidate if candidate.is_absolute() else self.root / candidate).resolve(strict=False)
        if path != self.root and not path.is_relative_to(self.root):
            raise ToolError(f"{raw} is outside the workspace. Only {self.root} is accessible.")
        relative = path.relative_to(self.root)
        if relative.parts[:1] == (".git",):
            raise ToolError("The .git directory is managed by git. Use git commands instead.")
        if os.name == "nt" and any(":" in part for part in relative.parts):
            raise ToolError("Alternate data streams are not supported.")
        if must_exist and not path.exists():
            raise ToolError(f"{self.label(path)} does not exist.")
        return path

    def label(self, path):
        return path.relative_to(self.root).as_posix()


def read_file(ws, path, start_line=None, end_line=None):
    target = ws.resolve(path)
    label = ws.label(target)
    if target.is_dir():
        raise ToolError(f"{label} is a directory. Use list_files to see its contents.")
    if is_secret(target.name):
        raise ToolError(
            f"{label} may contain secrets, so the agent cannot read it. Ask the user for what you need."
        )
    size = target.stat().st_size
    if size > MAX_FILE_BYTES:
        raise ToolError(
            f"{label} is {size:,} bytes, too large to read. Use search_files to find the relevant lines."
        )
    data = target.read_bytes()
    if _binary(data):
        return f"{label} is a binary file ({size:,} bytes)."
    lines = data.decode("utf-8-sig", errors="replace").splitlines()
    total = len(lines)
    if not total:
        return f"{label} is empty."
    start = max(1, start_line or 1)
    if start > total:
        raise ToolError(f"{label} has only {total} lines.")
    last = start + MAX_READ_LINES - 1
    end = min(total, last, end_line if end_line and end_line >= start else last)
    output, used = [], 0
    for number in range(start, end + 1):
        line = lines[number - 1]
        if len(line) > 2000:
            line = line[:2000] + " …[line truncated]"
        entry = f"{number:>6}\t{line}"
        if output and used + len(entry) > MAX_READ_CHARS:
            end = number - 1
            break
        output.append(entry)
        used += len(entry) + 1
    more = f"\n[{total - end} more lines; continue with start_line={end + 1}]" if end < total else ""
    return f"{label} · lines {start}-{end} of {total}\n" + "\n".join(output) + more


def list_files(ws, path=None, depth=None):
    base = ws.resolve(path or ".")
    if base.is_file():
        return f"{ws.label(base)} is a file ({base.stat().st_size:,} bytes)."
    depth = max(1, min(depth or 2, 5))
    lines = []
    truncated = False

    def walk(directory, level):
        nonlocal truncated
        try:
            children = sorted(directory.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError as exc:
            lines.append("  " * level + f"[unreadable: {exc.strerror}]")
            return
        for child in children:
            if len(lines) >= MAX_LIST_ENTRIES:
                truncated = True
                return
            indent = "  " * level
            if child.is_symlink():
                lines.append(f"{indent}{child.name} (link, not followed)")
            elif child.is_dir():
                if child.name == ".git":
                    continue
                if child.name in SKIP_DIRS:
                    lines.append(f"{indent}{child.name}/ (skipped: dependencies or build output)")
                    continue
                lines.append(f"{indent}{child.name}/")
                if level + 1 < depth:
                    walk(child, level + 1)
            else:
                lines.append(f"{indent}{child.name}")

    walk(base, 0)
    if truncated:
        lines.append(f"[listing truncated at {MAX_LIST_ENTRIES} entries; list a subdirectory for more]")
    header = "./" if base == ws.root else ws.label(base) + "/"
    return header + "\n" + ("\n".join(lines) if lines else "(empty directory)")


def _walk_files(base):
    for directory, dirnames, filenames in os.walk(base, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            yield Path(directory) / name


def search_files(ws, pattern, path=None, glob=None, case_sensitive=None):
    try:
        regex = re.compile(pattern, 0 if case_sensitive else re.IGNORECASE)
    except re.error as exc:
        raise ToolError(f"Invalid regular expression: {exc}") from None
    base = ws.resolve(path or ".")
    files = [base] if base.is_file() else _walk_files(base)
    deadline = time.monotonic() + SEARCH_SECONDS
    matches, scanned, limit = [], 0, ""
    for file in files:
        label = ws.label(file)
        if glob and not (fnmatch.fnmatch(label, glob) or fnmatch.fnmatch(file.name, glob)):
            continue
        if is_secret(file.name) or file.is_symlink():
            continue
        try:
            size = file.stat().st_size
            if size > 1024 * 1024:
                continue
            data = file.read_bytes()
        except OSError:
            continue
        if _binary(data):
            continue
        scanned += size
        if scanned > SEARCH_BYTES or time.monotonic() > deadline:
            limit = "the search budget was reached; narrow the path or glob"
            break
        for number, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
            if regex.search(line):
                matches.append(f"{label}:{number}: {line.strip()[:300]}")
                if len(matches) >= MAX_MATCHES:
                    limit = f"only the first {MAX_MATCHES} matches are shown"
                    break
        if len(matches) >= MAX_MATCHES:
            break
    if not matches:
        return f"No matches for /{pattern}/" + (f" ({limit})." if limit else ".")
    note = f"\n[{limit}]" if limit else ""
    return f"{len(matches)} matches for /{pattern}/\n" + "\n".join(matches) + note


def search_code(ws, query, path=None, glob=None, max_results=None, selector=None):
    """Bounded, replayable lexical retrieval; the model can request full lines with read_file."""
    if not isinstance(query, str) or not query.strip():
        raise ToolError("A non-empty search query is required.")
    terms = list(dict.fromkeys(re.findall(r"[\w.-]+", query.casefold())))[:12]
    if not terms:
        raise ToolError("The query must contain searchable words.")
    if max_results is not None and (not isinstance(max_results, int) or isinstance(max_results, bool)):
        raise ToolError("max_results must be an integer.")
    limit = min(max(1, max_results or 10), CODE_SEARCH_MAX_RESULTS)
    base = ws.resolve(path or ".")
    if base.is_file() and is_secret(base.name):
        raise ToolError("The requested file may contain secrets and cannot be searched.")
    files = [base] if base.is_file() else _walk_files(base)
    deadline = time.monotonic() + SEARCH_SECONDS
    candidates = []
    scanned = 0
    incomplete = False
    for file in files:
        if file.is_symlink() or is_secret(file.name):
            continue
        label = ws.label(file)
        if glob and not (fnmatch.fnmatch(label, glob) or fnmatch.fnmatch(file.name, glob)):
            continue
        try:
            size = file.stat().st_size
            if size > MAX_FILE_BYTES:
                continue
            if scanned + size > SEARCH_BYTES or time.monotonic() > deadline:
                incomplete = True
                break
            data = file.read_bytes()
        except OSError:
            continue
        scanned += size
        if _binary(data):
            continue
        lines = data.decode("utf-8-sig", errors="replace").splitlines()
        basename = file.stem.casefold()
        path_text = label.casefold()
        digest = None
        for number, line in enumerate(lines, 1):
            lower = line.casefold()
            hits = sum(term in lower for term in terms)
            if not hits:
                continue
            score = (
                hits * 10
                + sum(term in basename for term in terms) * 3
                + sum(term in path_text for term in terms)
                + (15 if _DEFINITION.match(line) else 0)
            )
            digest = digest or hashlib.sha256(data).hexdigest()
            candidates.append((score, label, number, line.strip()[:180], digest))
    candidates.sort(key=lambda item: (-item[0], item[1], item[2]))
    selection = {"mode": "lexical", "cost_usd": 0, "input_tokens": 0}
    if selector and candidates:
        # The selector may reorder, never discard, lexical candidates. A failed
        # judgment cannot turn an existing match into a false negative.
        head = candidates[: min(8, len(candidates))]
        try:
            order, selection = selector(query, head)
            if sorted(order) != list(range(len(head))):
                raise ValueError("selector returned an invalid permutation")
            candidates[: len(head)] = [head[index] for index in order]
        except Exception as exc:
            selection = {
                "mode": "lexical_fallback",
                "reason": str(exc)[:160],
                "cost_usd": 0,
                "input_tokens": 0,
            }
    results = []
    for _score, label, number, preview, digest in candidates:
        entry = {
            "path": label,
            "start_line": number,
            "end_line": number,
            "line_count": 1,
            "source_sha256": digest,
            "preview": preview,
            "full_excerpt_included": False,
        }
        if (
            len(results) >= limit
            or len(json.dumps(results + [entry], ensure_ascii=False)) > CODE_SEARCH_MAX_CHARS
        ):
            incomplete = True
            break
        results.append(entry)
    return json.dumps(
        {
            "query": query,
            "results": results,
            "incomplete": incomplete,
            "scanned_bytes": scanned,
            "candidate_count": len(candidates),
            "selection": {
                "role": "evaluator_rerank_only",
                "contract": "Jev may reorder bounded lexical matches; it cannot discard matches or edit code.",
                **selection,
            },
            "next_step": "Use read_file with the returned path and line range before editing.",
        },
        ensure_ascii=False,
    )


@dataclass
class FileChange:
    path: Path
    label: str
    kind: str  # "add" or "update"
    before: bytes | None  # The exact bytes the proposal was computed from.
    after: bytes
    diff: str
    added: int
    removed: int


def _decode(label, raw):
    if _binary(raw):
        raise ToolError(f"{label} is a binary file; it cannot be edited as text.")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ToolError(f"{label} is not UTF-8 text, so it cannot be edited safely.") from None
    # Edits work on LF text; the file keeps its own line endings and byte-order mark.
    return text.replace("\r\n", "\n"), ("\r\n" if "\r\n" in text else "\n"), raw.startswith(codecs.BOM_UTF8)


def _encode(text, newline, bom):
    return (codecs.BOM_UTF8 if bom else b"") + text.replace("\n", newline).encode("utf-8")


def _diff(label, before, after, kind):
    lines = list(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            "/dev/null" if kind == "add" else f"a/{label}",
            f"b/{label}",
            n=3,
            lineterm="",
        )
    )
    added = sum(1 for line in lines if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in lines if line.startswith("-") and not line.startswith("---"))
    return "\n".join(lines), added, removed


def _change(ws, target, before_raw, before, after, newline, bom):
    label = ws.label(target)
    kind = "add" if before_raw is None else "update"
    data = _encode(after, newline, bom)
    if len(data) > MAX_WRITE_BYTES:
        raise ToolError(f"The new content of {label} exceeds {MAX_WRITE_BYTES:,} bytes.")
    diff, added, removed = _diff(label, before, after, kind)
    return FileChange(target, label, kind, before_raw, data, diff, added, removed)


def prepare_write(ws, path, content):
    if not isinstance(content, str):
        raise ToolError("content must be text.")
    target = ws.resolve(path, must_exist=False)
    label = ws.label(target)
    if target.is_dir():
        raise ToolError(f"{label} is a directory.")
    if is_secret(target.name):
        raise ToolError(f"{label} may hold secrets; the agent does not create or overwrite secret files.")
    after = content.replace("\r\n", "\n")
    if not target.exists():
        return _change(ws, target, None, "", after, "\n", False)
    raw = target.read_bytes()
    before, newline, bom = _decode(label, raw)
    if before == after:
        raise ToolError(f"{label} already has exactly this content.")
    return _change(ws, target, raw, before, after, newline, bom)


def prepare_edit(ws, path, old_text, new_text, replace_all=None):
    target = ws.resolve(path)
    label = ws.label(target)
    if target.is_dir():
        raise ToolError(f"{label} is a directory.")
    if is_secret(target.name):
        raise ToolError(f"{label} may contain secrets, so the agent cannot edit it.")
    if not isinstance(old_text, str) or not isinstance(new_text, str):
        raise ToolError("old_text and new_text must be text.")
    raw = target.read_bytes()
    before, newline, bom = _decode(label, raw)
    old, new = old_text.replace("\r\n", "\n"), new_text.replace("\r\n", "\n")
    if not old:
        raise ToolError("old_text must not be empty. Use write_file to create or replace a whole file.")
    if old == new:
        raise ToolError("old_text and new_text are identical, so nothing would change.")
    count = before.count(old)
    if count == 0:
        raise ToolError(
            f"old_text was not found in {label}. Read the file again and copy the exact text, including indentation."
        )
    if count > 1 and not replace_all:
        raise ToolError(
            f"old_text appears {count} times in {label}. Include more surrounding lines to make it unique, "
            "or set replace_all to true."
        )
    after = before.replace(old, new) if replace_all else before.replace(old, new, 1)
    return _change(ws, target, raw, before, after, newline, bom)


def apply_change(change):
    current = change.path.read_bytes() if change.path.exists() else None
    if current != change.before:
        raise ToolError(f"{change.label} changed after this edit was proposed. Read it again and retry.")
    change.path.parent.mkdir(parents=True, exist_ok=True)
    temporary = change.path.with_name(f".{change.path.name}.panini-{secrets.token_hex(4)}.tmp")
    try:
        temporary.write_bytes(change.after)
        if current is not None:
            shutil.copymode(change.path, temporary)
        os.replace(temporary, change.path)
    finally:
        temporary.unlink(missing_ok=True)


def _function(name, description, properties):
    return {
        "type": "function",
        "name": name,
        "description": description,
        "strict": True,
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": properties,
            "required": list(properties),
        },
    }


PLAN_TOOL = _function(
    "update_plan",
    "Share or revise a short step plan for multi-step work; mark exactly one step in_progress.",
    {
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "step": {"type": "string"},
                    "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]},
                },
                "required": ["step", "status"],
            },
        }
    },
)

TOOLS = [
    _function(
        "list_files",
        "List a workspace directory as an indented tree. Dependency and build folders are skipped.",
        {
            "path": {
                "type": ["string", "null"],
                "description": "Directory relative to the workspace; null for the root.",
            },
            "depth": {"type": ["integer", "null"], "description": "Levels to show, 1-5. Default 2."},
        },
    ),
    _function(
        "read_file",
        "Read a text file with line numbers, up to 1000 lines per call. Secret files such as .env are not readable.",
        {
            "path": {"type": "string", "description": "File path relative to the workspace."},
            "start_line": {
                "type": ["integer", "null"],
                "description": "First line to read (1-based). Default 1.",
            },
            "end_line": {
                "type": ["integer", "null"],
                "description": "Last line to read. Default: 1000 lines on.",
            },
        },
    ),
    _function(
        "search_files",
        "Search file contents with a Python regular expression. Returns path:line: text matches.",
        {
            "pattern": {"type": "string", "description": "Regular expression to search for."},
            "path": {
                "type": ["string", "null"],
                "description": "File or directory to search. Default: workspace.",
            },
            "glob": {
                "type": ["string", "null"],
                "description": "Only search matching files, e.g. *.py or src/*.ts.",
            },
            "case_sensitive": {"type": ["boolean", "null"], "description": "Default false."},
        },
    ),
    _function(
        "search_code",
        "Find relevant code locations with bounded previews, line numbers, SHA-256 source hashes, and an incomplete flag. Read full excerpts only when needed.",
        {
            "query": {"type": "string", "description": "Words or symbols describing the code to find."},
            "path": {
                "type": ["string", "null"],
                "description": "Optional workspace-relative file or directory.",
            },
            "glob": {"type": ["string", "null"], "description": "Optional filename pattern."},
            "max_results": {"type": ["integer", "null"], "description": "Maximum results, 1-20; default 10."},
        },
    ),
    _function(
        "write_file",
        "Create a file or replace its entire content. The user reviews the diff and must approve it. "
        "Prefer edit_file for changes to existing files.",
        {
            "path": {"type": "string", "description": "File path relative to the workspace."},
            "content": {"type": "string", "description": "The complete new file content."},
        },
    ),
    _function(
        "edit_file",
        "Replace exact text in an existing file. old_text must match the file exactly, including "
        "indentation, and be unique unless replace_all is true. The user reviews the diff and must approve it.",
        {
            "path": {"type": "string", "description": "File path relative to the workspace."},
            "old_text": {"type": "string", "description": "Exact existing text to replace."},
            "new_text": {"type": "string", "description": "Replacement text."},
            "replace_all": {
                "type": ["boolean", "null"],
                "description": "Replace every occurrence. Default false.",
            },
        },
    ),
    _function(
        "run_command",
        "Run one shell command in the workspace, non-interactively (no input). The user must approve it. "
        "Use it to build, test, run programs and use git. Do not start servers or other processes that never exit.",
        {
            "command": {"type": "string", "description": "The command line to run."},
            "timeout_seconds": {"type": ["integer", "null"], "description": "5-600 seconds. Default 120."},
            "scope": {"type": ["string", "null"], "enum": ["workspace", "host", None],
                      "description": "workspace uses the selected sandbox. host runs with your Windows user access, including network and installed tools; requires approval. Default workspace."},
        },
    ),
    _function(
        "apply_patch",
        "Change several files in one reviewable, all-or-nothing step. Envelope: '*** Begin Patch' ... "
        "'*** End Patch'. Per file: '*** Add File: path' then '+' lines; '*** Update File: path' then hunks "
        "('@@ optional anchor', then ' ' context, '-' removed, '+' added lines, with 3 lines of context); "
        "'*** Delete File: path'. Prefer it for multi-file or multi-hunk edits.",
        {"patch": {"type": "string", "description": "The whole patch text."}},
    ),
    _function(
        "terminal_start",
        "Start a long-running or interactive process (dev server, REPL, watcher, a program that asks for input) "
        "in its own session; the user must approve it. Returns the session id and its first output. Input and "
        "output are piped, not a real terminal: full-screen programs and hidden password prompts do not work.",
        {"command": {"type": "string", "description": "The command line to start."},
         "scope": {"type": ["string", "null"], "enum": ["workspace", "host", None],
                   "description": "Use host for persistent terminals when the native sandbox is selected. This grants the session your Windows user access and requires approval. Default workspace."}},
    ),
    _function(
        "terminal_send",
        "Send a line of input to a running session (the user must approve it) and return the output that follows.",
        {
            "session_id": {"type": "string"},
            "input": {"type": "string", "description": "Text to send; a newline is added."},
            "wait_seconds": {
                "type": ["number", "null"],
                "description": "How long to wait for output, 0-30. Default 2.",
            },
        },
    ),
    _function(
        "terminal_read",
        "Read new output from a session, waiting up to wait_seconds for it to settle.",
        {"session_id": {"type": "string"}, "wait_seconds": {"type": ["number", "null"]}},
    ),
    _function(
        "terminal_stop",
        "Stop a session and everything it started. Stop sessions you no longer need before finishing.",
        {"session_id": {"type": "string"}},
    ),
    PLAN_TOOL,
]

# The subset offered to small local models: fewer choices, no multi-file patch syntax, no terminals.
LOCAL_TOOLS = frozenset(
    {"list_files", "read_file", "search_files", "search_code", "edit_file", "write_file", "run_command", "update_plan"}
)
