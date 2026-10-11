"""One execution broker for task commands, terminal sessions and project hooks.

Policy and authorization are separate from adapters. Microsoft MXC validates the
exact request before launch; unavailable guarantees never result in a host retry.
"""

import asyncio
import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from . import codex_sandbox, mxc_runtime, native_sandbox, sandbox, tools
from .network_proxy import NetworkProxy, normalize_hosts
from .shell import powershell_script, run_command, shell_argv


@dataclass(frozen=True)
class ExecutionPolicy:
    workspace: Path
    backend: str
    read_only: bool = False
    network_hosts: tuple[str, ...] = ()
    local_preview: bool = False
    max_processes: int = 64
    max_memory: int = 2 * 1024**3
    max_cpu_percent: int = 50
    max_output_bytes: int = 4 * 1024**2
    read_roots: tuple[str, ...] = ()
    runtime_identity: str = ""

    @property
    def identity(self):
        data = (os.path.normcase(str(self.workspace.resolve())), self.backend, self.read_only,
                self.network_hosts, self.local_preview, self.max_processes, self.max_memory, self.max_cpu_percent, self.max_output_bytes,
                self.read_roots, self.runtime_identity)
        return hashlib.sha256(json.dumps(data).encode()).hexdigest()


@dataclass
class PreparedExecution:
    argv: list[str]
    cwd: Path
    environ: dict | None = None
    receipt: dict = field(default_factory=dict)


def secret_paths(workspace):
    """Actual denied file paths, including nested secrets and directory reparse checks."""
    native_sandbox._reject_unsafe_workspace_links(workspace, allow_internal_hardlinks=True)
    paths = []
    for directory, _, files in os.walk(workspace, followlinks=False):
        paths.extend(str(Path(directory) / name) for name in files if tools.is_secret(name))
    return paths


