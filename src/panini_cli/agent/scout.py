"""Local scouts, cloud surgeon: read-only exploration delegated to the free local model.

A cloud model can call the `explore` tool with a question ("where is the retry logic and what
calls it?"). A sub-agent on the local Ollama model answers it with read-only tools only and
returns a short report with file:line references. The cloud model then reads just the lines it
needs, instead of paying to read whole files itself.
"""

from __future__ import annotations

import os
from dataclasses import replace

from . import config, tools

SCOUT_TOOLS = frozenset({"list_files", "read_file", "search_files", "search_code"})
SCOUT_STEPS = 12
SCOUT_CONTEXT = 24_000
REPORT_CHARS = 6_000

INSTRUCTIONS = (
    "You are a code scout working for another agent. Answer its question by searching and reading the "
    "workspace with your read-only tools. Reply with a short report: the relevant files and line numbers "
    "(path:line), what each does, and how they connect. Quote only short snippets. If you cannot find "
    "something, say so plainly; never guess."
)

EXPLORE_TOOL = tools._function(
    "explore",
    "Delegate read-only codebase exploration to a free local model (a scout). Ask one focused question, "
    "e.g. 'Where is retry logic implemented and what calls it?'. Returns a short report with path:line "
    "references; verify the lines you rely on with read_file before editing. Prefer this over reading many "
    "files yourself when you do not yet know where the relevant code is.",
    {
        "question": {"type": "string", "description": "What to find out."},
        "path": {"type": ["string", "null"], "description": "Optional subdirectory to focus on."},
    },
)


def enabled():
    return os.getenv("PANINI_CLI_EXPLORE", "1").lower() not in ("0", "false", "no", "off")


def available():
    if not (enabled() and config.local_enabled()):
        return False
    from . import ollama

    return ollama.has_model(config.local_model())


class _SilentHost:
    """The scout's work is summarized by the parent; it never asks for approval."""

    async def started(self, item):
        pass

    async def delta(self, *args):
        pass

    async def completed(self, item):
        pass

    async def approve(self, item):
        return "decline"


async def explore(workspace, settings, question, path=None):
    from .loop import Agent
    from .model import RoutedModel

    local = config.local_model()
    model = RoutedModel(local, escalate=False)
    scoped = replace(settings, model=local, max_steps=SCOUT_STEPS, context_tokens=SCOUT_CONTEXT, budget_usd=None)
    agent = Agent(workspace, model, scoped, _SilentHost(), developer_instructions=INSTRUCTIONS)
    agent.allowed_tools = SCOUT_TOOLS
    agent.is_scout = True
    task = question.strip() + (f"\n\nFocus on: {path}" if path else "")
    result = await agent.turn(task)
    if result.status != "completed" or not result.text.strip():
        return f"The local scout could not answer ({result.error or result.status}). Explore with your own tools."
    report = result.text.strip()[:REPORT_CHARS]
    return f"[Local scout report from {local} — unverified; check lines with read_file before relying on them]\n{report}"
