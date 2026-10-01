"""The agent loop: model responses, tool calls, approvals, interruption and a bounded context.

A host presents the work: the terminal CLI prints it and asks y/N; the app-server
forwards it to PANINI, whose coordinator asks the user. The loop never applies a
change or runs a command without the host's "accept".
"""

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Protocol
from uuid import uuid4

from . import codex_sandbox, compaction, gitops, intent, ledger, scout, verify, web, jev_selector, model_router, native_sandbox, patch, receipts, sandbox, tools
from .config import is_local as config_is_local
from .hooks import NoHooks
from .model import ModelError
from .prompt import instructions
from .shell import run_command
from .terminals import Terminals

DECLINED = "The user declined this action. Do not repeat it unchanged; adjust your approach or ask the user."
SKIPPED = "Not run: the user declined an earlier action in the same step."
INTERRUPTED = "Not completed: the user interrupted the task."
OMITTED = "[Earlier tool output removed to fit the context window.]"
KEEP_RECENT_OUTPUTS = 6
OMITTED_ARGUMENTS = '{"omitted": "Earlier tool arguments removed to fit the context window."}'
# Tools with no approval and no side effects: consecutive calls to them run concurrently.
READ_TOOLS = frozenset({"read_file", "list_files", "search_files", "search_code"})


class Host(Protocol):
    async def started(self, item: dict) -> None: ...
    async def delta(self, item_id: str, kind: str, text: str) -> None: ...
    async def completed(self, item: dict) -> None: ...
    async def approve(self, item: dict) -> str: ...  # "accept", "decline" or "cancel"


class Cancelled(Exception):
    """The host cancelled the whole turn at an approval prompt."""


@dataclass
class TurnResult:
    status: str  # completed, interrupted, failed or incomplete
    text: str = ""
    error: str = ""
    usage: dict = field(default_factory=dict)
    steps: int = 0
    cost_usd: float | None = None  # Priced cost of this turn; None when a model used was unpriced.


def _item_id(kind):
    return f"{kind}_{uuid4().hex[:12]}"


def _add_usage(total, usage):
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        total[key] = total.get(key, 0) + int(usage.get(key) or 0)


def _clip(text, head=4000, tail=6000):
    if len(text) <= head + tail:
        return text
    return f"{text[:head]}\n…[{len(text) - head - tail:,} characters omitted]…\n{text[-tail:]}"


def describe(tool, arguments):
    """A short, human-readable account of a read-only tool call."""
    path = arguments.get("path") or "."
    if tool == "read_file":
        start, end = arguments.get("start_line") or 1, arguments.get("end_line")
        span = "" if start == 1 and (not end or end >= 1000) else f" (lines {start}-{end or 'end'})"
        return f"Read {path}{span}"
    if tool == "list_files":
        return f"List {path}"
    if tool == "search_files":
        glob = f" [{arguments['glob']}]" if arguments.get("glob") else ""
        return f"Search /{arguments.get('pattern', '')}/ in {path}{glob}"
    if tool == "search_code":
        return f"Find code for {arguments.get('query', '')[:80]} in {path}"
    return tool


IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}
MAX_IMAGE_BYTES = 10 * 1024 * 1024


def image_part(path):
    """An image attached by the user, as a Responses `input_image` part (a data URL)."""
    import base64
    from pathlib import Path

    file = Path(path).expanduser()
    kind = IMAGE_TYPES.get(file.suffix.lower())
    if not kind:
        raise ValueError(f"{file} is not a supported image ({', '.join(IMAGE_TYPES)}).")
    data = file.read_bytes()
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError(f"{file} is larger than 10 MB.")
    return {"type": "input_image", "image_url": f"data:{kind};base64,{base64.b64encode(data).decode()}"}


def has_image(content):
    return isinstance(content, list) and any(
        isinstance(part, dict) and part.get("type") == "input_image" for part in content
    )


def text_of(content):
    if isinstance(content, list):
        return " ".join(part.get("text", "") for part in content if isinstance(part, dict))
    return str(content or "")


def command_scope(arguments):
    scope = arguments.get("scope") or "workspace"
    if scope not in ("workspace", "host"):
        raise tools.ToolError("scope must be workspace or host.")
    return scope


