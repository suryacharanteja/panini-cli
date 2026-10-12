#!/usr/bin/env node
'use strict';

const { createHash } = require('node:crypto');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const { spawnSync } = require('node:child_process');
const { constants } = require('node:os');
const metadata = require('../package.json');

try {
  if (process.platform !== 'win32' || process.arch !== 'x64') {
    throw new Error('This release supports Windows x64. See the GitHub repository for other platforms.');
  }
  const executable = join(__dirname, '..', 'runtime', 'panini-cli.exe');
  let bytes;
  try {
    bytes = readFileSync(executable);
  } catch {
    throw new Error('The bundled executable is missing or unreadable. Reinstall panini-cli.');
  }
  if (createHash('sha256').update(bytes).digest('hex') !== metadata.panini.executableSHA256) {
    throw new Error('The bundled executable failed its checksum check. Reinstall panini-cli.');
  }
  const result = spawnSync(executable, process.argv.slice(2), { stdio: 'inherit', shell: false });
  if (result.error) throw result.error;
  process.exitCode = result.status ?? (result.signal ? 128 + (constants.signals[result.signal] || 0) : 1);
} catch (error) {
  console.error(`PANINI CLI: ${error.message}`);
  process.exitCode = 1;
}
