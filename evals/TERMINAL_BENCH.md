# Terminal-Bench maintainer evaluation handoff

PANINI is preparing for the **official Terminal-Bench 4.0 leaderboard**. It has no
official score or ranking yet. Community submissions are currently closed; the
maintainers must accept and run the evaluation. See the
[current submission policy](https://github.com/harbor-framework/terminal-bench/blob/main/leaderboard/SUBMIT.md).

The existing [custom engineering pilot](pilots/) is separate evidence. Its six
tasks and 36 attempts cannot be submitted as a Terminal-Bench result.

The [evaluation kit](https://github.com/suryacharanteja/panini-cli/releases/tag/terminal-bench-kit-20261011)
contains the frozen wheel, adapter sources and setup instructions. The
[official-task smoke evidence](terminal-bench/) retains both failed attempts:
the first response was cut off; the repeat passed five of seven hidden verifier
checks but still received reward zero. No successful official-task run is claimed.

## Reproducible evaluation inputs

| Input | Value |
| --- | --- |
| Agent | PANINI CLI 2.1.1b1, `evals.terminal_bench:TerminalBenchPaniniAgent` |
| Dataset | `terminal-bench/terminal-bench` |
| Canonical dataset digest | `sha256:39d9f44b40420cde8fdcc087579c0d72a7e14fa3656d603c3f0d22fb35e27732` |
| Full evaluation | All 66 tasks, five attempts each: 330 trials |
| Model | Pinned `openai/gpt-5.6-luna`; no model routing or local model |
| Agent limits | 60 steps, $1 estimated API budget per trial, 20,000 context tokens |
| Agent runtime | Private CPython 3.12.12, uv 0.9.5, locked PANINI dependencies |
| Task limits | Original task timeouts and resources, without overrides |
| Retry policy | No automatic trial retries; errors count as failures |
| Audit output | Native tool actions exported as ATIF; usage and estimated costs retained |

The canonical digest and trial count come from the upstream
[dataset constant](https://github.com/harbor-framework/terminal-bench/blob/main/leaderboard/src/leaderboard/core/hub.py)
and [static checks](https://github.com/harbor-framework/terminal-bench/blob/main/leaderboard/src/leaderboard/ci/static_analysis.py).
Recheck these before a future run: this handoff freezes the requirements checked
on 2026-10-11.

## Prepare without spending API credits

Use Python 3.12 on the manager and Harbor 0.24.0. The Windows EXE is for Windows
users; Harbor evaluates the same Python engine in Linux task environments.

```sh
python -m venv .benchmark
# Linux: .benchmark/bin/python; Windows: .benchmark/Scripts/python.exe
.benchmark/bin/python -m pip install -r requirements-runtime.lock
.benchmark/bin/python -m pip install -e '.[benchmark]'
.benchmark/bin/harbor auth login
.benchmark/bin/harbor datasets download terminal-bench/terminal-bench@sha256:39d9f44b40420cde8fdcc087579c0d72a7e14fa3656d603c3f0d22fb35e27732 --output-dir build/harbor/official-dataset
.benchmark/bin/python -m evals.terminal_bench \
  --wheel /path/to/panini_cli-2.1.1b1-py3-none-any.whl \
  --task-dir build/harbor/official-dataset/terminal-bench \
  --output build/harbor/terminal-bench-ready
```

Use the frozen wheel supplied with the evaluation kit and compare its checksum.
Preparation validates every downloaded task against its canonical Hub digest,
freezes the wheel, downloads and verifies the pinned uv archive, and writes
`preflight.json` and `maintainer-config.json`. It does **not** start any trials.
Use a fresh output directory for each preparation.

The adapter installs its runtime under `/installed-agent`, leaving the task's
interpreter and task source intact. It supports Linux x64 environments with
Bash, tar and sha256sum and setup access to Python distributions and PyPI.
Harbor's task environment provides containment; these trials do not exercise
Windows MXC. The original pilot adapter stays unchanged for historical replay.

## Maintainer-run full evaluation

After maintainer acceptance and a separately approved compute/API budget:

```sh
# Configure Modal and the model credential privately on the evaluation manager.
.benchmark/bin/harbor run \
  --config build/harbor/terminal-bench-ready/maintainer-config.json \
  --upload --public
```

Modal is selected in the full configuration because the dataset includes GPU
tasks. Do not remove tasks, increase task timeouts, override resources, replace
verifiers, or discard failures. The $1 agent limit is per trial; it is **not** a
330-trial aggregate spending cap and does not include environment charges.
The earlier $5 engineering pilot authorization does not fund this full run.

The maintainers can then use their leaderboard tools to filter public jobs,
complete display metadata, check all 330 records and canonical hashes, inspect
successful ATIF trajectories, and apply their review process. No submission JSON
with fabricated jobs, guessed model release dates or prefilled scores is included.

## Optional budgeted smoke test

```sh
.benchmark/bin/python -m evals.terminal_bench \
  --wheel /path/to/panini_cli-2.1.1b1-py3-none-any.whl \
  --task-dir build/harbor/official-dataset/terminal-bench \
  --output build/harbor/terminal-bench-smoke \
  --smoke-task session-window-debug --budget-usd 1
```

This runs one preselected official task once, locally through Harbor Docker,
with the original task resources and timeouts. Its host proxy keeps the real API
key outside the task and rejects requests beyond its conservative reservation
cap. It allows up to 16,384 output tokens per request and reserves that output
cost before forwarding. The custom pilot keeps its original 2,048-token default;
its historical source is available at the beta tag. Account for previous spending
before choosing a new cap. Failed, interrupted
and budget-limited attempts remain in the result; a subset is never presented as
a full benchmark accuracy or an official leaderboard score.

When a model response is cut off or fails without complete usage, the adapter
records earlier reported cost as a lower bound and leaves the trial's total cost
unknown. It does not publish a partial total as the cost of the complete trial.

The local adapter is not an ACP server. Direct **Hosted Harbor** execution still
requires an ACP implementation and its source manifest and lockfile. Maintainers
can run this kit with ordinary Harbor; hosted custom-agent acceptance has not
been claimed.
