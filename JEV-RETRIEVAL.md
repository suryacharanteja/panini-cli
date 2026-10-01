# PANINI-CLI code retrieval (experimental)

`search_code` is a read-only, bounded tool. It returns workspace-relative paths,
one-line locations, SHA-256 hashes of source bytes, compact previews, and an
`incomplete` flag. It skips dependency/build trees, symlinks, binary files, and
known credential filenames. The agent must use `read_file` before editing code.

The default ranking is lexical. In a PANINI source checkout with the frontend
Node dependencies and `AI_GATEWAY_API_KEY` configured, set
`PANINI_CLI_JEV=1` to rerank at most eight candidate locations with Jev.
Jev never filters matches, and failures return `lexical_fallback` in the tool
result. `selection` includes Jev cost, input tokens, and latency when available.
Jev judges *where to read*, not what to change or whether a task is solved.

This is not a validated token-saving configuration. Before making Jev the
default, run paired coding tasks with the same model, prompts, and test oracle.
Record total model tokens, Jev input tokens and cost, elapsed time, and verified
solves; reject a cheaper configuration if it loses solves. The current local
tests cover the tool contract and Jev ranking adapter, not this live solve gate.

## First live pair (2026-09-27, Windows)

Read-only task: locate the function that drops older tool outputs when context
exceeds a budget. Both runs used `gpt-5.6-luna`, the same prompt and workspace.
The oracle was `src/panini_cli/agent/loop.py`: the method is
`Agent._fit_context` (line 215).

| Configuration | Model tokens | Jev input tokens | Jev cost | Elapsed | Answer |
| --- | ---: | ---: | ---: | ---: | --- |
| Lexical | 13,020 | 0 | $0 | 9.5 s | Correct file and function |
| Jev | 15,252 | 3,225 | $0.00013545 | 11.7 s | Correct file/method, incorrect containing class (`AgentLoop`) |

The treatment made three Jev calls versus two lexical searches in the
baseline. This single pair is not a statistically useful benchmark, but it is
already a failure of the proposed cost-and-solve promotion gate. Keep Jev
disabled by default. A larger paired suite must cover real code changes and
test-verified solves before release as an optimization.

The Jev adapter currently uses PANINI's existing backend sidecar from a source
checkout. An isolated pip-installed `panini-cli` wheel falls back to lexical
ranking; bundling a standalone Windows sidecar remains release work.


