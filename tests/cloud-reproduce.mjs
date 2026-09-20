// Opt-in clean-lockfile reproduction, separate from offline unit tests.
import { mkdtemp, mkdir, copyFile, readdir, rm } from 'node:fs/promises';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';

if (!process.env.TMPDIR) throw new Error('Hermes scratch TMPDIR required');
const source = fileURLToPath(new URL('../', import.meta.url));
const temp = await mkdtemp(join(process.env.TMPDIR, 'rm-clean-install-'));
try {
  for (const dir of ['sidecar', 'tests']) {
    await mkdir(join(temp, dir));
    for (const name of await readdir(join(source, dir))) {
      if (name.endsWith('.mjs') || (dir === 'sidecar' && ['package.json', 'package-lock.json'].includes(name))) {
        await copyFile(join(source, dir, name), join(temp, dir, name));
      }
    }
  }
  for (const command of ['npm ci --ignore-scripts --no-audit --no-fund', 'npm test']) {
    const result = spawnSync(command, { cwd: join(temp, 'sidecar'), shell: true, encoding: 'utf8', timeout: 180000 });
    console.log(result.stdout);
    if (result.stderr) console.error(result.stderr);
    if (result.status !== 0) throw new Error(`Clean reproduction failed: ${command}`);
  }
  console.log('Clean npm ci and npm test succeeded without vendored node_modules.');
} finally {
  await rm(temp, { recursive: true, force: true });
}
