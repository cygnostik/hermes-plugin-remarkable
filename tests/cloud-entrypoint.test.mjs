import test from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdtemp, rm, readdir } from 'node:fs/promises';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import * as sidecar from '../sidecar/sidecar.mjs';
import { storageFixture } from './cloud-storage-fixture.mjs';

test('sidecar exports injectable handlers without enrollment or import-time writes', async t => {
  assert.equal(typeof sidecar.createHandlers, 'function');
  const dir = await mkdtemp(join(process.env.TMPDIR, 'rm-entry-test-'));
  t.after(() => rm(dir, { recursive: true, force: true }));
  const f = await storageFixture();
  const handlers = sidecar.createHandlers({ getApi: async () => f.api, tokenFile: join(dir, 'missing-token'), cacheDir: join(dir, 'cache') });
  const status = await handlers.status({});
  assert.equal(status.enrolled, false);
  assert.deepEqual(await readdir(dir), []);
  const folder = await handlers.mkdir({ name: 'Injected' });
  assert.equal(folder.verified, true);
  assert.equal((await handlers.list({})).entries[0].name, 'Injected');
});

test('actual stdio CLI returns structured protocol errors and safe unenrolled status', async t => {
  const dir = await mkdtemp(join(process.env.TMPDIR, 'rm-cli-test-'));
  t.after(() => rm(dir, { recursive: true, force: true }));
  const proc = spawnSync(process.execPath, [fileURLToPath(new URL('../sidecar/sidecar.mjs', import.meta.url))], {
    input: '{bad\nnull\n{"id":"status","op":"status"}\n{"id":"list","op":"list"}\n{"id":"prototype","op":"__proto__"}\n',
    encoding: 'utf8', timeout: 10000,
    env: { ...process.env, RMAPI_JS_TOKEN_FILE: join(dir, 'missing-token'), RMAPI_JS_CACHE_DIR: join(dir, 'cache') },
  });
  assert.equal(proc.status, 0, proc.stderr);
  const replies = proc.stdout.trim().split('\n').map(JSON.parse);
  assert.equal(replies.length, 5);
  assert.deepEqual(replies.map(r => r.id), [null, null, 'status', 'list', 'prototype']);
  assert.equal(replies[2].result.enrolled, false);
  assert.equal(replies[3].code, 'NO_TOKEN');
  assert.equal(replies[4].code, 'BAD_OP');
  for (const r of replies) { assert.equal(typeof r.ok, 'boolean'); assert.ok(r.result && typeof r.result === 'object'); }
  assert.deepEqual(await readdir(dir), []);
});
