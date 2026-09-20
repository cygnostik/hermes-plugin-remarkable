import test from 'node:test';
import assert from 'node:assert/strict';
import { createDispatcher } from '../sidecar/protocol.mjs';
import { createCloudService } from '../sidecar/cloud-service.mjs';
import { storageFixture } from './cloud-storage-fixture.mjs';

test('a timed-out preflight cannot later start a cloud write', async () => {
  const f = await storageFixture();
  let release;
  const wait = new Promise(r => { release = r; });
  const original = f.api.listRefs.bind(f.api);
  f.api.listRefs = async refresh => { await wait; return original(refresh); };
  const service = createCloudService({ getApi: async () => f.api });
  const dispatch = createDispatcher(service, { minTimeoutMs: 1 });
  const result = await dispatch('{"id":"slow","op":"mkdir","args":{"name":"Too late"},"timeoutMs":5}');
  assert.equal(result.code, 'WRITE_OUTCOME_UNKNOWN');
  release();
  await new Promise(r => setTimeout(r, 30));
  assert.equal(f.calls.commits, 0);
});

test('JSON-lines dispatcher validates envelopes and always echoes id with object result', async () => {
  const dispatch = createDispatcher({ status: async () => ({ enrolled: false }) });
  for (const line of ['null', '[]', '3', '{', JSON.stringify({ id: 'r', op: '__proto__' }), JSON.stringify({ id: 'r', op: 'status', args: [] })]) {
    const response = await dispatch(line);
    assert.equal(response.ok, false);
    assert.equal(typeof response.result, 'object');
    assert.ok(!Array.isArray(response.result));
    assert.match(response.code, /BAD_REQUEST|BAD_JSON|BAD_OP|BAD_ARGS/);
  }
  assert.deepEqual(await dispatch(JSON.stringify({ id: 7, op: 'status' })), { id: 7, ok: true, result: { enrolled: false } });
});

test('dispatcher executes real cloud handlers and sanitizes third party errors', async () => {
  const f = await storageFixture();
  const service = createCloudService({ getApi: async () => f.api });
  const dispatch = createDispatcher(service);
  const created = await dispatch(JSON.stringify({ id: 'create', op: 'mkdir', args: { name: 'Fixture' } }));
  assert.equal(created.ok, true);
  assert.equal(created.result.verified, true);
  assert.equal((await dispatch(JSON.stringify({ id: 'list', op: 'list' }))).result.entries[0].id, created.result.id);
  const bad = createDispatcher({ status: async () => { throw new Error('Bearer DO-NOT-LEAK'); } });
  assert.ok(!JSON.stringify(await bad('{"id":"error","op":"status"}')).includes('DO-NOT-LEAK'));
});

test('timed-out write is reported ambiguous and subsequent writes stay blocked until restart', async () => {
  let resolve;
  const pending = new Promise(r => { resolve = r; });
  let writes = 0;
  const dispatch = createDispatcher({ mkdir: async () => { writes++; await pending; return { verified: true }; }, status: async () => ({ enrolled: true }) }, { minTimeoutMs: 1 });
  const result = await dispatch('{"id":"slow","op":"mkdir","timeoutMs":5}');
  assert.equal(result.code, 'WRITE_OUTCOME_UNKNOWN');
  assert.equal(result.result.retry_safe, false);
  assert.equal((await dispatch('{"id":"next","op":"mkdir"}')).code, 'WRITE_BLOCKED');
  resolve();
  assert.equal((await dispatch('{"id":"status","op":"status"}')).ok, true);
  assert.equal(writes, 1);
});
