# Codebase optimisation review — 2026-10-10

Scope: structural inventory of all 52 first-party Python modules, dependency closure and native Cargo workspace; focused manual review and changes to CLI dispatch, credential lifecycle, receipt verification and agent tool batching. This is not a line-by-line security audit of the 463 upstream Rust files.

## Measured changes

| Measure | Before | After |
| --- | ---: | ---: |
| First-party Python modules | 52 | 52 |
| First-party Python source lines | 11,039 | 11,000 |
| CLI main function lines | 391 | 240 |
| CLI main decision points | 98 | 61 |

Decision points count AST if/for/while/exception handlers/conditional expressions/comprehensions, including nested functions. This is a structural indicator, not formal cyclomatic complexity. Source counts include comments and blank lines, excluding vendored native code, tests and build outputs. Before/after inventories are in build/optimisation.

## Implemented

- Shared auth handler serves explicit CLI commands, interactive commands and initial key setup. Credential save/validation/error handling has one implementation. Jev logout now also stops its warm sidecar in the explicit CLI path, matching interactive logout.
- Shared receipt verification handles interactive and explicit commands, including export. Invalid session IDs and export I/O errors now return controlled errors rather than escaping the CLI.
- Dedicated sandbox and preview command handlers stay in the existing CLI module. These separate lifecycle responsibilities without adding files or introducing a generic framework.
- Ordered queues replace list-front removals and linear pending-ID removals in tool batches. Queue bookkeeping is linear in batch size; tool execution and model latency are unchanged. Read concurrency and write barriers remain intact.
- Existing audit script now reports per-function size and decision points so future changes can be measured.

## Architecture findings

The execution broker, process-tree owner, verified filesystem reader, model adapters and preview service already provide useful boundaries. Reuse these instead of introducing another layer of managers or factories. Configuration loading and effective execution policy remain separate because their trust boundaries differ.

No exact duplicate functions longer than six lines appeared in the AST scan. Similar credential and receipt paths nevertheless duplicated behavior and were consolidated. No source modules were removed: legacy engine and sandbox adapters remain exposed by supported entrypoints/options; tests and native SDK sources are required for compatibility and reproducible builds. Build archives are excluded from source counts and are recoverable artifacts, not redundant implementation.

Remaining complexity is concentrated in engine.run (378 lines), agent.turn (157 lines) and CLI interactive dispatch (114 lines). Further reductions should extract tested state transitions, not collapse safety checks or replace understandable branches with opaque registries. The CLI main function still has substantial initialization and session responsibilities.

## Validation

- Final Python suite: **354 passed, 3 skipped** (153.30 seconds), report `build/optimisation/pytest.xml`.
- Ruff: passed across source, tests and scripts.
- Packaged MXC UAT: passed workspace write, outside-write denial, secret file/environment protection and recovery.
- Packaged web UAT: passed registry-only dependency installation, offline build/test, authenticated preview, secret denial and verified shutdown, without host escalation.
- Source hashes matched the post-change inventory during validation. Rebuilt and replaced `dist/panini-cli.exe`; previous executable archived recoverably under `build/dist-archive`.
- New regression tests cover both Jev logout interfaces, invalid receipt session IDs, concurrent read batches, write barriers and ordered results.

Reports: `build/optimisation/uat/sandbox-verification.json` and `build/optimisation/uat/web-sandbox-verification.json`. Three skipped tests are not claimed as validated. Existing live-provider and terminal/platform limitations remain; this change makes no coverage-percentage or security-certification claim.