def mxc_config(policy, command, scratch, proxy=None, *, process_argv=None, bridge=None):
    roots = tuple(map(Path, policy.read_roots))
    environment = native_sandbox.native_environment(scratch)
    environment["PATH"] = native_sandbox.toolchain_path(roots)
    environment.update(UV_LINK_MODE="copy")
    if bridge:
        shim = scratch / "panini-preview.cjs"
        shim.write_text(bridge["source"], encoding="utf-8")
        environment.update(NODE_OPTIONS=f'--require "{shim.as_posix()}"', PANINI_BRIDGE_TOKEN=bridge["token"],
                           PANINI_BRIDGE_PORT=str(bridge["port"]))
    if proxy:
        environment.update(HTTP_PROXY=proxy.url, HTTPS_PROXY=proxy.url, ALL_PROXY=proxy.url,
                           NO_PROXY="", http_proxy=proxy.url, https_proxy=proxy.url, no_proxy="")
    shell = str(Path(os.environ["SystemRoot"]) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")
    script = powershell_script(command)
    encoded = base64.b64encode(script.encode("utf-16-le")).decode()
    process_line = subprocess.list2cmdline(process_argv) if process_argv else subprocess.list2cmdline(
        [shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-OutputFormat", "Text", "-EncodedCommand", encoded])
    if process_argv and Path(process_argv[0]).name.lower() == "cmd.exe" and process_argv[-2].lower() == "/c":
        process_line = subprocess.list2cmdline(process_argv[:-1]) + " " + process_argv[-1]
    readonly = list(policy.read_roots)
    # Toolchain paths are explicit grants, not permission to expose an entire user profile.
    home = Path.home().resolve()
    for root in map(Path, readonly):
        if root == home or root.parent == root:
            raise OSError("Toolchain read grant would expose an entire profile or drive.")
    workspace = policy.workspace
    protected = [workspace / ".git", workspace / ".panini"]
    readonly.extend(str(p) for p in protected if p.exists())
    if policy.read_only:
        readonly.append(str(workspace))
    granted = [workspace, scratch, *map(Path, readonly)]
    enumerate_paths = sorted({str(parent) for path in granted for parent in path.parents
                              if not any(parent.is_relative_to(root) for root in granted)})
    config = {
        "version": mxc_runtime.VERSION,
        "containment": "processcontainer",
        "fallback": {"allowDaclMutation": False},
        "process": {"commandLine": process_line,
                    "cwd": str(workspace), "env": [f"{k}={v}" for k, v in sorted(environment.items())],
                    "inheritDefaultEnv": False, "timeout": 0},
        "filesystem": {"readonlyPaths": list(dict.fromkeys(readonly)),
                       "readwritePaths": [str(scratch)] + ([] if policy.read_only else [str(workspace)]),
                       "deniedPaths": secret_paths(workspace)},
        "network": {"egress": {"default": "deny"},
                    "ingress": {"default": "deny", "hostLoopback": "deny"}},
        # PowerShell needs Win32k initialization even when non-interactive.
        # Block clipboard and injection separately; the launcher hides windows.
        "ui": {"disable": False, "clipboard": "none", "injection": False},
        "telemetry": {"enabled": False},
        "processContainer": {"filesystem": {"enumeratePaths": enumerate_paths}},
    }
    if proxy:
        config["runtimeConfig"] = {"networkProxy": proxy.url}
        config["network"]["ingress"] = {"default": "allow", "hostLoopback": "allow"}
    return config


def require_capabilities(probe, *, proxy=False):
    facts = probe.get("probes", {})
    if probe.get("error") or probe.get("tier") != "base-container" or probe.get("needsDaclAugmentation") is not False:
        raise OSError("MXC requires native BaseContainer isolation without host ACL mutation: " + json.dumps(probe))
    if facts.get("baseContainerSupportsDenyPaths") is not True:
        raise OSError("This Windows host cannot enforce MXC secret-file exclusions.")
    if facts.get("baseContainerSupportsEnumeratePaths") is not True:
        raise OSError("This Windows host cannot supply metadata-only toolchain ancestor access.")
    ui = facts.get("uiCapabilities", {})
    if any(ui.get(name) is not True for name in ("canBlockClipboardRead", "canBlockClipboardWrite", "canBlockInputInjection")):
        raise OSError("This Windows host cannot enforce the required clipboard and input restrictions.")
    if proxy and facts.get("baseContainerSupportsIngressHostLoopbackAllow") is not True:
        raise OSError("This Windows host cannot enforce the required proxy-only network policy.")
    return {"backend": "mxc", "tier": probe["tier"], "warnings": probe.get("warnings", [])}


class ExecutionBroker:
    def __init__(self, workspace, settings, *, read_only=False):
        self.workspace = Path(workspace).resolve(strict=True)
        self.settings = settings
        self.read_only = read_only

    def policy(self, scope="workspace"):
        backend = "none" if scope == "host" else self.settings.sandbox
        hosts = () if scope == "host" else normalize_hosts(getattr(self.settings, "network_hosts", ()))
        if hosts and backend != "mxc":
            raise OSError("Scoped network grants require the MXC backend; host escalation is not a substitute.")
        preview = bool(getattr(self.settings, "local_preview", False))
        if preview and backend != "mxc" and scope != "host":
            raise OSError("Built-asset previews require the MXC command sandbox.")
        if scope == "host":
            preview = False
        roots = ()
        if backend == "mxc":
            roots = tuple(sorted({str(Path(sys.prefix).resolve()), str(Path(sys.base_prefix).resolve()),
                                  *map(str, native_sandbox.toolchain_roots(include_system=True))}))
        return ExecutionPolicy(self.workspace, backend, self.read_only, hosts, preview, read_roots=roots,
                               runtime_identity=mxc_runtime.identity() if backend == "mxc" else "")

    def authorization(self, scope="workspace"):
        policy = self.policy(scope)
        return {"executionScope": "host" if policy.backend == "none" else "workspace",
                "policyIdentity": policy.identity, "sandbox": "host" if policy.backend == "none" else policy.backend,
                "filesystemMode": "host-unrestricted" if policy.backend == "none" else "read-only" if policy.read_only else "workspace-write",
                "networkMode": "host-unrestricted" if policy.backend == "none" else "proxy-only" if policy.network_hosts else "offline",
                "localPreview": policy.local_preview,
                "networkHosts": list(policy.network_hosts)}

    @asynccontextmanager
    async def prepare(self, command, scope="workspace", *, process_argv=None, bridge=None):
        policy = self.policy(scope)
        if policy.backend != "mxc":
            if policy.read_only and policy.backend != "none":
                raise OSError("Enforced read-only command execution requires MXC.")
            if policy.backend == "native":
                raise OSError("Native commands must use the native run adapter.")
            name = sandbox.new_name() if policy.backend == "docker" else None
            try:
                argv = sandbox.argv(command, self.workspace, name=name) if name else (
                    codex_sandbox.argv(command, self.workspace) if policy.backend == "codex" else shell_argv(command))
                yield PreparedExecution(argv, self.workspace, sandbox.environment() if name else None)
            finally:
                if name:
                    await asyncio.to_thread(sandbox.remove, name)
            return
        if os.name != "nt":
            raise OSError("MXC process execution requires Windows.")
        proxy = NetworkProxy(policy.network_hosts).start() if policy.network_hosts else None
        try:
            with tempfile.TemporaryDirectory(prefix="panini-mxc-") as directory:
                scratch = Path(directory).resolve()
                if scratch.is_relative_to(self.workspace):
                    raise OSError("Sandbox scratch storage must be outside the writable project.")
                config = await asyncio.to_thread(mxc_config, policy, command, scratch, proxy,
                                                 process_argv=process_argv, bridge=bridge)
                probe = await asyncio.to_thread(mxc_runtime.probe, config, self.workspace)
                receipt = require_capabilities(probe, proxy=proxy is not None)
                receipt.update(policyIdentity=policy.identity, networkHosts=list(policy.network_hosts), readRoots=list(policy.read_roots),
                               engine="panini-sandbox", engineVersion=mxc_runtime.VERSION, runtimeIdentity=policy.runtime_identity,
                               filesystemMode="read-only" if policy.read_only else "workspace-write",
                               maxProcesses=policy.max_processes, maxMemoryBytes=policy.max_memory,
                               maxCpuPercent=policy.max_cpu_percent, maxOutputBytes=policy.max_output_bytes)
                # Keep configuration outside the writable scratch directory. The
                # workload cannot replace its executor policy or acquire broader grants.
                with tempfile.TemporaryDirectory(prefix="panini-mxc-policy-") as config_directory:
                    config_file = Path(config_directory) / "policy.json"
                    config_file.write_text(json.dumps(config), encoding="utf-8")
                    binary = mxc_runtime.executable()
                    yield PreparedExecution([str(binary), str(config_file)], binary.parent, receipt=receipt)
        finally:
            if proxy:
                await asyncio.to_thread(proxy.close)

    async def run(self, command, timeout, on_output=None, *, scope="workspace", stdin_text=None, separate_stderr=False):
        policy = self.policy(scope)
        if policy.backend == "native":
            if self.read_only or stdin_text is not None:
                raise OSError("This native adapter cannot enforce read-only execution or hook input; use MXC.")
            return await native_sandbox.run_command(command, self.workspace, timeout, on_output)
        async with self.prepare(command, scope) as prepared:
            snapshot = None
            if getattr(self.settings, "recoverable", False) and not policy.read_only:
                from .recovery import Snapshot

                snapshot = await asyncio.to_thread(Snapshot, self.workspace)
            try:
                result = await run_command(command, prepared.cwd, timeout, on_output,
                                           environ=prepared.environ, argv=prepared.argv, stdin_text=stdin_text,
                                           separate_stderr=separate_stderr,
                                           max_processes=policy.max_processes, max_job_memory=policy.max_memory,
                                           max_cpu_percent=policy.max_cpu_percent, max_output_bytes=policy.max_output_bytes)
            except OSError as exc:
                if snapshot:
                    raise OSError(f"{exc}. Recovery snapshot: {snapshot.id}.") from exc
                raise
            finally:
                if snapshot:
                    try:
                        await asyncio.to_thread(snapshot.finish)
                    except OSError as exc:
                        raise OSError(f"Recovery completion failed for snapshot {snapshot.id}: {exc}") from exc
            result.policy = prepared.receipt
            result.recovery_id = snapshot.id if snapshot else None
            return result

    async def status(self):
        if self.settings.sandbox != "mxc":
            return {**self.authorization(), "backend": self.settings.sandbox}
        async with self.prepare("Write-Output MXC_READY") as prepared:
            return prepared.receipt
