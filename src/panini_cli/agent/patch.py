"""apply_patch: several file changes in one reviewable, all-or-nothing step.

Format (the same envelope Codex-style agents use):

    *** Begin Patch
    *** Add File: path/new.py
    +line
    *** Update File: path/old.py
    @@ optional anchor line
     context
    -removed
    +added
    *** Delete File: path/gone.py
    *** End Patch
"""

import secrets

from . import tools
from .tools import FileChange, ToolError


def parse(text):
    if not isinstance(text, str):
        raise ToolError("patch must be text.")
    lines = text.replace("\r\n", "\n").strip("\n").split("\n")
    if not lines or lines[0].strip() != "*** Begin Patch" or lines[-1].strip() != "*** End Patch":
        raise ToolError("A patch starts with '*** Begin Patch' and ends with '*** End Patch'.")
    operations, current = [], None
    for line in lines[1:-1]:
        if line.startswith("*** Add File: "):
            current = {"op": "add", "path": line[14:].strip(), "lines": []}
            operations.append(current)
        elif line.startswith("*** Update File: "):
            current = {"op": "update", "path": line[17:].strip(), "hunks": [[]]}
            operations.append(current)
        elif line.startswith("*** Delete File: "):
            current = {"op": "delete", "path": line[17:].strip()}
            operations.append(current)
        elif line.startswith("*** Move to: "):
            raise ToolError("Moving files is not supported; delete the old file and add the new one.")
        elif line.startswith("*** End of File"):
            continue
        elif current is None:
            raise ToolError(f"Unexpected line before any file header: {line[:80]}")
        elif current["op"] == "add":
            if not line.startswith("+"):
                raise ToolError(f"Lines of an added file start with '+': {line[:80]}")
            current["lines"].append(line[1:])
        elif current["op"] == "update":
            if line.startswith("@@"):
                if current["hunks"][-1]:
                    current["hunks"].append([])
                anchor = line[2:].strip()
                if anchor:
                    current["hunks"][-1].append(("@", anchor))
            elif line[:1] in (" ", "-", "+"):
                current["hunks"][-1].append((line[0], line[1:]))
            elif line == "":
                current["hunks"][-1].append((" ", ""))
            else:
                raise ToolError(f"Hunk lines start with ' ', '-' or '+': {line[:80]}")
        else:
            raise ToolError(f"A deleted file takes no content: {line[:80]}")
    if not operations:
        raise ToolError("The patch changes no files.")
    paths = [op["path"] for op in operations]
    if len(set(paths)) != len(paths):
        raise ToolError("Each file may appear only once in a patch.")
    return operations


def _find(lines, block, start):
    """Index of block in lines at or after start: exact first, then ignoring trailing spaces."""
    size = len(block)
    for normalise in (lambda s: s, lambda s: s.rstrip()):
        wanted = [normalise(b) for b in block]
        for index in range(start, len(lines) - size + 1):
            if [normalise(x) for x in lines[index : index + size]] == wanted:
                return index
    return -1


def _update(before, hunks, label):
    lines = before.split("\n")
    cursor = 0
    for hunk in hunks:
        if not hunk:
            continue
        anchor = [text for kind, text in hunk if kind == "@"]
        body = [(kind, text) for kind, text in hunk if kind != "@"]
        old = [text for kind, text in body if kind in (" ", "-")]
        new = [text for kind, text in body if kind in (" ", "+")]
        if anchor:
            key = anchor[0].strip()
            found = next((i for i in range(cursor, len(lines)) if lines[i].strip().startswith(key)), -1)
            if found < 0:
                raise ToolError(f"Anchor '{anchor[0][:60]}' was not found in {label}.")
            cursor = found
        if not old:
            raise ToolError(f"A hunk in {label} has no context; include a few unchanged lines.")
        index = _find(lines, old, cursor)
        if index < 0:
            raise ToolError(f"A hunk did not match {label}. Read the file again and copy the exact lines.")
        lines[index : index + len(old)] = new
        cursor = index + len(new)
    return "\n".join(lines)


def prepare(ws, text):
    """All changes are computed (and validated) before anything is written."""
    changes = []
    for op in parse(text):
        target = ws.resolve(op["path"], must_exist=op["op"] != "add")
        label = ws.label(target)
        if tools.is_secret(target.name):
            raise ToolError(f"{label} may hold secrets; the agent does not change secret files.")
        if target.is_dir():
            raise ToolError(f"{label} is a directory.")
        if op["op"] == "add":
            if target.exists():
                raise ToolError(f"{label} already exists; use Update File.")
            changes.append(tools.prepare_write(ws, op["path"], "\n".join(op["lines"]) + "\n"))
        elif op["op"] == "delete":
            raw = ws.read_bytes(target)
            before, _, _ = tools._decode(label, raw)
            diff, _, removed = tools._diff(label, before, "", "update")
            changes.append(FileChange(target, label, "delete", raw, None, diff, 0, removed, ws.root))
        else:
            raw = ws.read_bytes(target)
            before, newline, bom = tools._decode(label, raw)
            after = _update(before, op["hunks"], label)
            if after == before:
                raise ToolError(f"The patch does not change {label}.")
            changes.append(tools._change(ws, target, raw, before, after, newline, bom))
    return changes


def apply(changes):
    """Apply every change or none: on a failure the files already written are restored."""
    done = []
    try:
        for change in changes:
            ws = tools.Workspace(change.workspace) if change.workspace else None
            if ws:
                ws.resolve(str(change.path), must_exist=change.before is not None)
            current = (ws.read_bytes(change.path) if ws else change.path.read_bytes()) if change.path.exists() else None
            if current != change.before:
                raise ToolError(f"{change.label} changed after this patch was proposed. Read it again.")
        for change in changes:
            current = change.path.read_bytes() if change.path.exists() else None
            if current != change.before:
                raise ToolError(f"{change.label} changed after this patch was proposed. Read it again.")
            if change.after is None:
                change.path.unlink()
            else:
                tools.apply_change(change)
            done.append(change)
    except (ToolError, OSError):
        for change in reversed(done):
            restore(change.path, change.before)
        raise


def restore(path, content):
    """Put a file back to earlier bytes (None means the file did not exist)."""
    if content is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.panini-{secrets.token_hex(4)}.tmp")
    try:
        temporary.write_bytes(content)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
