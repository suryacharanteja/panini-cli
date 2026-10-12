# Publishing PANINI on npm

The npm package source is in `npm/`. It wraps the released Windows x64 EXE;
it does not rewrite the Python engine in JavaScript. The public beta is
[panini-cli@2.1.1-beta.1](https://www.npmjs.com/package/panini-cli/v/2.1.1-beta.1),
published on 2026-10-12 with the `beta` tag.

## Install the beta

Requires Windows x64 and Node.js 22.14 or newer:

```powershell
npx.cmd panini-cli@beta --version
npx.cmd panini-cli@beta
```

Or install globally with `npm.cmd install --global panini-cli@beta`, then run
`panini-cli.cmd` or `panini.cmd`. The `.cmd` shims avoid PowerShell execution
policy errors. Python is bundled; project tools such as Git and Node are
installed separately.

The first beta was downloaded from the public registry and matched the prepared
tarball checksum. A fresh global installation, `npx`, both command aliases,
help, native argument-error exit codes and MXC sandbox status passed on the
supported Windows development host.

## Prepare the package

From the repository root, with Node.js 22.14 or newer:

```powershell
node .\scripts\prepare-npm.cjs
npm.cmd pack .\build\npm\package --ignore-scripts --pack-destination .\build\npm
```

This produces `build/npm/panini-cli-2.1.1-beta.1.tgz`. Preparation checks the EXE
against the released SHA256. The tarball contains the launcher, EXE, package
metadata, README, MIT license and third-party notices. The source repository,
credentials, tests and development files are excluded.

Python's `2.1.1b1` becomes npm's SemVer `2.1.1-beta.1`. The `beta` tag keeps this
prerelease separate from a future stable `latest` release.

## Test before publishing

Use a temporary global prefix inside the workspace, avoiding your existing CLI:

```powershell
npm.cmd install --global .\build\npm\panini-cli-2.1.1-beta.1.tgz --prefix .\build\npm\uat --ignore-scripts --offline --no-audit --no-fund
.\build\npm\uat\panini-cli.cmd --version
.\build\npm\uat\panini.cmd --version
.\build\npm\uat\panini-cli.cmd sandbox status
```

## Publish a prepared release

Create an account on [npmjs.com](https://www.npmjs.com/signup), enable publishing
2FA, and complete npm's browser sign-in on this PC:

```powershell
npm.cmd login --registry=https://registry.npmjs.org
npm.cmd whoami
npm.cmd publish .\build\npm\panini-cli-2.1.1-beta.1.tgz --access public --tag beta
npm.cmd view panini-cli@beta version
npx.cmd panini-cli@beta --version
```

Complete npm's authentication/2FA prompt when publishing. Each package version
can be published only once, so increment the version before the next release.
Publish the reviewed tarball, rather than the source `npm/` directory, which
does not contain the native runtime.

After npm finishes processing the upload, verify the registry version,
tarball checksum and a fresh registry installation. The initial release
supports Windows x64; other platforms need native artifacts and separate
packaging validation.

## Subsequent releases

Update the npm version, native release tag and verified EXE checksum in
`npm/package.json` together, then rebuild and test the tarball. Publish beta
versions with `--tag beta`; publish a stable version with `--tag latest` only
when its release checks are complete.

For repeat releases, npm's
[trusted publishing](https://docs.npmjs.com/trusted-publishers/) supports GitHub
Actions OIDC and automatically attaches provenance. Configure the package's
trusted publisher for `suryacharanteja/panini-cli` and an
explicit publishing workflow. That workflow needs `id-token: write`, a
GitHub-hosted runner, Node.js 22.14+ and npm 11.5.1+. No long-lived npm token is
needed for that flow. Automatic publication is not configured in this repository.

See npm's [publishing guide](https://docs.npmjs.com/creating-and-publishing-unscoped-public-packages/)
and [package metadata reference](https://docs.npmjs.com/cli/v11/configuring-npm/package-json/).
