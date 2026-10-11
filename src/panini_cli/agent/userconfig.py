"""~/.panini/config.toml: user settings and named profiles.

Precedence, highest first: command-line flags, environment variables, the selected profile,
the file's top-level keys, built-in defaults. Only the user's own file is read; a project
cannot change approvals or sandboxing by shipping a config file.

    model = "auto"
    approval_mode = "ask"
    local_model = "qwen2.5:3b-instruct"
    budget_usd = 2.0

    [profiles.offline]
    model = "local"

    [profiles.careful]
    approval_mode = "read-only"

    [prices]                       # USD per million tokens: [input, output]
    "gpt-5.6-terra" = [1.25, 10.0]

    [mcp_servers.files]
    command = "npx"
    args = ["-y", "@modelcontextprotocol/server-filesystem", "."]
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

# config key -> environment variable it fills when that variable is not already set
ENV_KEYS = {
    "model": "PANINI_CLI_MODEL",
    "approval_mode": "PANINI_CLI_APPROVAL_MODE",
    "sandbox": "PANINI_CLI_SANDBOX",
    "network_hosts": "PANINI_CLI_NETWORK_HOSTS",
    "recoverable": "PANINI_CLI_RECOVERABLE",
    "local_preview": "PANINI_CLI_LOCAL_PREVIEW",
    "reasoning_effort": "PANINI_CLI_REASONING_EFFORT",
    "local": "PANINI_CLI_LOCAL",
    "local_model": "PANINI_CLI_LOCAL_MODEL",
    "local_context": "PANINI_CLI_LOCAL_CTX",
    "ollama_host": "OLLAMA_HOST",
    "router_models": "PANINI_CLI_ROUTER_MODELS",
    "router_fallback": "PANINI_CLI_ROUTER_FALLBACK",
    "budget_usd": "PANINI_CLI_BUDGET_USD",
    "max_steps": "PANINI_CLI_MAX_STEPS",
    "context_tokens": "PANINI_CLI_CONTEXT_TOKENS",
    "command_timeout": "PANINI_CLI_COMMAND_TIMEOUT",
    "trust_read_only": "PANINI_CLI_TRUST_READ_ONLY",
    "web_search": "PANINI_CLI_WEB_SEARCH",
    "auto_verify": "PANINI_CLI_AUTO_VERIFY",
    "explore": "PANINI_CLI_EXPLORE",
    "local_strong_model": "PANINI_CLI_LOCAL_STRONG_MODEL",
    "checkpoints": "PANINI_CLI_CHECKPOINTS",
}

TEMPLATE = '''# PANINI CLI settings. Environment variables and command-line flags override these.
model = "auto"                 # auto | local | <OpenAI model id> | ollama/<name>
approval_mode = "ask"          # read-only | ask | edits | auto
# sandbox = "mxc"              # mxc | none | native | codex | docker
# network_hosts = ["registry.npmjs.org", "pypi.org", "files.pythonhosted.org"]
# recoverable = true           # bounded command snapshots including untracked files
local_model = "qwen2.5:3b-instruct"
# budget_usd = 2.0             # stop a session's cloud spend at this amount
# router_models = ["gpt-5.6-luna:cheap:Fast edits", "gpt-5.6-terra:balanced:Routine coding", "gpt-5.6-sol:strong:Hard work"]

[profiles.offline]
model = "local"

[profiles.careful]
approval_mode = "read-only"

[profiles.yolo]
approval_mode = "auto"

[prices]   # USD per million tokens, [input, output]; used for the cost ledger
# "gpt-5.6-terra" = [1.25, 10.0]

# [mcp_servers.filesystem]
# command = "npx"
# args = ["-y", "@modelcontextprotocol/server-filesystem", "."]
'''


def path() -> Path:
    return Path(os.getenv("PANINI_CONFIG") or Path.home() / ".panini" / "config.toml")


def load() -> dict:
    target = path()
    if not target.is_file():
        return {}
    try:
        return tomllib.loads(target.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"{target}: {exc}") from None


def _as_env(key, value):
    if key == "network_hosts" and isinstance(value, list):
        return ",".join(str(host) for host in value)
    if isinstance(value, bool):
        return "1" if value else "0"
    if key == "local" and isinstance(value, str):
        return value
    if key == "router_models" and isinstance(value, list):
        parts = []
        for entry in value:
            if isinstance(entry, dict):
                parts.append(":".join(str(entry.get(k, "")) for k in ("id", "tier", "description")))
            else:
                parts.append(str(entry))
        return ",".join(parts)
    return str(value)


def effective(data: dict, profile: str | None = None) -> dict:
    """Top-level keys overlaid with the named profile's keys."""
    name = profile or os.getenv("PANINI_CLI_PROFILE") or data.get("profile")
    merged = {key: value for key, value in data.items() if key in ENV_KEYS}
    if name:
        profiles = data.get("profiles") or {}
        if name not in profiles:
            raise ValueError(f"Unknown profile {name!r}. Defined: {', '.join(profiles) or 'none'}")
        merged.update({key: value for key, value in profiles[name].items() if key in ENV_KEYS})
    return merged


def apply(profile: str | None = None) -> dict:
    """Fill unset environment variables from the config file. Returns the whole parsed file."""
    data = load()
    for key, value in effective(data, profile).items():
        os.environ.setdefault(ENV_KEYS[key], _as_env(key, value))
    return data


def section(name: str) -> dict:
    try:
        value = load().get(name) or {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def init() -> Path:
    target = path()
    if target.exists():
        raise FileExistsError(f"{target} already exists")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(TEMPLATE, encoding="utf-8")
    return target
