# PANINI CLI

A terminal coding agent with a bundled Windows sandbox. This package runs the
same executable as the [PANINI Windows beta](https://github.com/suryacharanteja/panini-cli/releases/tag/v2.1.1b1).

Requires **Windows x64** and **Node.js 22.14 or newer**. Python is included in the
executable. Git, Node and other tools used by your projects are installed separately.

```powershell
npx panini-cli@beta --version
npx panini-cli@beta
```

Or install globally:

```powershell
npm install --global panini-cli@beta
panini-cli --version
panini-cli auth login
panini-cli
```

`panini` is also available as a command alias. If PowerShell blocks `.ps1` shims,
use `panini-cli.cmd` or `panini.cmd`.

```powershell
panini-cli sandbox status
panini-cli sandbox uat --features --web
panini-cli app-server --stdio
```

The package includes the EXE and its license notices. It has no npm dependencies
or installation scripts and does not download another runtime after installation.
The launcher verifies the executable checksum, forwards arguments directly,
and preserves the current directory, terminal streams and native exit code.

The beta executable is unsigned. Windows MXC requires a supported Windows host;
its capability check fails closed. Read the
[sandbox limits](https://github.com/suryacharanteja/panini-cli/blob/main/docs/SANDBOX.md)
and [security model](https://github.com/suryacharanteja/panini-cli/blob/main/SECURITY.md).
Linux and macOS native npm releases are not included in this beta.

[Source and documentation](https://github.com/suryacharanteja/panini-cli)
· [Report an issue](https://github.com/suryacharanteja/panini-cli/issues)