class Agent:
    def __init__(self, workspace, model, settings, host, *, developer_instructions="", hooks=None):
        self.workspace = tools.Workspace(workspace)
        self.model, self.settings, self.host = model, settings, host
        self.instructions = instructions(
            self.workspace.root, extra=developer_instructions, sandbox=settings.sandbox
        ) + verify.instructions(self.workspace.root)
        self.allowed_tools = None  # A set of tool names restricts this agent (the local scout uses it).
        self.is_scout = False
        self.extra_tools = []  # Tools from MCP servers.
        self.mcp = None
        self.checkpoints = []  # [(start sha, end sha)] per turn, newest last, when in a git repository.
        self.session_base = None
        self._needs_verify = False
        self._changed_this_turn = []
        self.items = []
        self.plan = []
        self.hooks = hooks or NoHooks()
        self.terminals = Terminals(self.workspace.root, settings)
        self.started_session = False
        self._routed_user = -2  # Index of the user message the model was last routed for.
        # Applied file changes per turn, newest last: [(path, label, before, after)].
        self.changes = []
        self.cache_key = f"panini-cli-{uuid4().hex}"
        self.usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        self.last_input_tokens = 0  # As reported by the provider for the latest request.
        self.cost_usd = 0.0  # Priced cloud spend this session.
        self.unpriced_calls = 0
        self._budget_warned = False
        self.session_id = uuid4().hex
        self._receipt = None
        self._receipt_id = None
        self._active_call_id = None
        self._active_tool = None
        self._active_sandbox = None

    def _receipt_log(self):
        if self._receipt_id != self.session_id:
            self._receipt = receipts.ReceiptLog(self.session_id, self.workspace.root)
            self._receipt_id = self.session_id
        return self._receipt

    async def _checkpoint_start(self):
        self._turn_start = None
        if self.is_scout or not gitops.enabled():
            return
        try:
            self._turn_start = await asyncio.to_thread(gitops.snapshot, self.workspace.root)
        except (gitops.GitError, OSError, ValueError):
            self._turn_start = None
        if self._turn_start and self.session_base is None:
            self.session_base = self._turn_start
            await asyncio.to_thread(gitops.snapshot, self.workspace.root, f"sessions/{self.session_id}/base")

    async def _checkpoint_end(self):
        start = getattr(self, "_turn_start", None)
        if not start:
            return
        try:
            end = await asyncio.to_thread(gitops.snapshot, self.workspace.root)
        except (gitops.GitError, OSError, ValueError):
            return
        if end != start:
            self.checkpoints.append((start, end))

    def session_diff(self):
        """Everything that changed in the workspace since this session's first turn."""
        if not self.session_base:
            return ""
        return gitops.diff(self.workspace.root, self.session_base)

    async def _account(self, model_id, usage, turn_cost):
        spent = ledger.cost(model_id, usage)
        ledger.record(
            "call",
            session=self.session_id,
            workspace=str(self.workspace.root),
            model=model_id,
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            cost_usd=spent,
        )
        if spent is None:
            turn_cost[1] = False
            self.unpriced_calls += 1
            if self.settings.budget_usd and not self._budget_warned and hasattr(self.host, "hook_note"):
                self._budget_warned = True
                await self.host.hook_note(
                    f"{model_id} has no price in the cost table, so the budget cannot count it. "
                    "Add it under [prices] in ~/.panini/config.toml."
                )
            return
        turn_cost[0] += spent
        self.cost_usd += spent

    def _turn_record(self, status, first_model, steps, turn_cost):
        final = getattr(self.model, "model", "scripted")
        ledger.record(
            "turn",
            session=self.session_id,
            workspace=str(self.workspace.root),
            model=first_model,
            final_model=final,
            escalated=bool(first_model and config_is_local(first_model) and not config_is_local(final)),
            status=status,
            steps=steps,
            cost_usd=turn_cost[0] if turn_cost[1] else None,
        )

    async def compact(self, reason="manual"):
        """Replace the history with a summary. Returns (tokens before, tokens after, summarizer)."""
        if not self.items:
            return None
        before = compaction.estimate_tokens(self.items, self.last_input_tokens)
        summary, by = await compaction.summarize(self.items, self.model)
        if not summary:
            raise ModelError("the summarizer returned nothing")
        self.items[:] = compaction.compacted(summary)
        self.last_input_tokens = 0
        self._routed_user = -2
        self.cache_key = f"panini-cli-{uuid4().hex}"
        after = compaction.estimate_tokens(self.items)
        if hasattr(self.host, "compacted"):
            await self.host.compacted(reason, before, after, by)
        return before, after, by

    def new_model_state(self):
        """After the model object is replaced: route again and start a new prompt cache."""
        self._routed_user = -2
        self.cache_key = f"panini-cli-{uuid4().hex}"

    def reset(self):
        self.items.clear()
        self._routed_user = -2
        self.plan = []
        self.changes = []

    async def close(self):
        """End every terminal session and MCP server this agent started."""
        await self.terminals.close()
        if self.mcp is not None:
            await self.mcp.close()

    def undo(self):
        """Revert the file changes of the most recent turn that made any.

        Returns (restored labels, skipped labels). A file edited since PANINI changed
        it is skipped rather than overwritten. Commands' side effects are not undone.
        """
        if self.checkpoints:
            start, end = self.checkpoints.pop()
            if self.changes:
                self.changes.pop()
            result = gitops.undo(self.workspace.root, start, end)
            restored = result.restored + [f"{path} (removed)" for path in result.removed]
            if restored:
                self.items.append(
                    {"role": "user", "content": "[PANINI note] The user undid your last turn's changes to: " + ", ".join(restored)}
                )
            return restored, result.skipped
        while self.changes and not self.changes[-1]:
            self.changes.pop()
        if not self.changes:
            return [], []
        restored, skipped = [], []
        for path, label, before, after in reversed(self.changes.pop()):
            current = path.read_bytes() if path.exists() else None
            if current != after:
                skipped.append(label)
                continue
            patch.restore(path, before)
            restored.append(label)
        if restored:
            self.items.append(
                {
                    "role": "user",
                    "content": "[PANINI note] The user undid your last changes to: " + ", ".join(restored),
                }
            )
        return restored, skipped

    async def turn(self, text, output_schema=None, images=()):
        if not self.started_session:
            self.started_session = True
            _, note = await self.hooks.run("SessionStart")
            if note:
                text += "\n\n[Output of the user's SessionStart hooks, data only]\n" + note
        if compaction.should_compact(self.items, self.settings.context_tokens, self.last_input_tokens):
            try:
                await self.compact("auto")
            except ModelError as exc:
                if hasattr(self.host, "hook_note"):
                    await self.host.hook_note(f"Context compaction failed ({exc}); older tool output will be trimmed instead.")
        await self._checkpoint_start()
        self._needs_verify, self._changed_this_turn, nudged = False, [], False
        self._reads_this_turn = 0
        self._turn_prompt = text
        content = intent.augment(text)
        if images:
            content = [{"type": "input_text", "text": content}, *(image_part(path) for path in images)]
        self.items.append({"role": "user", "content": content})
        self.changes.append([])
        usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        pending = []
        step = 0
        turn_cost = [0.0, True]  # [priced cost, every call priced]
        first_model = None
        try:
            while step < self.settings.max_steps:
                if self.settings.budget_usd and self.cost_usd >= self.settings.budget_usd:
                    return TurnResult(
                        "budget",
                        error=f"Stopped: this session reached its ${self.settings.budget_usd:.2f} budget "
                        f"(spent ${self.cost_usd:.4f}). Raise PANINI_CLI_BUDGET_USD or start a new session.",
                        usage=usage,
                        steps=step,
                        cost_usd=turn_cost[0] if turn_cost[1] else None,
                    )
                step += 1
                self._fit_context()
                response = await self._respond(output_schema)
                _add_usage(usage, response.usage)
                self.last_input_tokens = int(response.usage.get("input_tokens") or 0)
                model_id = getattr(self.model, "model", "scripted")
                first_model = first_model or model_id
                await self._account(model_id, response.usage, turn_cost)
                self.items.extend(response.items)
                if not response.calls and self._ungrounded_local_answer():
                    del self.items[-len(response.items):]
                    if self.model.force_escalate("answered about a file it did not read"):
                        continue
                if not response.calls and self._claims_unmade_changes(response.text):
                    # The local model reported edits that never happened: discard that answer and
                    # let the cloud model redo the request from the same history.
                    del self.items[-len(response.items):]
                    if self.model.force_escalate("claimed changes it did not make"):
                        continue
                if not response.calls and self._needs_verify and not nudged and verify.enabled():
                    found = verify.commands(self.workspace.root)
                    if found:
                        nudged = True
                        self.items.append({"role": "developer", "content": verify.reminder(self._changed_this_turn, found)})
                        continue
                if not response.calls:
                    _, note = await self.hooks.run("TurnEnd", result=response.text)
                    if note and hasattr(self.host, "hook_note"):
                        await self.host.hook_note(note)
                    self._turn_record("completed", first_model, step, turn_cost)
                    return TurnResult(
                        "completed", response.text, usage=usage, steps=step,
                        cost_usd=turn_cost[0] if turn_cost[1] else None,
                    )
                pending = [call.call_id for call in response.calls]
                declined = False
                calls = list(response.calls)
                while calls:
                    batch = [calls.pop(0)]
                    if batch[0].name in READ_TOOLS:
                        while calls and calls[0].name in READ_TOOLS:
                            batch.append(calls.pop(0))
                    if declined:
                        results = [(SKIPPED, True)] * len(batch)
                    elif len(batch) == 1:
                        results = [await self._tool(batch[0])]
                    else:
                        results = await asyncio.gather(*(self._tool(call) for call in batch))
                    for call, (output, was_declined) in zip(batch, results, strict=True):
                        declined = declined or was_declined
                        self.items.append(
                            {"type": "function_call_output", "call_id": call.call_id, "output": output}
                        )
                        pending.remove(call.call_id)
            self._turn_record("incomplete", first_model, step, turn_cost)
            return TurnResult(
                "incomplete",
                error=f"Stopped after {self.settings.max_steps} steps without finishing. Ask to continue.",
                usage=usage,
                steps=step,
                cost_usd=turn_cost[0] if turn_cost[1] else None,
            )
        except asyncio.CancelledError:
            self._close_pending(pending)
            raise
        except Cancelled:
            self._close_pending(pending)
            return TurnResult("interrupted", error="The task was cancelled.", usage=usage, steps=step)
        except ModelError as exc:
            self._close_pending(pending)
            self._turn_record("failed", first_model, step, turn_cost)
            return TurnResult("failed", error=str(exc), usage=usage, steps=step)
        finally:
            _add_usage(self.usage, usage)
            await self._checkpoint_end()

    async def _respond(self, output_schema):
        await self._route_model()
        message = {}

        async def on_text(_provider_item, delta):
            if not message:
                message.update(id=_item_id("message"), type="agentMessage", text="", status="inProgress")
                await self.host.started(dict(message))
            await self.host.delta(message["id"], "agentMessage", delta)

        response = await self.model.respond(
            self.instructions,
            self.items,
            self.tool_defs(),
            on_text=on_text,
            output_schema=output_schema,
            cache_key=self.cache_key,
        )
        if response.text and not message:
            message.update(id=_item_id("message"), type="agentMessage", text="", status="inProgress")
            await self.host.started(dict(message))
        if message:
            await self.host.completed({**message, "text": response.text, "status": "completed"})
        return response

    def tool_defs(self):
        defs = list(tools.TOOLS) + [web.WEB_FETCH_TOOL] + list(self.extra_tools)
        current = str(getattr(self.model, "model", ""))
        if web.search_enabled() and current and not config_is_local(current):
            defs.append(web.HOSTED_WEB_SEARCH)
        if (
            not self.is_scout
            and model_router.enabled(self.settings.model)
            and not config_is_local(current)
            and scout.available()
        ):
            defs.append(scout.EXPLORE_TOOL)
        if self.allowed_tools is not None:
            defs = [d for d in defs if d.get("name") in self.allowed_tools]
        return defs

    async def _route_model(self):
        if not model_router.enabled(self.settings.model) or not hasattr(self.model, "switch_model"):
            return
        # Route once per user message. Tool-result steps stay on the chosen model so a task never
        # bounces between backends mid-flight; the only mid-task move is a local -> cloud escalation.
        last_user = max(
            (index for index, item in enumerate(self.items) if isinstance(item, dict) and item.get("role") == "user"),
            default=-1,
        )
        escalation = getattr(self.model, "escalation", None)
        if escalation:
            self.model.escalation = None
            self.cache_key = f"panini-cli-{uuid4().hex}"
            if hasattr(self.host, "model_route"):
                reason = f"escalated: {escalation}"
                await self.host.model_route(model_router.RouteDecision(self.model.model, reason, True))
        if last_user == self._routed_user:
            return
        self._routed_user = last_user
        content = self.items[last_user].get("content", "") if last_user >= 0 else ""
        prompt = text_of(content)
        current = getattr(self.model, "model", model_router.fallback_model())
        context_tokens = max(0, sum(len(json.dumps(item, ensure_ascii=False)) for item in self.items) // 4)
        allow_local = not getattr(self.model, "local_blocked", False) and not has_image(content)
        try:
            decision = await asyncio.to_thread(
                model_router.route, prompt, current, context_tokens, allow_local=allow_local
            )
        except Exception as exc:  # noqa: BLE001 -- model routing must never block the CLI.
            decision = model_router.RouteDecision(current, f"router-error:{str(exc)[:120]}", False)
        if decision.changed:
            self.model.switch_model(decision.model)
            self.cache_key = f"panini-cli-{uuid4().hex}"
        if hasattr(self.host, "model_route"):
            await self.host.model_route(decision)

    def _close_pending(self, pending):
        # Every function call needs an output, or the next request would be rejected.
        for call_id in pending:
            self.items.append({"type": "function_call_output", "call_id": call_id, "output": INTERRUPTED})
        pending.clear()

    def _fit_context(self):
        budget = self.settings.context_tokens * 4  # Roughly four characters per token.
        size = sum(len(json.dumps(item, ensure_ascii=False)) for item in self.items)
        if size <= budget:
            return
        outputs = [item for item in self.items if item.get("type") == "function_call_output"]
        for item in outputs[:-KEEP_RECENT_OUTPUTS]:
            if item["output"] != OMITTED:
                size -= len(item["output"]) - len(OMITTED)
                item["output"] = OMITTED
                if size <= budget:
                    return
        calls = [item for item in self.items if item.get("type") == "function_call"]
        for item in calls[:-KEEP_RECENT_OUTPUTS]:
            arguments = item.get("arguments") or ""
            if len(arguments) > 2000:
                size -= len(arguments) - len(OMITTED_ARGUMENTS)
                item["arguments"] = OMITTED_ARGUMENTS
                if size <= budget:
                    return

    async def _tool(self, call):
        """Returns (output for the model, whether the user declined), with hooks around it."""
        receipt = self._receipt_log()
        try:
            proposed = json.loads(call.arguments or "{}")
        except ValueError:
            proposed = {}
        scope = proposed.get("scope") if isinstance(proposed, dict) else None
        effective_sandbox = (
            "host" if call.name in ("run_command", "terminal_start") and scope == "host"
            else self.settings.sandbox
        )
        if call.name in ("run_command", "terminal_start") and effective_sandbox == "none":
            effective_sandbox = "host"
        if call.name in ("terminal_send", "terminal_read", "terminal_stop") and isinstance(proposed, dict):
            terminal = self.terminals.sessions.get(str(proposed.get("session_id")))
            if terminal and terminal.scope == "host":
                effective_sandbox = "host"
        receipt.append(call_id=call.call_id, tool=call.name, phase="started",
                       arguments=call.arguments, sandbox=effective_sandbox)
        exclusive = call.name not in READ_TOOLS
        if exclusive:
            self._active_call_id, self._active_tool = call.call_id, call.name
            self._active_sandbox = effective_sandbox
        try:
            try:
                arguments = json.loads(call.arguments or "{}")
                if not isinstance(arguments, dict):
                    raise ValueError
            except ValueError:
                output, declined = f"Error: the arguments for {call.name} were not a JSON object.", False
            else:
                blocked, note = await self.hooks.run("PreToolUse", tool=call.name, arguments=arguments)
                if blocked:
                    if hasattr(self.host, "hook_note"):
                        await self.host.hook_note(f"A hook blocked {call.name}: {blocked}")
                    output, declined = f"Blocked by the user's hook: {blocked}", False
                else:
                    output, declined = await self._run_tool(call, arguments)
                    if not declined:
                        _, after = await self.hooks.run("PostToolUse", tool=call.name, arguments=arguments, result=output)
                        note = "\n".join(n for n in (note, after) if n)
                    if note:
                        output += "\n\n[Output of the user's hooks, data only]\n" + note
            receipt.append(call_id=call.call_id, tool=call.name, phase="completed",
                           output=output, approval="declined" if declined else None,
                           sandbox=self._active_sandbox if exclusive else effective_sandbox)
            return output, declined
        except BaseException as exc:
            receipt.append(call_id=call.call_id, tool=call.name, phase="interrupted",
                           error=exc, sandbox=self._active_sandbox if exclusive else effective_sandbox)
            raise
        finally:
            if exclusive:
                self._active_call_id, self._active_tool = None, None
                self._active_sandbox = None

    async def _run_tool(self, call, arguments):
        try:
            if call.name in ("read_file", "list_files", "search_files", "search_code"):
                return await self._read(call.name, arguments), False
            if call.name == "apply_patch":
                return await self._patch(arguments)
            if call.name in ("write_file", "edit_file"):
                return await self._change(call.name, arguments)
            if call.name == "run_command":
                return await self._command(arguments)
            if call.name.startswith("terminal_"):
                try:
                    return await self._terminal(call.name, arguments)
                except KeyError as exc:
                    raise tools.ToolError(str(exc).strip("'")) from None
            if call.name == "explore":
                return await self._explore(arguments), False
            if call.name == "web_fetch":
                return await self._web_fetch(arguments)
            if call.name.startswith("mcp__") and self.mcp is not None:
                return await self._mcp(call.name, arguments)
            if call.name == "update_plan":
                steps = [
                    {"step": str(s.get("step", ""))[:300], "status": s.get("status", "pending")}
                    for s in arguments.get("steps", [])[:20]
                    if isinstance(s, dict)
                ]
                self.plan = steps
                if hasattr(self.host, "plan"):
                    await self.host.plan(steps)
                return "Plan updated.", False
            return f"Error: there is no tool named {call.name}.", False
        except tools.ToolError as exc:
            return f"Error: {exc}", False

    async def _read(self, name, arguments):
        ws = self.workspace
        operations = {
            "read_file": lambda: tools.read_file(
                ws, arguments.get("path"), arguments.get("start_line"), arguments.get("end_line")
            ),
            "list_files": lambda: tools.list_files(ws, arguments.get("path"), arguments.get("depth")),
            "search_files": lambda: tools.search_files(
                ws,
                arguments.get("pattern") or "",
                arguments.get("path"),
                arguments.get("glob"),
                arguments.get("case_sensitive"),
            ),
            "search_code": lambda: tools.search_code(
                ws,
                arguments.get("query") or "",
                arguments.get("path"),
                arguments.get("glob"),
                arguments.get("max_results"),
                selector=jev_selector.select if jev_selector.enabled() else None,
            ),
        }
        item = {
            "id": _item_id("tool"),
            "type": "dynamicToolCall",
            "tool": name,
            "arguments": arguments,
            "summary": describe(name, arguments),
            "status": "inProgress",
        }
        await self.host.started(item)
        self._reads_this_turn = getattr(self, "_reads_this_turn", 0) + 1
        try:
            result = await asyncio.to_thread(operations[name])
        except (tools.ToolError, OSError) as exc:
            await self.host.completed({**item, "status": "failed", "error": str(exc)})
            raise tools.ToolError(str(exc)) from None
        await self.host.completed({**item, "status": "completed", "result": result.splitlines()[0][:300]})
        return result

    async def _explore(self, arguments):
        question = arguments.get("question")
        if not isinstance(question, str) or not question.strip():
            raise tools.ToolError("question must be a non-empty string.")
        item = {
            "id": _item_id("tool"),
            "type": "dynamicToolCall",
            "tool": "explore",
            "arguments": arguments,
            "summary": f"Explore with local scout: {question[:80]}",
            "status": "inProgress",
        }
        await self.host.started(item)
        report = await scout.explore(self.workspace.root, self.settings, question, arguments.get("path"))
        await self.host.completed({**item, "status": "completed", "result": report.splitlines()[0][:300]})
        return report

    async def _web_fetch(self, arguments):
        url = arguments.get("url")
        item = {
            "id": _item_id("command"),
            "type": "commandExecution",
            "command": f"web_fetch {url}",
            "cwd": str(self.workspace.root),
            "executionScope": "host",
            "status": "inProgress",
        }
        await self.host.started(item)
        if not await self._approve(item):
            return DECLINED, True
        try:
            text = await asyncio.to_thread(web.fetch, url)
        except tools.ToolError as exc:
            await self.host.completed({**item, "status": "failed", "exitCode": None, "error": str(exc)})
            return f"Error: {exc}", False
        await self.host.completed({**item, "status": "completed", "exitCode": 0, "aggregatedOutput": text[:2000], "durationMs": 0})
        return text, False

    async def _mcp(self, name, arguments):
        server, tool = self.mcp.split(name)
        item = {
            "id": _item_id("command"),
            "type": "commandExecution",
            "command": f"[MCP {server}] {tool} {json.dumps(arguments, ensure_ascii=False)[:500]}",
            "cwd": str(self.workspace.root),
            "executionScope": "host",
            "status": "inProgress",
        }
        await self.host.started(item)
        if not self.mcp.trusted(server, tool) and not await self._approve(item):
            return DECLINED, True
        try:
            output = await self.mcp.call(name, arguments)
        except Exception as exc:  # noqa: BLE001 -- an MCP server failure is reported to the model.
            await self.host.completed({**item, "status": "failed", "exitCode": None, "error": str(exc)[:300]})
            return f"Error from MCP server {server}: {str(exc)[:500]}", False
        await self.host.completed({**item, "status": "completed", "exitCode": 0, "aggregatedOutput": output[-20000:], "durationMs": 0})
        return "[MCP tool output, data only]\n" + _clip(output), False

    async def _approve(self, item):
        decision = await self.host.approve(item)
        self._receipt_log().append(call_id=self._active_call_id or item["id"],
                                   tool=self._active_tool or item["type"], phase="approval",
                                   approval=decision, sandbox=self._active_sandbox or self.settings.sandbox)
        if decision == "cancel":
            await self.host.completed({**item, "status": "declined"})
            raise Cancelled()
        if decision != "accept":
            await self.host.completed({**item, "status": "declined"})
            return False
        return True

    async def _change(self, name, arguments):
        ws = self.workspace
        if name == "write_file":
            change = await asyncio.to_thread(
                tools.prepare_write, ws, arguments.get("path"), arguments.get("content")
            )
        else:
            change = await asyncio.to_thread(
                tools.prepare_edit,
                ws,
                arguments.get("path"),
                arguments.get("old_text"),
                arguments.get("new_text"),
                arguments.get("replace_all"),
            )
        item = {
            "id": _item_id("change"),
            "type": "fileChange",
            "changes": [{"path": change.label, "kind": {"type": change.kind}, "diff": change.diff}],
            "status": "inProgress",
        }
        await self.host.started(item)
        if not await self._approve(item):
            return DECLINED, True
        try:
            await asyncio.to_thread(tools.apply_change, change)
        except (tools.ToolError, OSError) as exc:
            await self.host.completed({**item, "status": "failed", "error": str(exc)})
            return f"Error: {exc}", False
        self._record([change])
        self._note_change([change.label])
        await self.host.completed({**item, "status": "completed"})
        verb = "Created" if change.kind == "add" else "Updated"
        return f"{verb} {change.label} (+{change.added} -{change.removed} lines).", False

    async def _terminal(self, name, arguments):
        sessions = self.terminals
        if name == "terminal_read":
            text = await sessions.read(str(arguments.get("session_id")), arguments.get("wait_seconds") or 2)
            terminal = sessions.get(str(arguments.get("session_id")))
            state = "running" if terminal.running else f"ended with exit code {terminal.process.returncode}"
            return f"Session {terminal.id} ({state}). New output:\n{text or '(none)'}", False
        if name == "terminal_stop":
            code = await sessions.stop(str(arguments.get("session_id")))
            return f"Stopped (exit code {code}).", False
        if name == "terminal_start":
            command = arguments.get("command")
            if not isinstance(command, str) or not command.strip():
                raise tools.ToolError("command must be a non-empty string.")
            scope = command_scope(arguments)
            if self.settings.sandbox == "none":
                scope = "host"
            label = command
        else:
            session_id, text = str(arguments.get("session_id")), arguments.get("input")
            if not isinstance(text, str):
                raise tools.ToolError("input must be text.")
            try:
                terminal = sessions.get(session_id)
            except KeyError as exc:
                raise tools.ToolError(str(exc).strip("'")) from None
            scope = terminal.scope
            self._active_sandbox = "host" if scope == "host" else self.settings.sandbox
            label = f"[session {session_id}] input: {text}"
        item = {
            "id": _item_id("command"),
            "type": "commandExecution",
            "command": label,
            "cwd": str(self.workspace.root),
            "executionScope": scope,
            "status": "inProgress",
        }
        await self.host.started(item)
        if not await self._approve(item):
            return DECLINED, True
        try:
            if scope == "host":
                native_sandbox.invalidate_link_scan(self.workspace.root)
            if name == "terminal_start":
                terminal = await sessions.start(command, scope=scope)
                session_id = terminal.id
            else:
                await sessions.send(session_id, text)
        except (OSError, KeyError, RuntimeError) as exc:
            await self.host.completed({**item, "status": "failed", "exitCode": None, "error": str(exc)})
            return f"Error: {exc}", False
        output = await sessions.read(session_id, arguments.get("wait_seconds") or 2)
        terminal = sessions.get(session_id)
        await self.host.completed(
            {
                **item,
                "status": "completed",
                "exitCode": None if terminal.running else terminal.process.returncode,
                "aggregatedOutput": output[-20000:],
                "durationMs": 0,
            }
        )
        state = "running" if terminal.running else f"ended with exit code {terminal.process.returncode}"
        return f"Session {session_id} ({state}). Output:\n{output or '(none yet)'}", False

    _CLAIM = re.compile(
        r"\b(?:has|have) been (?:renamed|updated|changed|fixed|modified|created|added|removed|replaced|refactored)\b"
        r"|\bI (?:have |'ve )?(?:renamed|updated|changed|fixed|modified|created|added|removed|replaced|refactored)\b",
        re.IGNORECASE,
    )

    _FILE_MENTION = re.compile(r"[\w./\\-]+\.[A-Za-z0-9]{1,6}\b")

    def _ungrounded_local_answer(self):
        """The local model answered a question about a named file without reading anything."""
        return (
            hasattr(self.model, "force_escalate")
            and config_is_local(str(getattr(self.model, "model", "")))
            and self._reads_this_turn == 0
            and bool(self._FILE_MENTION.search(getattr(self, "_turn_prompt", "") or ""))
        )

    def _claims_unmade_changes(self, text):
        return (
            hasattr(self.model, "force_escalate")
            and config_is_local(str(getattr(self.model, "model", "")))
            and not self._changed_this_turn
            and bool(self._CLAIM.search(text or ""))
        )

    def _note_change(self, labels):
        self._needs_verify = True
        self._changed_this_turn.extend(label for label in labels if label not in self._changed_this_turn)

    def _record(self, changes):
        if not self.changes:
            self.changes.append([])
        self.changes[-1].extend((c.path, c.label, c.before, c.after) for c in changes)

    async def _patch(self, arguments):
        changes = await asyncio.to_thread(patch.prepare, self.workspace, arguments.get("patch"))
        item = {
            "id": _item_id("change"),
            "type": "fileChange",
            "changes": [{"path": c.label, "kind": {"type": c.kind}, "diff": c.diff} for c in changes],
            "status": "inProgress",
        }
        await self.host.started(item)
        if not await self._approve(item):
            return DECLINED, True
        try:
            await asyncio.to_thread(patch.apply, changes)
        except (tools.ToolError, OSError) as exc:
            await self.host.completed({**item, "status": "failed", "error": str(exc)})
            return f"Error: {exc} No file was changed.", False
        self._record(changes)
        self._note_change([c.label for c in changes])
        await self.host.completed({**item, "status": "completed"})
        summary = ", ".join(f"{c.kind} {c.label} (+{c.added} -{c.removed})" for c in changes)
        return f"Applied the patch: {summary}.", False

    async def _command(self, arguments):
        command = arguments.get("command")
        if not isinstance(command, str) or not command.strip():
            raise tools.ToolError("command must be a non-empty string.")
        scope = command_scope(arguments)
        mode = "none" if scope == "host" else self.settings.sandbox
        if mode == "none":
            scope = "host"
        requested = arguments.get("timeout_seconds") or self.settings.command_timeout
        timeout = max(5, min(int(requested), 600))
        item = {
            "id": _item_id("command"),
            "type": "commandExecution",
            "command": command,
            "cwd": str(self.workspace.root),
            "executionScope": scope,
            "status": "inProgress",
        }
        await self.host.started(item)
        if not await self._approve(item):
            return DECLINED, True

        async def on_output(text):
            await self.host.delta(item["id"], "commandExecution", text)

        container = None
        argv = None
        if mode == "docker":
            container = sandbox.new_name()
            argv = sandbox.argv(command, self.workspace.root, name=container)
        elif mode == "codex":
            try:
                argv = codex_sandbox.argv(command, self.workspace.root)
            except RuntimeError as exc:
                raise tools.ToolError(str(exc)) from None
        try:
            if mode == "native":
                result = await native_sandbox.run_command(
                    command,
                    self.workspace.root,
                    timeout,
                    on_output,
                )
            else:
                result = await run_command(
                    command,
                    self.workspace.root,
                    timeout,
                    on_output,
                    environ=sandbox.environment() if container else None,
                    argv=argv,
                )
        except OSError as exc:
            await self.host.completed({**item, "status": "failed", "exitCode": None, "error": str(exc)})
            return f"Error: the command could not start: {exc}", False
        finally:
            if container:
                await asyncio.to_thread(sandbox.remove, container)
            if scope == "host":
                native_sandbox.invalidate_link_scan(self.workspace.root)
        self._needs_verify = False  # A command ran after the last change.
        await self.host.completed(
            {
                **item,
                "status": "completed" if result.exit_code == 0 else "failed",
                "exitCode": result.exit_code,
                "aggregatedOutput": result.output[-20000:],
                "durationMs": int(result.duration * 1000),
            }
        )
        status = (
            f"Timed out after {timeout}s; the command and its child processes were stopped."
            if result.timed_out
            else f"Exit code: {result.exit_code}"
        )
        return (
            f"{status}\nDuration: {result.duration:.1f}s\nOutput:\n{_clip(result.output) or '(no output)'}",
            False,
        )
