"""The coding runtime owns its loop; PANINI owns invocation, approval and verification.

The runtime is PANINI-CLI (panini_cli.agent) unless PANINI_CLI_EXECUTABLE names
another app-server-compatible executable, such as the Codex CLI.
"""

import asyncio
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .progress import EventJournal, LoopGuard, digest
from .protocol import AppServer, ProtocolError
from .verification import file_receipt, verify_files

BUILTIN = ["-m", "panini_cli.agent"]


def external_runtime():
    """An explicitly configured app-server-compatible executable, or None."""
    configured = os.getenv("PANINI_CLI_EXECUTABLE") or os.getenv("JEV_REV_PANINI_CLI_EXECUTABLE")
    if not configured:
        return None
    path = Path(configured).expanduser().resolve(strict=True)
    if not path.is_file() or (os.name == "nt" and path.suffix.lower() != ".exe"):
        raise ValueError(
            "PANINI_CLI_EXECUTABLE must name an app-server-compatible executable (.exe on Windows)"
        )
    return str(path)


def find_panini_cli():
    """The coding runtime's command line (without its app-server arguments)."""
    external = external_runtime()
    if external:
        return [external]
    if getattr(sys, "frozen", False):
        return [sys.executable]  # The standalone executable is already the CLI entry point.
    return [sys.executable, *BUILTIN]


def runtime_environment(command):
    """PANINI-CLI must import this package even when it is not pip-installed."""
    if command[1:] != BUILTIN:
        return None
    source = str(Path(__file__).resolve().parents[1])
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(filter(None, [source, environment.get("PYTHONPATH")]))
    return environment


@dataclass
class RunResult:
    status: str
    message: str
    thread_id: str = ""
    turn_id: str = ""
    commands: list = field(default_factory=list)
    changes: list = field(default_factory=list)
    verified: bool = False
    verification: list = field(default_factory=list)


