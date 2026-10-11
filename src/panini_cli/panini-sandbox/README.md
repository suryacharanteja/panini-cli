# PANINI Sandbox

This is PANINI CLI's native sandbox source, adapted from Microsoft MXC 1.0.0.
PANINI builds and uses this directory directly. Original Microsoft copyright
headers, the MIT license and trademark notices are retained.

Build from the PANINI CLI repository root:

```powershell
./scripts/build-sandbox.ps1
panini-cli sandbox install
panini-cli sandbox status
```

Requirements: Rust 1.93.0, Visual Studio C++ build tools, Windows SDK, and a
Windows host with native BaseContainer support. Run PowerShell 7 for the build.

The retained Cargo workspace has three crates:

- `src/mxc-sdk`: isolation engine, policy contracts, capability probes and tests.
- `src/ffi/mxc_ffi`: native DLL used by Python for exact policy validation.
- `src/tools/wxc`: native executor used for streaming commands and terminals.

`.cargo/config.toml` retains Windows CFG, ASLR, CET and static CRT hardening.
`src/Cargo.lock` pins dependencies. Shared source modules stay because the
engine's public API and dispatch compile against them; optional VM features
are disabled. Native test fixtures are retained for regression testing.

The build emits only `runtime/win-x64` (or `win-arm64`) executor, DLL, license
and `build.json`. The manifest records source and binary hashes, with the
source identity embedded in the executable's Windows version resource.
Native compilation outputs live outside this source tree in
`build/panini-sandbox`. The Windows CLI build bundles this local runtime.

PANINI's execution broker lives in `../agent/execution.py`. It supplies explicit
filesystem, environment and network policy for commands, project hooks and
terminals; requires native enforcement capabilities; and refuses host fallback.
Execution copies the build outside the writable workspace before use.

Unused SDKs, samples, pipelines and standalone tools were removed from this
active source tree into `build/unused-panini-sandbox` at the repository root.
Automatic approval review blocked permanent deletion, so those files remain
recoverable outside the production source.

See the repository's `docs/SANDBOX.md` for supported policies, tests and limits.
