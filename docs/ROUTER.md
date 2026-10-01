# The PANINI CLI router

One question, asked once per user message: **which model should answer this?**

## Tiers

`local` → `cheap` → `balanced` → `strong` → `frontier`

`local` is a small on-device model served by Ollama (id `ollama/<name>`, default `ollama/qwen2.5:3b-instruct`).
The others are OpenAI models configured with `PANINI_CLI_ROUTER_MODELS` (`id:tier:description,…`).
Tiers with no configured model are skipped: the router picks the cheapest available model at or above the tier it wants.

Available options are computed on every route:

| Situation | Options |
| --- | --- |
| Ollama up, model pulled, OpenAI key set | local + cloud tiers |
| No OpenAI key | local only (`only-model`) |
| Ollama down / model missing / `PANINI_CLI_LOCAL=off` | cloud tiers only |
| Neither | `no-models-available` (the session refuses to start) |

Ollama is probed with a 0.5 s timeout and the result is cached for 20 s, so routing never stalls on a dead server.

## Deciding

1. **Override.** "use qwen", "use local", "switch to gpt-4.1-mini", a tier name, or an exact model id wins.
2. **Jev** (if `AI_GATEWAY_API_KEY` is set and the sidecar is installed): one boolean question per model,
   *"should PANINI CLI use this model for this request?"*. The highest probability wins; ties go to the cheaper tier.
3. **Offline estimate** (Jev unavailable, timed out, or errored): [`complexity.py`](../src/panini_cli/agent/complexity.py) scores the
   request and maps it to a tier.

Offline scoring:

| Signal | Points |
| --- | --- |
| each hard word (refactor, architect, migrate, auth, security, concurrency, debug, deploy, database, "across", "entire" …) | +2 |
| mild engineering word (fix, bug, error, test, update …) | +1 (once) |
| easy word (what is, explain, list, show, rename, git status …), only if no hard word | −1 |
| prompt > 600 chars / > 1500 chars | +1 / +3 |
| 3+ list items, code fence, 3+ file names | +1 each |
| history > 12k tokens | +2 |

`points ≤ 0` and a short prompt → `local`; `≤ 1` → `cheap`; `≤ 3` → `balanced`; `≤ 6` → `strong`; else `frontier`.

## Guards

Applied to the chosen model whichever path picked it, in this order:

1. **Machine-task floor.** Requests about this PC (RAM, disks, processes, Downloads/Desktop …) need at least `balanced`.
2. **Complexity veto.** If Jev picked `local` but the offline estimate says the task is not simple, use the cheapest cloud model at
   or above the estimated tier. (`complexity-veto`)
3. **Low confidence** (Jev probability < 0.30). Never move to a weaker tier than the current one; never jump above `balanced`.
4. **No local after cloud context.** Going cloud → local is refused once the conversation holds more than ~2k tokens:
   the small model would inherit history it cannot use well. (`no-local-after-cloud-context`)
5. **Cache-rebuild guard.** Any downgrade past 20k tokens of history is refused. (`downgrade-not-worth-cache-rebuild`)

## Escalation

`RoutedModel` (in `model.py`) sends `ollama/…` ids to Ollama and everything else to OpenAI. A local turn is rejected when Ollama errors or
is unreachable, names a tool that does not exist, has malformed arguments, or is empty. If the model was chosen by the router (`auto`) and
no text had been shown yet, the same request is retried on `PANINI_CLI_ROUTER_FALLBACK` (default `gpt-4.1`), the loop announces
`escalated: <reason>`, and local routing is off for the rest of that session. If you pinned a local model (`--model local`), errors are
reported instead.

## Conversation format

History is kept in OpenAI Responses format. When talking to Ollama it is converted to chat messages (`function_call` → assistant `tool_calls`,
`function_call_output` → `tool` messages) and encrypted reasoning items are dropped. Local turns are written back as plain assistant messages and
function calls, which OpenAI accepts, so a task can move from local to cloud without losing its history.

## Tuning

- More capable local work: `PANINI_CLI_LOCAL_MODEL=qwen2.5:7b-instruct-q4_K_M` (slower, needs ~6 GB).
- Keep everything cloud: `PANINI_CLI_LOCAL=off`. Keep everything local: `--model local`.
- Change what counts as "simple": edit the word lists and thresholds in `complexity.py`; `tests/test_complexity.py` documents the intent.
- Watch decisions with `panini-cli route "…"` and the `Model selected:` line printed each turn.
