"""Cost ledger: what every model call cost, what local routing saved, and how routes turned out.

Prices are list prices in USD per million tokens [input, output]. Models missing from the
table (and from `[prices]` in ~/.panini/config.toml) are *unpriced*: their tokens are counted
but their cost is unknown, and a budget cannot be enforced on them. Local models cost 0.

Records are appended to usage.jsonl in PANINI's data directory. Nothing leaves the machine.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from . import config

# USD per 1M tokens, [input, output]. Cached-input discounts are ignored, so this is an upper bound.
PRICES = {
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-5": (1.25, 10.00),
    "gpt-5-mini": (0.25, 2.00),
    "gpt-5-nano": (0.05, 0.40),
    "o4-mini": (1.10, 4.40),
    # GPT-5.6 standard list prices after the 2026-07-30 cut, as reported by third-party pricing
    # guides in October 2026 (not OpenAI's own page). Override under [prices] if they change.
    "gpt-5.6-luna": (0.20, 1.20),
    "gpt-5.6-terra": (2.00, 12.00),
    "gpt-5.6-sol": (5.00, 30.00),
}


def data_dir() -> Path:
    base = os.getenv("PANINI_DATA_DIR") or Path(os.getenv("LOCALAPPDATA") or Path.home() / ".local/share") / "PANINI"
    return Path(base)


def ledger_path() -> Path:
    return data_dir() / "usage.jsonl"


def _price_table():
    table = dict(PRICES)
    from . import userconfig

    for model, pair in userconfig.section("prices").items():
        if isinstance(pair, (list, tuple)) and len(pair) == 2 and all(isinstance(v, (int, float)) for v in pair):
            table[model] = (float(pair[0]), float(pair[1]))
    return table


def price(model: str):
    """(input, output) USD per million tokens, (0, 0) for local models, or None when unknown."""
    if config.is_local(model):
        return (0.0, 0.0)
    table = _price_table()
    if model in table:
        return table[model]
    base = re.sub(r"-\d{4}-\d{2}-\d{2}$", "", model or "")  # gpt-4.1-2025-04-14 -> gpt-4.1
    return table.get(base)


def cost(model: str, usage: dict):
    pair = price(model)
    if pair is None:
        return None
    return (int(usage.get("input_tokens") or 0) * pair[0] + int(usage.get("output_tokens") or 0) * pair[1]) / 1e6


def record(kind: str, **fields):
    """Append one record; a failure to write never interrupts the agent."""
    try:
        target = ledger_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"kind": kind, "t": time.time(), **fields}, ensure_ascii=False) + "\n")
    except OSError:
        pass


def read(since: float = 0.0, path: Path | None = None):
    target = path or ledger_path()
    if not target.is_file():
        return []
    rows = []
    for line in target.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("t", 0) >= since:
            rows.append(row)
    return rows


def summary(rows, *, reference_model=None):
    """Totals by model, plus what the local tokens would have cost on `reference_model`."""
    models = {}
    for row in rows:
        if row.get("kind") != "call":
            continue
        entry = models.setdefault(row["model"], {"calls": 0, "input": 0, "output": 0, "cost": 0.0, "unpriced": 0})
        entry["calls"] += 1
        entry["input"] += row.get("input_tokens", 0)
        entry["output"] += row.get("output_tokens", 0)
        if row.get("cost_usd") is None:
            entry["unpriced"] += 1
        else:
            entry["cost"] += row["cost_usd"]
    local_in = sum(v["input"] for k, v in models.items() if config.is_local(k))
    local_out = sum(v["output"] for k, v in models.items() if config.is_local(k))
    saved = None
    if reference_model:
        saved = cost(reference_model, {"input_tokens": local_in, "output_tokens": local_out})
    turns = [row for row in rows if row.get("kind") == "turn"]
    return {
        "models": models,
        "cost_usd": sum(v["cost"] for v in models.values()),
        "local_tokens": local_in + local_out,
        "saved_usd": saved,
        "turns": len(turns),
        "escalations": sum(1 for row in turns if row.get("escalated")),
    }