class CodeEngine:
    def __init__(self, workspace, journal_path, *, executable=None, server_factory=AppServer):
        self.workspace = Path(workspace).resolve(strict=True)
        if not self.workspace.is_dir():
            raise ValueError("Workspace must be an existing directory")
        command = executable or find_panini_cli()
        self.command = [command] if isinstance(command, str) else list(command)
        self.executable = self.command[0]
        environment = runtime_environment(self.command)
        self.launch = {"env": environment} if environment else {}
        self.journal = EventJournal(Path(journal_path))
        self.server_factory = server_factory
        self.server = None
        self.thread_id = self.turn_id = ""
        self.stopped = False
        self.items = {}
        self.item_events = {}
        self.approved = set()
        self.every_action_approved = False
        self.closed = False
        self.turn_ready = asyncio.Event()

    async def doctor(self):
        server = self.server_factory(
            [*self.command, "app-server", "--stdio"], self.workspace, self._deny, **self.launch
        )
        try:
            info = await server.start()
            # No model request or command execution in this check.
            return {"protocol": "ready", "server": info, "executable": " ".join(self.command)}
        finally:
            await server.close()

    async def _deny(self, method, params):
        raise ProtocolError("No active coordinator for server request")

    async def run(
        self,
        run_id,
        instruction,
        *,
        confirm,
        emit,
        check,
        timeout=270,
        model=None,
        reasoning_effort=None,
        output_schema=None,
        postconditions=(),
    ):
        if self.server or self.closed:
            raise RuntimeError("Use one engine instance per task")
        if not instruction or len(instruction) > 20000:
            raise ValueError("Instruction must contain 1-20000 characters")
        if self.journal.replay(run_id, limit=1):
            raise ValueError("Run already exists; replay its events instead of executing it again")
        check()
        claimed = self.journal.append(
            run_id, "start", {"type": "run.started", "workspace": str(self.workspace)}
        )
        if not claimed:
            raise ValueError("Run was claimed by another executor; replay its events")
        guard = LoopGuard()
        result = RunResult("unknown_effect", "Execution did not return a terminal result.")
        unsupported = []

        async def request(method, params):
            await asyncio.wait_for(self.turn_ready.wait(), 5)
            check()
            if (
                self.stopped
                or params.get("threadId") != self.thread_id
                or params.get("turnId") != self.turn_id
            ):
                raise ProtocolError("Approval does not belong to the active turn")
            if method not in {
                "item/commandExecution/requestApproval",
                "item/fileChange/requestApproval",
            }:
                unsupported.append(method)
                await self.interrupt()
                raise ProtocolError("This server request requires the native PANINI-CLI interface")
            if params.get("grantRoot"):
                return {"decision": "decline"}  # No session-wide permission expansion.
            if method.endswith("fileChange/requestApproval") and params.get("itemId") not in self.items:
                try:
                    await asyncio.wait_for(
                        self.item_events.setdefault(params.get("itemId"), asyncio.Event()).wait(),
                        2,
                    )
                except TimeoutError:
                    return {"decision": "decline"}
            item = self.items.get(params.get("itemId"), {})
            review = {"method": method, "request": params, "item": item}
            if method.endswith("fileChange/requestApproval") and not item.get("changes"):
                return {"decision": "decline"}  # Must have a concrete patch to review.
            if method.endswith("commandExecution/requestApproval") and not params.get("command"):
                return {"decision": "decline"}
            accepted = await confirm(review)
            check()
            if self.stopped:
                return {"decision": "cancel"}
            decision = "accept" if accepted else "decline"
            if accepted:
                self.approved.add(params.get("itemId"))
            self.journal.append(
                run_id,
                "approval:" + digest(params),
                {
                    "type": "approval.decided",
                    "item_id": params.get("itemId"),
                    "decision": decision,
                },
            )
            return {"decision": decision}

        self.server = self.server_factory(
            [*self.command, "app-server", "--stdio"], self.workspace, request, **self.launch
        )
        try:
            async with asyncio.timeout(timeout):
                initialized = await self.server.start()
                panini_protocol = initialized.get("capabilities", {}).get("sessionPolicy") is True
                check()
                # Disable discovered external MCP servers in this managed thread. They
                # retain their native CLI path, but cannot bypass PANINI capability routing.
                config = (
                    await self.server.request(
                        "config/read",
                        {"includeLayers": False, "cwd": str(self.workspace)},
                    )
                ).get("config", {})
                overrides = {
                    "mcp_servers": {name: {"enabled": False} for name in (config.get("mcp_servers") or {})}
                }
                overrides.update(
                    {
                        "features.apps": False,
                        "features.panini_cli_apps_mcp_2026_07_28": False,
                        "features.enable_mcp_apps": False,
                        "features.plugins": False,
                        "features.browser_use": False,
                        "features.computer_use": False,
                        "sandbox_workspace_write.network_access": False,
                        "sandbox_workspace_write.writable_roots": [],
                    }
                )
                if reasoning_effort:
                    overrides["model_reasoning_effort"] = reasoning_effort
                thread_params = {
                        "cwd": str(self.workspace),
                        "approvalPolicy": "untrusted",
                        "approvalsReviewer": "user",
                        "sandbox": "workspace-write",
                        "ephemeral": True,
                        "config": overrides,
                        **({"model": model} if model else {}),
                        "developerInstructions": "Work only on the user's CLI task in the selected workspace. The host owns confirmations and cancellation. Never automate desktop input. Report commands, test results and limitations accurately. Do not claim that your own summary independently verifies the task.",
                    }
                if panini_protocol:
                    from .agent.config import Settings
                    settings = Settings.from_env()
                    thread_params = {key: thread_params[key] for key in ("cwd", "approvalPolicy", "developerInstructions")}
                    thread_params.update(sandbox=settings.sandbox, config={"mcp_enabled": False, "network_hosts": [],
                        **({"model_reasoning_effort": reasoning_effort} if reasoning_effort else {})})
                    if model:
                        thread_params["model"] = model
                started = await self.server.request("thread/start", thread_params)
                self.thread_id = started["thread"]["id"]
                await emit(
                    {
                        "type": "execution.started",
                        "model": started.get("model") or model or "Runtime default",
                    }
                )
                sandbox = started.get("sandbox", {})
                # Either an OS sandbox (workspace writes, no network), or no sandbox claim at
                # all and every command and file change confirmed by the user, which is then
                # enforced below: an unapproved action ends the run.
                os_sandbox = sandbox.get("type") == "workspaceWrite" and sandbox.get("networkAccess") is False
                self.every_action_approved = sandbox.get("type") == "confirmEveryAction"
                if panini_protocol:
                    os_sandbox = (sandbox.get("backend") == "mxc" and sandbox.get("tier") == "base-container"
                                  and sandbox.get("filesystemMode") == "workspace-write" and not sandbox.get("networkHosts"))
                    self.every_action_approved = started.get("approvalPolicy") == "untrusted" and sandbox.get("backend") == "none"
                if (
                    Path(started.get("cwd", "")).resolve() != self.workspace
                    or started.get("approvalPolicy") != "untrusted"
                    or started.get("approvalsReviewer") != "user"
                    or not (os_sandbox or self.every_action_approved)
                    or any(
                        not Path(root).resolve().is_relative_to(self.workspace)
                        for root in sandbox.get("writableRoots", [])
                    )
                ):
                    raise ProtocolError(
                        "Coding engine did not accept the requested workspace/approval/sandbox policy"
                    )
                check()
                params = {
                    "threadId": self.thread_id,
                    "input": [{"type": "text", "text": instruction, "text_elements": []}],
                }
                if output_schema is not None:
                    params["outputSchema"] = output_schema
                started_turn = await self.server.request("turn/start", params)
                self.turn_id = started_turn["turn"]["id"]
                self.turn_ready.set()
                result.thread_id, result.turn_id = self.thread_id, self.turn_id
                self.journal.append(
                    run_id,
                    "turn",
                    {
                        "type": "turn.started",
                        "thread_id": self.thread_id,
                        "turn_id": self.turn_id,
                    },
                )
                while True:
                    event = await self.server.next_event()
                    check()
                    name, params = event["method"], event.get("params", {})
                    if params.get("threadId") != self.thread_id:
                        continue
                    if params.get("turnId", self.turn_id) != self.turn_id:
                        continue
                    item = params.get("item", {})
                    if name == "item/started" and item.get("type") in {
                        "commandExecution",
                        "fileChange",
                    }:
                        await emit(
                            {
                                "type": "item.started",
                                "item_id": item["id"],
                                "kind": item["type"],
                                "command": item.get("command", ""),
                                "paths": [c.get("path") for c in item.get("changes", [])][:30],
                            }
                        )
                    elif name in {
                        "item/commandExecution/outputDelta",
                        "item/agentMessage/delta",
                    }:
                        await emit(
                            {
                                "type": "item.delta",
                                "item_id": params.get("itemId"),
                                "kind": "commandExecution" if "outputDelta" in name else "agentMessage",
                                "delta": str(params.get("delta", ""))[-4000:],
                            }
                        )
                    if name == "item/started" and item.get("type") in {
                        "commandExecution",
                        "fileChange",
                    }:
                        if len(self.items) >= 128:
                            raise ProtocolError("Too many active coding items")
                        self.items[item["id"]] = item
                        self.item_events.setdefault(item["id"], asyncio.Event()).set()
                    if name == "item/completed":
                        self.items.pop(item["id"], None)
                        self.item_events.pop(item["id"], None)
                        kind = item.get("type")
                        declined = item.get("status") == "declined"
                        if (
                            self.every_action_approved
                            and kind in {"commandExecution", "fileChange"}
                            and not declined
                            and item["id"] not in self.approved
                        ):
                            raise ProtocolError(
                                "The coding runtime reported an action the user did not approve"
                            )
                        record = {
                            "type": "item.completed",
                            "item_id": item["id"],
                            "kind": kind,
                            "status": item.get("status"),
                        }
                        if kind == "dynamicToolCall":
                            record["tool"] = str(item.get("tool", ""))[:100]
                            record["summary"] = str(item.get("summary") or item.get("tool", ""))[:300]
                        elif kind == "agentMessage":
                            result.message = item.get("text", "")[-12000:]
                            record["text"] = result.message
                        elif kind == "commandExecution":
                            receipt = {
                                "command": item.get("command", "")[:4000],
                                "cwd": item.get("cwd"),
                                "exit_code": item.get("exitCode"),
                                "output": (item.get("aggregatedOutput") or "")[-4000:],
                            }
                            record["receipt"] = receipt
                        elif kind == "fileChange":
                            record["paths"] = [c.get("path") for c in item.get("changes", [])][:100]
                        if not self.journal.append(run_id, "item:" + item["id"], record):
                            continue
                        if kind == "commandExecution" and not declined:
                            result.commands.append(receipt)
                        elif kind == "fileChange" and not declined:
                            result.changes.extend(record["paths"])
                            result.changes = result.changes[-100:]
                            readback = []
                            for path in record["paths"][:30]:
                                try:
                                    readback.append(file_receipt(self.workspace, path))
                                except (OSError, ValueError, TypeError):
                                    continue
                            if readback:
                                # The host reads bytes; model claims/exit codes do not reset the guard.
                                fingerprint = digest(readback)
                                guard.verified_progress(fingerprint)
                                self.journal.append(
                                    run_id,
                                    "readback:" + item["id"],
                                    {"type": "workspace.readback", "files": readback},
                                )
                        await emit(record)
                        if kind in {
                            "commandExecution",
                            "fileChange",
                            "mcpToolCall",
                            "dynamicToolCall",
                        }:
                            action = {
                                k: v
                                for k, v in item.items()
                                if k
                                in {
                                    "type",
                                    "command",
                                    "cwd",
                                    "changes",
                                    "server",
                                    "tool",
                                    "arguments",
                                }
                            }
                            observed = {
                                k: v
                                for k, v in item.items()
                                if k
                                in {
                                    "status",
                                    "exitCode",
                                    "aggregatedOutput",
                                    "error",
                                    "result",
                                }
                            }
                            reason = guard.observe(
                                action,
                                observed,
                                error_family={
                                    "type": kind,
                                    "exit_code": item.get("exitCode"),
                                    "error": item.get("error"),
                                }
                                if item.get("exitCode") not in (None, 0) or item.get("error")
                                else None,
                            )
                            if reason:
                                result.status, result.message = (
                                    "blocked",
                                    "Stopped coding execution: " + reason,
                                )
                                await self.interrupt()
                                break
                    elif name == "turn/completed":
                        turn = params["turn"]
                        if turn["id"] != self.turn_id:
                            continue
                        result.status = (
                            "partial"
                            if turn["status"] == "completed"
                            else "cancelled"
                            if turn["status"] == "interrupted"
                            else "failed"
                        )
                        if turn.get("error"):
                            result.message = str(turn["error"].get("message", "Coding turn failed"))[:2000]
                        if unsupported:
                            result.status, result.message = (
                                "blocked",
                                "Use native PANINI-CLI for this interaction: " + unsupported[0],
                            )
                        if result.status == "partial" and postconditions:
                            await self.stop()  # Background children cannot race the final host readback.
                            try:
                                result.verification = verify_files(self.workspace, postconditions)
                            except (OSError, ValueError):
                                result.verification = []
                            if result.verification:
                                result.verified, result.status = (
                                    True,
                                    "verified_complete",
                                )
                            else:
                                result.message += "\nThe caller's file postconditions did not pass."
                        break
        except asyncio.CancelledError:
            result.status, result.message = (
                "cancelled",
                "Stopped. Previously dispatched changes may remain.",
            )
            raise
        except TimeoutError:
            result.status, result.message = (
                "partial",
                "The coding time budget was reached. Previously dispatched changes may remain.",
            )
        except (OSError, ValueError, KeyError, TypeError, ProtocolError) as exc:
            result.status, result.message = "unknown_effect", str(exc)[:2000]
        finally:
            await self.stop()
            summary = asdict(result)
            summary["message"] = result.message[:2000]
            summary["commands"] = [
                {"command_sha256": digest(c["command"]), "exit_code": c["exit_code"]}
                for c in result.commands[-8:]
            ]
            summary["changes"] = [str(path)[:500] for path in result.changes[-10:]]
            self.journal.append(run_id, "end", {"type": "run.completed", "result": summary})
        return result

    async def interrupt(self):
        self.stopped = True
        if self.server and self.thread_id and self.turn_id:
            try:
                await self.server.request(
                    "turn/interrupt",
                    {"threadId": self.thread_id, "turnId": self.turn_id},
                    timeout=1,
                )
            except (OSError, TimeoutError, ProtocolError):
                return  # stop() still closes the Job even when cooperative interrupt fails.

    async def stop(self):
        await self.interrupt()
        if self.server:
            await self.server.close()

    async def close(self):
        if not self.closed:
            await self.stop()
            self.journal.close()
            self.closed = True
