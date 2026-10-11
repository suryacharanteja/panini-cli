"""Agent settings. Credentials come from the environment or PANINI's own .env, never the workspace."""

import os
import sys
from dataclasses import dataclass
from pathlib import Path

PANINI_ROOT = Path(__file__).resolve().parents[3]  # the repo root in a source checkout
EFFORTS = ("minimal", "low", "medium", "high")
DEFAULT_SANDBOX = "mxc" if os.name == "nt" else "none"
DEFAULT_MODEL = "auto"
DEFAULT_ROUTER_FALLBACK_MODEL = "gpt-5.6-terra"
# Local models run through Ollama and are addressed as "ollama/<name>".
LOCAL_PREFIX = "ollama/"
DEFAULT_LOCAL_MODEL = "qwen2.5:3b-instruct"
DEFAULT_OLLAMA_HOST = "http://127.0.0.1:11434"
DEFAULT_LOCAL_CONTEXT = 16_384
DEFAULT_ROUTER_MODELS = (
    "gpt-5.6-luna:cheap:Fast mechanical edits, tiny lookups, formatting, and simple shell checks",
    "gpt-5.6-terra:balanced:Routine coding, bounded debugging, command-line setup, and test repair",
    "gpt-5.6-sol:strong:Harder multi-file coding, architecture, unfamiliar failures, and higher blast radius work",
)


def load_panini_env():
    """The terminal CLI runs in any folder, so the workspace's own .env is never loaded."""
    from .auth import PROVIDERS

    roots = [Path(os.environ["PANINI_RUNTIME_ROOT"])] if os.getenv("PANINI_RUNTIME_ROOT") else []
    if getattr(sys, "frozen", False):
        roots.append(Path(sys.executable).resolve().parent)
    for root in [*roots, PANINI_ROOT]:
        path = root / ".env"
        if path.is_file():
            from dotenv import load_dotenv

            load_dotenv(path, override=False)
    if os.name == "nt":
        from .auth import read_key

        for _, variable, target in PROVIDERS.values():
            value = os.getenv(variable, "").strip()
            if value and not value.startswith("your_"):
                continue
            key = read_key(target=target)
            if key:
                os.environ[variable] = key


def ollama_host():
    host = os.getenv("OLLAMA_HOST", "").strip() or DEFAULT_OLLAMA_HOST
    if "://" not in host:
        host = "http://" + host
    return host.rstrip("/")


def local_model():
    """The Ollama model id, with the ollama/ prefix. PANINI_CLI_LOCAL=off disables local routing."""
    name = os.getenv("PANINI_CLI_LOCAL_MODEL", "").strip() or DEFAULT_LOCAL_MODEL
    return name if name.startswith(LOCAL_PREFIX) else LOCAL_PREFIX + name


def local_strong_model():
    """Optional bigger local model tried before the cloud when the small one fails (e.g. qwen2.5:7b)."""
    name = os.getenv("PANINI_CLI_LOCAL_STRONG_MODEL", "").strip()
    if not name:
        return None
    return name if name.startswith(LOCAL_PREFIX) else LOCAL_PREFIX + name


def local_enabled():
    return os.getenv("PANINI_CLI_LOCAL", "auto").strip().lower() not in {"off", "0", "false", "no"}


def is_local(model):
    return (model or "").startswith(LOCAL_PREFIX)


def configured_key():
    value = os.getenv("OPENAI_API_KEY", "").strip()
    return bool(value) and not value.startswith("your_")


def _integer(name, default, low, high):
    try:
        return max(low, min(high, int(os.getenv(name, default))))
    except ValueError:
        return default


def _budget():
    try:
        value = float(os.getenv("PANINI_CLI_BUDGET_USD", "") or 0)
    except ValueError:
        return None
    return value if value > 0 else None


def command_mode():
    mode = os.getenv("PANINI_CLI_SANDBOX", DEFAULT_SANDBOX).strip().lower() or DEFAULT_SANDBOX
    if mode not in {"none", "mxc", "native", "codex", "docker"}:
        raise ValueError(f"Invalid PANINI_CLI_SANDBOX={mode!r}; choose mxc, none, native, codex, or docker")
    return mode


