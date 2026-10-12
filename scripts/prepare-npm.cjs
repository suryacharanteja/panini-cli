'use strict';

const { createHash } = require('node:crypto');
const { copyFileSync, mkdirSync, readFileSync, writeFileSync } = require('node:fs');
const { join, resolve } = require('node:path');

const root = resolve(__dirname, '..');
const source = join(root, 'npm');
const metadata = JSON.parse(readFileSync(join(source, 'package.json'), 'utf8'));
const executable = join(root, 'dist', 'panini-cli.exe');
const bytes = readFileSync(executable);
if (createHash('sha256').update(bytes).digest('hex') !== metadata.panini.executableSHA256) {
  throw new Error('dist/panini-cli.exe does not match the pinned release. Review release metadata before packaging.');
}
const destination = join(root, 'build', 'npm', 'package');
const files = [
  [join(source, 'bin', 'panini-cli.cjs'), 'bin/panini-cli.cjs'],
  [join(source, 'README.md'), 'README.md'],
  [join(root, 'LICENSE'), 'LICENSE'],
  [join(root, 'src', 'panini_cli', 'licenses', 'THIRD_PARTY_NOTICES.txt'), 'THIRD_PARTY_NOTICES.txt'],
  [join(root, 'src', 'panini_cli', 'licenses', 'manifest.json'), 'components.json'],
  [executable, 'runtime/panini-cli.exe'],
];
for (const [input, relative] of files) {
  const target = join(destination, relative);
  mkdirSync(resolve(target, '..'), { recursive: true });
  copyFileSync(input, target);
}
writeFileSync(join(destination, 'package.json'), JSON.stringify(metadata, null, 2) + '\n');
console.log(`Prepared ${metadata.name}@${metadata.version}: ${destination}`);
