import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, readFile, rm, link, readdir, stat, chmod } from 'node:fs/promises';
import { join } from 'node:path';
import { createHandlers } from '../sidecar/sidecar.mjs';
import { createSessionProvider } from '../sidecar/session-provider.mjs';
import { spawnSync } from 'node:child_process';
import { requestContext } from '../sidecar/request-context.mjs';
import { storageFixture } from './cloud-storage-fixture.mjs';

const jwt = exp => `synthetic.${Buffer.from(JSON.stringify({ exp, 'device-id': 'synthetic-device' })).toString('base64url')}.synthetic`;
async function fixture(t) {
  assert.ok(process.env.TMPDIR, 'Use Hermes scratch only');
  const dir = await mkdtemp(join(process.env.TMPDIR, 'rm-session-test-'));
  t.after(() => rm(dir, { recursive: true, force: true }));
  const credentials = join(dir, 'credentials');
  await mkdir(credentials, { mode: 0o700 });
  const tokenFile = join(credentials, 'device-token');
  await writeFile(tokenFile, 'synthetic-device-A', { mode: 0o600 });
  return { dir, tokenFile, sessionFile: `${tokenFile}.session.json`, cacheDir: join(dir, 'metadata') };
}

test('independent handlers reuse authentication without replaying failed cloud requests', async t => {
  const f = await fixture(t);
  let exchanges = 0, cloudCalls = 0;
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });
  globalThis.fetch = async url => {
    if (String(url).endsWith('/token/json/2/user/new')) {
      exchanges++;
      return new Response(jwt(Math.floor(Date.now() / 1000) + 3600));
    }
    cloudCalls++;
    return new Response('offline fixture unavailable', { status: 503 });
  };
  for (let i = 0; i < 2; i++) {
    const status = await createHandlers(f).status();
    assert.equal(status.cloud_code, 'HTTP_503');
  }
  assert.equal(exchanges, 1, 'one exchange must serve independent handler instances');
  assert.equal(cloudCalls, 2, 'cloud requests are not transparently replayed');
});

test('session refresh starts at the expiry margin, including an existing handler', async t => {
  const f = await fixture(t);
  let now = 1_700_000_000_000, exchanges = 0;
  const options = { ...f, now: () => now,
    auth: async () => { exchanges++; return jwt(now / 1000 + 3600); },
    session: () => ({ listRefs: async () => [] }),
  };
  const handler = createHandlers(options);
  assert.equal((await handler.status()).cloud, 'ok');
  now += 3539_000;
  assert.equal((await createHandlers(options).status()).cloud, 'ok');
  assert.equal(exchanges, 1);
  now += 1000;
  assert.equal((await handler.status()).cloud, 'ok');
  assert.equal(exchanges, 2, 'refresh at exactly 60 seconds remaining');
  assert.equal((await createHandlers(options).status()).cloud, 'ok');
  assert.equal(exchanges, 2);
});

const fixedNow = 1_700_000_000_000;
const goodRecord = () => ({ version: 1, deviceToken: 'synthetic-device-A', sessionToken: jwt(fixedNow / 1000 + 3600) });
for (const [name, contents] of [
  ['malformed JSON', () => '{broken'],
  ['null record', () => 'null'],
  ['unknown version', () => JSON.stringify({ ...goodRecord(), version: 99 })],
  ['missing token', () => JSON.stringify({ version: 1, deviceToken: 'synthetic-device-A' })],
  ['non-JWT token', () => JSON.stringify({ ...goodRecord(), sessionToken: 'not-a-jwt' })],
  ['string expiry', () => JSON.stringify({ ...goodRecord(), sessionToken: jwt(String(fixedNow / 1000 + 3600)) })],
  ['missing expiry', () => JSON.stringify({ ...goodRecord(), sessionToken: jwt(undefined) })],
  ['expired token', () => JSON.stringify({ ...goodRecord(), sessionToken: jwt(fixedNow / 1000 - 1) })],
  ['oversized record', () => JSON.stringify({ ...goodRecord(), padding: 'x'.repeat(65536) })],
  ['different exact device identity', () => JSON.stringify({ ...goodRecord(), deviceToken: 'synthetic-device-B' })],
]) {
  test(`unusable cached credential refreshes once: ${name}`, async t => {
    const f = await fixture(t);
    await writeFile(f.sessionFile, contents(), { mode: 0o600 });
    let exchanges = 0;
    const getApi = createSessionProvider({ tokenFile: f.tokenFile, now: () => fixedNow,
      auth: async () => { exchanges++; return jwt(fixedNow / 1000 + 7200); },
      session: () => ({ ready: true }),
    });
    assert.equal((await getApi('synthetic-device-A')).ready, true);
    assert.equal(exchanges, 1);
    await getApi('synthetic-device-A');
    assert.equal(exchanges, 1);
  });
}

