"""Bounded child agents, with read-only exploration or independent Git worktrees."""

import asyncio
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import uuid4

from . import gitops, tools
from .model import create_model

MAX_WORKERS = 2


class WorkerHost:
    def __init__(self, parent, identifier, read_only):
        self.parent, self.identifier, self.read_only = parent, identifier, read_only
        self.mode = getattr(parent.host, "mode", "ask")

    async def started(self, item):
        pass

    async def delta(self, *args):
        pass

    async def completed(self, item):
        pass

    async def approve(self, item):
        if self.read_only or item.get("executionScope") == "host" or item.get("permissionRequest"):
            return "decline"
        return await self.parent.host.approve({**item, "workerId": self.identifier})


@dataclass
class Worker:
    id: str
    agent: object
    task: asyncio.Task
    mode: str
    workspace: Path
    result: object = None
    error: str = ""

    def info(self):
        return {"id": self.id, "mode": self.mode, "workspace": str(self.workspace),
                "status": self.result.status if self.result else ("failed" if self.error else "running"),
                "text": self.result.text[:16000] if self.result else "", "error": self.error,
                "usage": self.result.usage if self.result else {},
                "costUsd": self.result.cost_usd if self.result else None}


class Workers:
    def __init__(self, parent):
        self.parent, self.entries = parent, {}
        self.lock = asyncio.Lock()

    @property
    def reserved(self):
        return sum(w.agent.settings.budget_usd or 0 for w in self.entries.values() if not w.task.done())

    async def start(self, task, mode="read-only"):
        from .loop import Agent
        if not isinstance(task, str) or not task.strip() or len(task) > 32000:
            raise ValueError("Worker task must contain 1-32000 characters")
        if mode not in {"read-only", "worktree"}:
            raise ValueError("Worker mode must be read-only or worktree")
        async with self.lock:
            if len(self.entries) >= MAX_WORKERS:
                raise ValueError("At most two workers per session; close a completed worker first")
            if self.parent.settings.sandbox != "mxc":
                raise ValueError("Background workers require MXC isolation")
            if mode == "worktree" and self.parent.execution.read_only:
                raise ValueError("Read-only sessions cannot create editing workers")
            identifier = uuid4().hex
            workspace = self.parent.workspace.root
            if mode == "worktree":
                root = await asyncio.to_thread(gitops.repo_root, workspace)
                if root != workspace:
                    raise ValueError("Worktree workers require the workspace to be the Git repository root")
                # Fixed Git operations disable hooks and filters. A detached checkout
                # starts at HEAD; the parent's uncommitted files remain independent.
                base = (Path.home() / ".panini" / "worktrees").resolve()
                base.mkdir(parents=True, exist_ok=True)
                workspace = base / identifier / "project"
                if not workspace.resolve().is_relative_to(base):
                    raise ValueError("Invalid managed worktree path")
                await asyncio.to_thread(gitops._git, root, "worktree", "add", "--detach", str(workspace), "HEAD")
            remaining = None
            if self.parent.settings.budget_usd:
                remaining = max(0, self.parent.settings.budget_usd - self.parent.cost_usd - self.reserved)
                if remaining <= 0:
                    raise ValueError("Parent model budget exhausted")
                remaining /= MAX_WORKERS + 1  # Reserve capacity for the parent and both children.
            settings = replace(self.parent.settings, max_steps=min(24, self.parent.settings.max_steps),
                               budget_usd=remaining, local_preview=False, recoverable=False)
            host = WorkerHost(self.parent, identifier, mode == "read-only")
            child = Agent(workspace, create_model(settings), settings, host,
                          developer_instructions="You are a bounded background worker. Complete only the assigned task. "
                          "Report evidence and any limits. Editing worktrees start at HEAD; do not assume parent uncommitted changes.")
            child.is_scout = True  # Connectors are owned by the parent, not inherited by arbitrary children.
            child.allowed_tools = {t["name"] for t in tools.TOOLS if not t["name"].startswith(("worker_", "preview_", "terminal_"))}
            if mode == "read-only":
                child.allowed_tools &= {"read_file", "list_files", "search_files", "search_code", "skill_load", "update_plan"}
            worker = Worker(identifier, child, None, mode, workspace)
            self.entries[identifier] = worker
            worker.task = asyncio.create_task(self._run(worker, task))
            return worker.info()

    async def _run(self, worker, task):
        try:
            worker.result = await worker.agent.turn(task)
        except asyncio.CancelledError:
            worker.error = "interrupted"
        except Exception as exc:  # noqa: BLE001 -- preserve the parent on child/provider failures.
            worker.error = str(exc)[:500]
        finally:
            self.parent.cost_usd += worker.agent.cost_usd
            for key, value in worker.agent.usage.items():
                self.parent.usage[key] = self.parent.usage.get(key, 0) + value
            await worker.agent.close()

    def get(self, identifier):
        if identifier not in self.entries:
            raise ValueError("Unknown worker")
        return self.entries[identifier]

    def list(self):
        return [w.info() for w in self.entries.values()]

    async def diff(self, identifier):
        worker = self.get(identifier)
        if worker.mode != "worktree":
            raise ValueError("Only editing worktrees have a diff")
        diff = await asyncio.to_thread(gitops._git, worker.workspace, "diff", "HEAD", "--")
        untracked = await asyncio.to_thread(gitops._git, worker.workspace, "ls-files", "--others", "--exclude-standard")
        return (diff + "\nUntracked files (review their content before merging):\n" + untracked)[:100000]

    async def stop(self, identifier):
        worker = self.get(identifier)
        if not worker.task.done():
            worker.task.cancel()
        await asyncio.gather(worker.task, return_exceptions=True)
        result = worker.info()
        self.entries.pop(identifier)
        # Keep the checkout and all edits for review and recovery; no automatic merge or deletion.
        return result

    async def close(self):
        for identifier in list(self.entries):
            await self.stop(identifier)