def session_policy(params):
    """Validate RPC policy explicitly; unsupported grants never disappear silently."""
    config = params.get("config") or {}
    allowed = {"sandbox", "network_hosts", "recoverable", "local_preview", "model_reasoning_effort", "acceptance_checks", "mcp_enabled"}
    if not isinstance(config, dict) or set(config) - allowed:
        raise ValueError("Unsupported session config keys")
    selected = params.get("sandboxPolicy", params.get("sandbox", config.get("sandbox")))
    network = config.get("network_hosts")
    if isinstance(selected, dict):
        if set(selected) - {"type", "networkHosts", "networkAccess"}:
            raise ValueError("Unsupported sandbox policy fields")
        if "networkAccess" in selected and not isinstance(selected["networkAccess"], bool):
            raise ValueError("networkAccess must be boolean")
        network = selected.get("networkHosts", network)
        if selected.get("networkAccess") is True and not network:
            raise ValueError("Grant exact networkHosts instead of unrestricted networking")
        if selected.get("networkAccess") is False:
            network = []
        selected = selected.get("type")
    read_only = selected in {"readOnly", "read-only"}
    aliases = {"readOnly": "mxc", "read-only": "mxc", "workspaceWrite": "mxc", "workspace-write": "mxc",
               "dangerFullAccess": "none", "full-access": "none"}
    backend = aliases.get(selected, selected)
    effort = config.get("model_reasoning_effort")
    if effort is not None and effort not in EFFORTS:
        raise ValueError("Unsupported model reasoning effort")
    settings = Settings.from_env(model=params.get("model"), reasoning_effort=effort)
    settings.execution_options(sandbox=backend, network_hosts=network,
        recoverable=config.get("recoverable"), local_preview=config.get("local_preview"))
    checks = config.get("acceptance_checks", [])
    if not isinstance(checks, list) or len(checks) > 20 or any(not isinstance(c, str) or not c.strip() or len(c) > 4096 for c in checks):
        raise ValueError("acceptance_checks must contain at most 20 bounded commands")
    settings.acceptance_checks = tuple(checks)
    if "mcp_enabled" in config:
        if not isinstance(config["mcp_enabled"], bool):
            raise ValueError("mcp_enabled must be boolean")
        settings.mcp_enabled = config["mcp_enabled"]
    mode = params.get("approvalPolicy", "untrusted")
    mode = {"onRequest": "ask", "unlessTrusted": "ask", "read-only": "read-only"}.get(mode, mode)
    if mode not in {"untrusted", "ask", "edits", "auto", "never", "read-only"}:
        raise ValueError("Unsupported approval policy")
    read_only = read_only or mode == "read-only"
    if read_only and settings.sandbox != "mxc":
        raise ValueError("Read-only command enforcement requires MXC")
    return settings, mode, read_only


@dataclass
class Settings:
    model: str = DEFAULT_MODEL
    reasoning_effort: str | None = None
    max_steps: int = 60
    # Every turn resends the whole context; older tool outputs are dropped past this budget.
    context_tokens: int = 96_000
    command_timeout: int = 120
    request_timeout: float = 180.0
    sandbox: str = DEFAULT_SANDBOX  # mxc on Windows; legacy adapters are explicit.
    network_hosts: tuple[str, ...] = ()  # Explicit user grants; exact HTTPS hosts only.
    local_preview: bool = False  # Explicit grant to serve built assets through the read-only broker.
    recoverable: bool = False
    terminal_timeout: int = 3600
    budget_usd: float | None = None  # Stop a session's priced cloud spend at this amount.
    acceptance_checks: tuple[str, ...] = ()
    mcp_enabled: bool = True

    def execution_options(self, *, sandbox=None, network_hosts=None, recoverable=None, local_preview=None):
        """Apply validated task execution grants without mutating process-wide configuration."""
        if sandbox is not None:
            if sandbox not in {"mxc", "none", "native", "codex", "docker"}:
                raise ValueError("Unknown sandbox backend")
            self.sandbox = sandbox
        if network_hosts is not None:
            from .network_proxy import normalize_hosts

            if not isinstance(network_hosts, (list, tuple)) or len(network_hosts) > 20:
                raise ValueError("networkHosts must be a list of at most 20 exact HTTPS hosts")
            self.network_hosts = normalize_hosts(network_hosts)
        for name, value in (("recoverable", recoverable), ("local_preview", local_preview)):
            if value is not None:
                if not isinstance(value, bool):
                    raise ValueError(f"{name} must be a boolean")
                setattr(self, name, value)
        return self

    @classmethod
    def from_env(cls, *, model=None, reasoning_effort=None):
        effort = (
            reasoning_effort
            or os.getenv("PANINI_CLI_REASONING_EFFORT")
            or None
        )
        return cls(
            model=(
                model
                or os.getenv("PANINI_CLI_MODEL")
                or os.getenv("JEV_REV_CODE_MODEL")
                or os.getenv("PANINI_AGENT_MODEL")
                or os.getenv("PANINI_PLANNER_MODEL")
                or os.getenv("EXTENSION_PLANNER_MODEL")
                or cls.model
            ),
            reasoning_effort=effort if effort in EFFORTS else None,
            max_steps=_integer("PANINI_CLI_MAX_STEPS", cls.max_steps, 1, 500),
            context_tokens=_integer("PANINI_CLI_CONTEXT_TOKENS", cls.context_tokens, 20_000, 1_000_000),
            command_timeout=_integer("PANINI_CLI_COMMAND_TIMEOUT", cls.command_timeout, 5, 600),
            sandbox=command_mode(),
            network_hosts=tuple(h.strip() for h in os.getenv("PANINI_CLI_NETWORK_HOSTS", "").split(",") if h.strip()),
            local_preview=os.getenv("PANINI_CLI_LOCAL_PREVIEW", "").lower() in {"1", "true", "yes"},
            recoverable=os.getenv("PANINI_CLI_RECOVERABLE", "").lower() in {"1", "true", "yes"},
            budget_usd=_budget(),
        )