test('atomic credential replacement does not modify a linked old file', async t => {
  const f = await fixture(t);
  const original = JSON.stringify(goodRecord());
  await writeFile(f.sessionFile, original, { mode: 0o600 });
  const alias = join(f.dir, 'old-credential-link');
  await link(f.sessionFile, alias);
  let exchanges = 0;
  const getApi = createSessionProvider({ tokenFile: f.tokenFile, now: () => fixedNow,
    auth: async () => { exchanges++; return jwt(fixedNow / 1000 + 7200); },
    session: () => ({ ready: true }),
  });
  await getApi('synthetic-device-A');
  assert.equal(exchanges, 1, 'linked cache must not be trusted');
  assert.equal(await readFile(alias, 'utf8'), original, 'replacement must not overwrite old inode');
  assert.deepEqual((await readdir(join(f.dir, 'credentials'))).sort(), ['device-token', 'device-token.session.json']);
  if (process.platform !== 'win32') assert.equal((await stat(f.sessionFile)).mode & 0o777, 0o600);
});

test('POSIX credentials directory with broad permissions fails closed', { skip: process.platform === 'win32' }, async t => {
  const f = await fixture(t);
  await chmod(join(f.dir, 'credentials'), 0o755);
  let exchanges = 0;
  const getApi = createSessionProvider({ tokenFile: f.tokenFile,
    auth: async () => { exchanges++; return jwt(fixedNow / 1000 + 7200); },
    session: () => ({ ready: true }),
  });
  await assert.rejects(getApi('synthetic-device-A'), { code: 'EACCES' });
  assert.equal(exchanges, 0);
});

for (const [name, token] of [
  ['missing expiry', jwt(undefined)],
  ['expired', jwt(fixedNow / 1000 - 1)],
  ['within margin', jwt(fixedNow / 1000 + 60)],
  ['oversized', `synthetic.${'x'.repeat(17000)}.synthetic`],
]) {
  test(`invalid fresh auth response is not persisted or retried: ${name}`, async t => {
    const f = await fixture(t);
    let exchanges = 0, sessions = 0;
    const getApi = createSessionProvider({ tokenFile: f.tokenFile, now: () => fixedNow,
      auth: async () => { exchanges++; return token; },
      session: () => { sessions++; return {}; },
    });
    await assert.rejects(getApi('synthetic-device-A'), e => e.status === 401 && !e.message.includes(token));
    assert.equal(exchanges, 1);
    assert.equal(sessions, 0);
    assert.deepEqual(await readdir(join(f.dir, 'credentials')), ['device-token']);
  });
}

test('cached token rejected by rmapi session parsing gets one fresh exchange', async t => {
  const f = await fixture(t);
  const stale = goodRecord();
  await writeFile(f.sessionFile, JSON.stringify(stale), { mode: 0o600 });
  let exchanges = 0;
  const getApi = createSessionProvider({ tokenFile: f.tokenFile, now: () => fixedNow,
    auth: async () => { exchanges++; return jwt(fixedNow / 1000 + 7200); },
    session: token => { if (token === stale.sessionToken) throw new Error('synthetic invalid claims'); return { ready: true }; },
  });
  assert.equal((await getApi('synthetic-device-A')).ready, true);
  assert.equal(exchanges, 1);
});

test('enrollment invalidates even an unchanged device credential', async t => {
  const f = await fixture(t);
  let exchanges = 0;
  const handlers = createHandlers({ ...f, now: () => fixedNow,
    registerDevice: async () => 'synthetic-device-A',
    auth: async () => { exchanges++; return jwt(fixedNow / 1000 + 3600); },
    session: () => ({ listRefs: async () => [] }),
  });
  assert.equal((await handlers.status()).cloud, 'ok');
  assert.equal(exchanges, 1);
  await handlers.enroll({ code: '12345678' });
  assert.equal((await handlers.status()).cloud, 'ok');
  assert.equal(exchanges, 2);
});

test('device identity changing during exchange fails before publishing a session', async t => {
  const f = await fixture(t);
  const getApi = createSessionProvider({ tokenFile: f.tokenFile, now: () => fixedNow,
    auth: async () => { await writeFile(f.tokenFile, 'synthetic-device-B'); return jwt(fixedNow / 1000 + 3600); },
    session: () => ({ ready: true }),
  });
  await assert.rejects(getApi('synthetic-device-A'), { status: 401 });
  assert.deepEqual(await readdir(join(f.dir, 'credentials')), ['device-token']);
});

