# General model routing

The harness owns tools, approvals, hooks, sandbox, receipts, state, verification and
interruption. Providers implement the same respond interface. Models construct
function calls; routing does not construct commands or task-specific actions.

## Selection

Honor explicit model choices. Discover usable backends. Jev independently rates
every candidate's ability to complete the request with the shared approved function
tools and current context. Choose the cheapest tier above its capability threshold:
local 0.65, cloud 0.60. These values are policy thresholds, not calibrated guarantees.
Topic words and edit intent do not veto a confident Jev assessment.

Backend availability, image transport support, session cooldown and local context
capacity constrain candidates. Independent simple questions avoid paid routing.
When Jev is unavailable or lacks usable scores, an offline workload estimate is used.
Cache decisions for 60 seconds using eligible providers and context in the key.
Route once per user turn; subsequent tool steps stay on that model.

## Execution and escalation

Qwen receives the same approved function tools, including edits, patches, shell,
terminals and connected tools. OpenAI hosted tools cannot be transported to Ollama.
Provider-neutral function calls and results preserve conversation continuity.

Errors or invalid output may retry on a configured stronger local model, then cloud.
The loop also escalates after two observed tool failures, more than six local model
steps, or completion of edited work without a successful verification command after
a reminder. Changes and outputs are retained so escalation continues from evidence.
Jev additionally checks local tool-backed final answers against the original request and recorded tool evidence. Complete and grounded scores must each reach 0.75. A failed check escalates; unavailable checking falls back to harness checks. These are probabilistic quality gates, not correctness guarantees. Denied actions skip quality-driven escalation. Explicitly pinned local models never switch
providers automatically. Backend error cooldown expires after 60 seconds.

Verification is bounded: a successful command proves execution, not correctness of
every requested property. Project test commands and live task oracles remain vital.

## Context and accounting

PANINI_CLI_LOCAL_CTX sets the local window (default 16384). Routing reserves 6000
tokens for standing instructions, tools and response; this is an estimate. Unrelated
simple questions may isolate input while preserving full session history.

Events expose chosen model, reason, Jev confidence and cost. Local tokens consume
machine resources without API charges. Historical failure statistics do not disable
local for unrelated future tasks. See evals/results for retained live regressions.

## Reference patterns

Shared model/tool loops and application-owned approvals/state follow the
[Codex harness](https://developers.openai.com/blog/codex-as-a-platform) and
[agent loop](https://developers.openai.com/api/docs/guides/agents/running-agents).
Jev selection and the local/cloud cascade are PANINI extensions.
