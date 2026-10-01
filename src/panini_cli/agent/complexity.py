"""Offline task-complexity estimate: which tier of model does this request need?

It costs nothing and never fails, so it is both the router's fallback when Jev is
unreachable and a veto on Jev choosing the small local model for a hard task.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Words that mean "hard, wide, or risky". Each distinct hit adds two points.
HARD = (
    "refactor", "architect", "migrat", "redesign", "rewrite", "security", "vulnerab", "auth",
    "concurren", "race condition", "deadlock", "performance", "optimi", "root cause", "investigate",
    "debug", "regression", "flaky", "implement", "integrat", "design", "deploy",
    "release", "database", "schema", "sandbox", "whole repo", "entire", "across", "every file",
    "all files", "end-to-end", "from scratch", "cryptograph", "memory leak", "packag",
)
# Words that mean "small, mechanical, or a question". They never cancel a hard signal.
EASY = (
    "what is", "what's", "explain", "list", "show", "print", "rename", "typo", "format", "git status",
    "git log", "git diff", "how many", "which file", "where is", "summar", "count", "version",
    "hello", "thanks", "commit message",
)
# Ordinary engineering words: not hard by themselves, but too vague for the small local model.
MILD = (
    "fix", "bug", "error", "broken", "fails", "failing", "traceback", "exception", "crash",
    "test", "add ", "update", "change",
)


@dataclass(frozen=True)
class Complexity:
    points: int
    tier: str  # "local", "cheap", "balanced", "strong" or "frontier"
    signals: tuple[str, ...]

    @property
    def simple(self):
        return self.tier == "local"


def estimate(prompt: str, *, context_tokens: int = 0) -> Complexity:
    text = (prompt or "").casefold()
    signals = []
    points = 0
    hard = [word for word in HARD if word in text]
    if hard:
        points += 2 * len(hard)
        signals.append("hard:" + ",".join(hard[:4]))
    easy = [word for word in EASY if word in text]
    if easy and not hard:
        points -= 1
        signals.append("easy:" + ",".join(easy[:3]))
    mild = [word for word in MILD if word in text]
    if mild:
        points += 1
        signals.append("mild:" + ",".join(mild[:3]))
    size = len(text)
    if size > 1500:
        points += 3
        signals.append("long")
    elif size > 600:
        points += 1
        signals.append("lengthy")
    steps = len(re.findall(r"^\s*(?:\d+[.)]|[-*])\s", prompt or "", flags=re.MULTILINE))
    if steps >= 3:
        points += 1
        signals.append(f"{steps}-steps")
    if "```" in text:
        points += 1
        signals.append("code-block")
    files = set(re.findall(r"[\w./\\-]+\.(?:py|tsx?|jsx?|rs|go|java|cs|cpp|c|h|toml|json|ya?ml|md)\b", text))
    if len(files) >= 3:
        points += 1
        signals.append(f"{len(files)}-files")
    if context_tokens > 12_000:  # A long session is hard for a small model to keep straight.
        points += 2
        signals.append("long-context")
    if points <= 0 and size <= 300:
        tier = "local"
    elif points <= 1:
        tier = "cheap"
    elif points <= 3:
        tier = "balanced"
    elif points <= 6:
        tier = "strong"
    else:
        tier = "frontier"
    return Complexity(points, tier, tuple(signals))