test('actual independent Node processes perform one exchange across two operations', async t => {
  const f = await fixture(t);
  const code = `
    import { createHandlers } from ${JSON.stringify(new URL('../sidecar/sidecar.mjs', import.meta.url).href)};
    let exchanges = 0, cloudCalls = 0;
    globalThis.fetch = async url => {
      if (String(url).endsWith('/token/json/2/user/new')) {
        exchanges++;
        return new Response(${JSON.stringify(jwt(fixedNow / 1000 + 3600))});
      }
      cloudCalls++;
      return new Response('offline synthetic unavailable', { status: 503 });
    };
    const result = await createHandlers({ now: () => ${fixedNow} }).status();
    process.stdout.write(JSON.stringify({ exchanges, cloudCalls, code: result.cloud_code }));
  `;
  const results = [];
  for (let i = 0; i < 2; i++) {
    const proc = spawnSync(process.execPath, ['--input-type=module', '-e', code], {
      encoding: 'utf8', timeout: 10000,
      env: { ...process.env, RMAPI_JS_TOKEN_FILE: f.tokenFile, RMAPI_JS_CACHE_DIR: f.cacheDir },
    });
    assert.equal(proc.status, 0, proc.stderr);
    results.push(JSON.parse(proc.stdout));
  }
  assert.deepEqual(results, [
    { exchanges: 1, cloudCalls: 1, code: 'HTTP_503' },
    { exchanges: 0, cloudCalls: 1, code: 'HTTP_503' },
  ]);
});

test('existing handler sees account changes and never uses cached session when unenrolled', async t => {
  const f = await fixture(t);
  const seen = [];
  const handlers = createHandlers({ ...f, now: () => fixedNow,
    auth: async token => { seen.push(token); return jwt(fixedNow / 1000 + 3600); },
    session: () => ({ listRefs: async () => [] }),
  });
  await handlers.status();
  await writeFile(f.tokenFile, 'synthetic-device-B');
  await handlers.status();
  await writeFile(f.tokenFile, 'synthetic-device-A');
  await handlers.status();
  assert.deepEqual(seen, ['synthetic-device-A', 'synthetic-device-B', 'synthetic-device-A']);
  await rm(f.tokenFile);
  await assert.rejects(handlers.list({}), { code: 'NO_TOKEN' });
  assert.equal(seen.length, 3);
});

test('legacy apiFactory injection keeps device token, retry options and per-identity cache', async t => {
  const f = await fixture(t);
  let calls = 0;
  const handlers = createHandlers({ ...f, apiFactory: async (token, options) => {
    calls++;
    assert.ok(token.startsWith('synthetic-device-'));
    assert.deepEqual(options, { maxGenerationRetries: 0, maxTransientRetries: 0, maxCacheSize: 20000 });
    return { listRefs: async () => [] };
  }});
  await handlers.status(); await handlers.status();
  assert.equal(calls, 1);
  await writeFile(f.tokenFile, 'synthetic-device-B');
  await handlers.status();
  assert.equal(calls, 2);
  assert.deepEqual(await readdir(join(f.dir, 'credentials')), ['device-token']);
});

test('AUTH_FAILED during commit is not replayed or automatically reauthenticated', async t => {
  const f = await fixture(t), storage = await storageFixture();
  let exchanges = 0, commits = 0;
  storage.api.raw.putRootHash = async () => { commits++; throw Object.assign(new Error('synthetic auth refusal'), { status: 401 }); };
  const handlers = createHandlers({ ...f, now: () => fixedNow,
    auth: async () => { exchanges++; return jwt(fixedNow / 1000 + 3600); },
    session: () => storage.api,
  });
  await assert.rejects(handlers.mkdir({ name: 'Synthetic folder' }), e => {
    assert.equal(e.code, 'WRITE_OUTCOME_UNKNOWN');
    assert.equal(e.details.retry_safe, false);
    return true;
  });
  assert.equal(exchanges, 1);
  assert.equal(commits, 1);
});

test('oversized device identity is rejected before reuse or exchange', async t => {
  const f = await fixture(t);
  const deviceToken = 'x'.repeat(17000);
  await writeFile(f.tokenFile, deviceToken);
  await writeFile(f.sessionFile, JSON.stringify({ ...goodRecord(), deviceToken }), { mode: 0o600 });
  let exchanges = 0;
  const getApi = createSessionProvider({ tokenFile: f.tokenFile, now: () => fixedNow,
    auth: async () => { exchanges++; return jwt(fixedNow / 1000 + 3600); },
    session: () => ({ ready: true }),
  });
  await assert.rejects(getApi(deviceToken), { status: 401 });
  assert.equal(exchanges, 0);
});

test('request expiry during auth never publishes temporary credentials', async t => {
  const f = await fixture(t);
  const context = { expired: false };
  const getApi = createSessionProvider({ tokenFile: f.tokenFile, now: () => fixedNow,
    auth: async () => { context.expired = true; return jwt(fixedNow / 1000 + 3600); },
    session: () => ({ ready: true }),
  });
  await assert.rejects(requestContext.run(context, () => getApi('synthetic-device-A')), { code: 'REQUEST_EXPIRED' });
  assert.deepEqual(await readdir(join(f.dir, 'credentials')), ['device-token']);
});
