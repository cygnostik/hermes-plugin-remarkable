import test from 'node:test';
import assert from 'node:assert/strict';
import { createCloudService } from '../sidecar/cloud-service.mjs';

import { mkdtemp, readFile, writeFile, readdir, rm } from 'node:fs/promises';
import { join } from 'node:path';

async function scratch(t) {
  assert.ok(process.env.TMPDIR, 'Hermes scratch TMPDIR must be set');
  const dir = await mkdtemp(join(process.env.TMPDIR, 'rm-sidecar-test-'));
  t.after(() => rm(dir, { recursive: true, force: true }));
  return dir;
}

test('download publishes whole original bytes atomically without overwrite and supports archives', async t => {
  const dir = await scratch(t);
  const f = fixture([{ id: 'a', name: 'PDF', files: ['pdf', 'epub'] }, { id: 'n', name: 'Notebook' }]);
  f.api.getEpub = async ref => f.api.raw.getHash({ id: `${ref.id}.epub`, hash: 'epub-ref' });
  f.api.getPdf = async ref => f.api.raw.getHash({ id: `${ref.id}.pdf`, hash: 'pdf-ref' });
  f.api.getDocumentArchive = async ref => {
    const { default: JSZip } = await import('../sidecar/node_modules/jszip/lib/index.js');
    const zip = new JSZip();
    for (const e of (await f.api.raw.getEntries(ref)).entries) zip.file(e.id, await f.api.raw.getHash(e));
    return zip.generateAsync({ type: 'uint8array' });
  };
  const outPath = join(dir, 'original.epub');
  await writeFile(outPath, 'keep me');
  await assert.rejects(f.service.download({ id: 'a', outPath, format: 'original' }), { code: 'EXISTS' });
  assert.equal(await readFile(outPath, 'utf8'), 'keep me');
  const result = await f.service.download({ id: 'a', outPath, format: 'original', overwrite: true });
  assert.equal(result.kind, 'epub');
  assert.equal(await readFile(outPath, 'utf8'), 'original fixture');
  await assert.rejects(f.service.download({ id: 'n', outPath: join(dir, 'n.pdf'), format: 'original' }), { code: 'NO_ORIGINAL' });
  const archived = await f.service.download({ id: 'n', outPath: join(dir, 'n.zip'), format: 'archive' });
  assert.equal(archived.kind, 'archive');
  assert.equal((await readFile(archived.written)).subarray(0, 2).toString(), 'PK');
  const raced = await Promise.allSettled([1, 2].map(() => f.service.download({ id: 'a', outPath: join(dir, 'race.epub'), format: 'original' })));
  assert.equal(raced.filter(x => x.status === 'fulfilled').length, 1);
  assert.equal(raced.find(x => x.status === 'rejected').reason.code, 'EXISTS');
  assert.deepEqual((await readdir(dir)).sort(), ['n.zip', 'original.epub', 'race.epub']);
});

import { storageFixture } from './cloud-storage-fixture.mjs';

test('mkdir and PDF/EPUB creation target a validated folder and verify committed metadata', async t => {
  const f = await storageFixture();
  const service = createCloudService({ getApi: async () => f.api });
  const parent = await service.mkdir({ name: 'Scratch', parent: '' });
  assert.equal(parent.verified, true);
  assert.equal(parent.entry.name, 'Scratch');
  const dir = await scratch(t);
  const path = join(dir, 'source.pdf');
  await writeFile(path, '%PDF-1.7\nfixture');
  const pdf = await service.upload_pdf({ path, name: 'Paper', parent: parent.id });
  assert.equal(pdf.entry.parent, parent.id);
  assert.equal(pdf.entry.type, 'file:pdf');
  assert.equal(pdf.verified, true);
  const { default: JSZip } = await import('../sidecar/node_modules/jszip/lib/index.js');
  const zip = new JSZip(); zip.file('mimetype', 'application/epub+zip'); zip.file('META-INF/container.xml', '<container/>');
  await writeFile(path, await zip.generateAsync({ type: 'nodebuffer' }));
  const epub = await service.upload_epub({ path, name: 'Book', parent: parent.id });
  assert.equal(epub.entry.type, 'file:epub');
  assert.equal(epub.entry.parent, parent.id);
  const before = f.calls.commits;
  await assert.rejects(service.mkdir({ name: 'Wrong', parent: pdf.id }), { code: 'NOT_FOLDER' });
  await assert.rejects(service.upload_pdf({ path, name: 'Wrong', parent: '' }), { code: 'INVALID_FILE' });
  assert.equal(f.calls.commits, before);
  f.calls.ignoreCommit = true;
  await assert.rejects(service.mkdir({ name: 'Invisible', parent: '' }), { code: 'VERIFY_FAILED' });
});

test('manage preserves unknown metadata, uses version CAS, and supports reversible trash only', async () => {
  const f = await storageFixture();
  const service = createCloudService({ getApi: async () => f.api });
  const folder = await service.mkdir({ name: 'Parent' });
  const putMetadata = f.raw.putMetadata;
  f.raw.putMetadata = (id, md) => putMetadata(id, { ...md, futureFirmwareField: { keep: ['exactly', 7] } });
  const child = await service.mkdir({ name: 'Child', parent: folder.id });
  await assert.rejects(service.manage({ id: child.id, action: 'rename', name: 'Next' }), { code: 'CONFIRM_REQUIRED' });
  await assert.rejects(service.manage({ id: child.id, action: 'rename', name: 'Next', confirm: true, expected_version: 'stale' }), { code: 'VERSION_CONFLICT' });
  await assert.rejects(service.manage({ id: folder.id, action: 'move', parent: child.id, confirm: true }), { code: 'INVALID_PARENT' });
  const renamed = await service.manage({ id: child.id, action: 'rename', name: 'Next', expected_version: child.version, confirm: true });
  assert.equal(renamed.entry.name, 'Next');
  assert.equal(renamed.entry.parent, folder.id);
  assert.notEqual(renamed.version, child.version);
  const metadataRef = (await f.raw.getEntries({ id: child.id, hash: renamed.version })).entries.find(e => e.id.endsWith('.metadata'));
  assert.deepEqual(JSON.parse(Buffer.from(await f.raw.getHash(metadataRef))).futureFirmwareField, { keep: ['exactly', 7] });
  const moved = await service.manage({ id: child.id, action: 'move', parent: '', confirm: true });
  assert.equal(moved.entry.parent, '');
  const trashed = await service.manage({ id: child.id, action: 'trash', confirm: true });
  assert.equal(trashed.entry.parent, 'trash');
  assert.equal((await service.list({})).entries.some(x => x.id === child.id), false);
  const restored = await service.manage({ id: child.id, action: 'restore', parent: folder.id, confirm: true });
  assert.equal(restored.entry.parent, folder.id);
  assert.equal(restored.entry.deleted, false);
  await assert.rejects(service.manage({ id: child.id, action: 'delete', confirm: true }), { code: 'BAD_ARGS' });
});

test('IR-1: concurrent move cannot commit a cycle from a later root', async () => {
  const f = await storageFixture();
  const service = createCloudService({ getApi: async () => f.api });
  const a = await service.mkdir({ name: 'A' });
  const b = await service.mkdir({ name: 'B' });
  const getRoot = f.raw.getRootHash;
  let reads = 0, injected = false;
  const commits = f.calls.commits;
  // Exact independent-review interleaving: B moves under A immediately before
  // the outer A -> B operation acquires its third (commit) root snapshot.
  f.raw.getRootHash = async (...args) => {
    if (!injected && ++reads === 3) {
      injected = true;
      await service.manage({ id: b.id, action: 'move', parent: a.id, confirm: true });
    }
    return getRoot(...args);
  };
  await assert.rejects(service.manage({ id: a.id, action: 'move', parent: b.id, confirm: true }), { code: 'VERSION_CONFLICT' });
  assert.equal(injected, true);
  assert.equal(f.calls.commits, commits + 1, 'only the independent move commits; no retry');
  const listing = await service.list({ refresh: true });
  assert.equal(listing.complete, true);
  assert.deepEqual(listing.hierarchyErrors, []);
  assert.equal(listing.entries.find(x => x.id === a.id).parent, '');
  assert.equal(listing.entries.find(x => x.id === b.id).parent, a.id);
});

for (const operation of ['mkdir', 'upload_pdf', 'upload_epub']) {
  for (const trashAncestor of [false, true]) {
    test(`snapshot race: ${operation} rejects ${trashAncestor ? 'ancestor' : 'destination'} trashed after validation`, async t => {
      const f = await storageFixture();
      const service = createCloudService({ getApi: async () => f.api });
      const ancestor = await service.mkdir({ name: 'Ancestor' });
      const destination = await service.mkdir({ name: 'Destination', parent: ancestor.id });
      const otherApi = f.newClient();
      const other = createCloudService({ getApi: async () => otherApi });
      const dir = await scratch(t), path = join(dir, 'source');
      if (operation === 'upload_epub') {
        const { default: JSZip } = await import('../sidecar/node_modules/jszip/lib/index.js');
        const zip = new JSZip();
        zip.file('mimetype', 'application/epub+zip'); zip.file('META-INF/container.xml', '<container/>');
        await writeFile(path, await zip.generateAsync({ type: 'nodebuffer' }));
      } else await writeFile(path, '%PDF-1.7\nfixture');
      const getRoot = f.raw.getRootHash;
      let reads = 0, injected = false;
      const commits = f.calls.commits;
      f.raw.getRootHash = async (...args) => {
        // The high-level put method independently refreshes the root after
        // validateParent. Its new generation must NOT authorize this write.
        if (!injected && ++reads === 2) {
          injected = true;
          await other.manage({ id: trashAncestor ? ancestor.id : destination.id, action: 'trash', confirm: true });
        }
        return getRoot(...args);
      };
      await assert.rejects(service[operation]({ name: 'Must not appear', parent: destination.id, path }), e => {
        assert.equal(e.code, 'VERSION_CONFLICT');
        assert.equal(e.details.retry_safe, false);
        return true;
      });
      assert.equal(injected, true);
      assert.equal(f.calls.commits, commits + 1, 'only the independent trash commits');
      const listing = await service.list({ refresh: true, include_trash: true });
      assert.equal(listing.complete, true);
      assert.equal(listing.entries.length, 2);
      assert.equal(listing.entries.find(x => x.id === destination.id).trashed, true);
    });
  }
}

for (const operation of ['mkdir', 'upload_pdf', 'upload_epub']) {
  test(`shared root cache: ${operation} cannot merge onto another command's newer root`, { timeout: 3000 }, async t => {
    const f = await storageFixture();
    const service = createCloudService({ getApi: async () => f.api });
    const parent = await service.mkdir({ name: 'Parent' });
    const otherApi = f.newClient();
    const other = createCloudService({ getApi: async () => otherApi });
    const dir = await scratch(t), path = join(dir, 'source');
    if (operation === 'upload_epub') {
      const { default: JSZip } = await import('../sidecar/node_modules/jszip/lib/index.js');
      const zip = new JSZip();
      zip.file('mimetype', 'application/epub+zip'); zip.file('META-INF/container.xml', '<container/>');
      await writeFile(path, await zip.generateAsync({ type: 'nodebuffer' }));
    } else await writeFile(path, '%PDF-1.7\nfixture');
    const started = Promise.withResolvers(), resume = Promise.withResolvers();
    t.after(() => resume.resolve());
    const putContent = f.raw.putContent;
    f.raw.putContent = async (...args) => {
      started.resolve();
      await resume.promise;
      return putContent(...args);
    };
    const commits = f.calls.commits;
    const rejected = assert.rejects(service[operation]({ name: 'Must not appear', parent: parent.id, path }), { code: 'VERSION_CONFLICT' });
    await started.promise;
    await other.manage({ id: parent.id, action: 'trash', confirm: true });
    // Independent async command updates the SAME rmapi-js instance's private
    // cached root while its high-level upload is paused after validation.
    await service.list({ refresh: true });
    resume.resolve();
    await rejected;
    assert.equal(f.calls.commits, commits + 1, 'stale merge blocked before CAS, no retry');
    const listing = await service.list({ refresh: true, include_trash: true });
    assert.equal(listing.complete, true);
    assert.equal(listing.entries.length, 1);
    assert.equal(listing.entries[0].trashed, true);
  });
}

test('late concurrent move still fails the server generation CAS without a retry', async () => {
  const f = await storageFixture();
  const service = createCloudService({ getApi: async () => f.api });
  const a = await service.mkdir({ name: 'A' }), b = await service.mkdir({ name: 'B' });
  const otherApi = f.newClient();
  const other = createCloudService({ getApi: async () => otherApi });
  const putRoot = f.raw.putRootHash;
  let injected = false;
  const commits = f.calls.commits;
  f.raw.putRootHash = async (...args) => {
    if (!injected) {
      injected = true;
      await other.manage({ id: b.id, action: 'move', parent: a.id, confirm: true });
    }
    return putRoot(...args);
  };
  await assert.rejects(service.manage({ id: a.id, action: 'move', parent: b.id, confirm: true }), { code: 'VERSION_CONFLICT' });
  assert.equal(f.calls.commits, commits + 2, 'independent success and one rejected CAS');
  const listing = await service.list({ refresh: true });
  assert.equal(listing.complete, true);
  assert.deepEqual(listing.hierarchyErrors, []);
  assert.equal(listing.entries.find(x => x.id === a.id).parent, '');
  assert.equal(listing.entries.find(x => x.id === b.id).parent, a.id);
});

test('snapshot guard prevents upstream generation retries even when configured', async t => {
  // rmapi-js retry backoff uses an unref timer; keep this test alive so RED
  // reaches the commit-count assertion rather than cancelling in backoff.
  const keepAlive = setInterval(() => {}, 1000);
  t.after(() => clearInterval(keepAlive));
  const f = await storageFixture({ maxGenerationRetries: 1 });
  const service = createCloudService({ getApi: async () => f.api });
  f.calls.conflict = true;
  await assert.rejects(service.mkdir({ name: 'Do not retry' }), { code: 'VERSION_CONFLICT' });
  assert.equal(f.calls.commits, 1, 'the high-level rmapi-js method must not replay the root commit');
});

test('write conflicts and lost acknowledgements do not blindly retry', async () => {
  const f = await storageFixture();
  const service = createCloudService({ getApi: async () => f.api });
  const item = await service.mkdir({ name: 'Before' });
  let before = f.calls.commits;
  f.calls.conflict = true;
  await assert.rejects(service.manage({ id: item.id, action: 'rename', name: 'After', confirm: true }), { code: 'VERSION_CONFLICT' });
  assert.equal(f.calls.commits, before + 1);
  f.calls.conflict = false; f.calls.loseResponse = true;
  before = f.calls.commits;
  await assert.rejects(service.manage({ id: item.id, action: 'rename', name: 'After', confirm: true }), e => {
    assert.equal(e.code, 'WRITE_OUTCOME_UNKNOWN');
    assert.equal(e.details.retry_safe, false);
    assert.equal(e.details.item_id, item.id);
    return true;
  });
  assert.equal(f.calls.commits, before + 1);
  assert.equal((await service.list({ refresh: true })).entries[0].name, 'After');
  before = f.calls.commits;
  await assert.rejects(service.mkdir({ name: 'Ambiguous' }), { code: 'WRITE_OUTCOME_UNKNOWN' });
  assert.equal(f.calls.commits, before + 1);
});

test('changes compares provided immutable refs, including additions modifications and removals', async () => {
  const f = fixture([{ id: 'a', name: 'Alpha' }, { id: 'b', name: 'Beta' }]);
  const initial = await f.service.changes({ baseline: null });
  assert.equal(initial.complete, true);
  assert.deepEqual(initial.added, ['a', 'b']);
  assert.deepEqual(initial.modified, []);
  f.records.get('a').hash = 'new-api-ref';
  f.records.delete('b');
  f.records.set('c', { id: 'c', name: 'Gamma' });
  const diff = await f.service.changes({ baseline: initial.snapshot });
  assert.deepEqual(diff.added, ['c']);
  assert.deepEqual(diff.modified, ['a']);
  assert.deepEqual(diff.removed, ['b']);
  assert.deepEqual(diff.changed, ['a', 'b', 'c']);
  assert.equal(diff.snapshot.refs.find(x => x.id === 'a').version, 'new-api-ref');
  await assert.rejects(f.service.changes({ baseline: { bogus: true } }), { code: 'BAD_ARGS' });
  assert.deepEqual(f.calls.refs, [true, true]);
});

test('disk cache is scoped by API device identity and never freezes missing refs', async t => {
  const dir = await scratch(t);
  const f = fixture([{ id: 'a', name: 'Alpha' }, { id: 'b', name: 'Beta', fail: true }]);
  f.api.deviceId = 'fixture-account';
  const first = createCloudService({ getApi: async () => f.api, cacheDir: dir });
  assert.equal((await first.list({})).partial, true);
  assert.ok((await readdir(dir)).includes('metadata-cache.json'));
  f.records.get('b').fail = false;
  const reads = f.calls.reads;
  const next = createCloudService({ getApi: async () => f.api, cacheDir: dir });
  assert.equal((await next.list({})).entries.length, 2);
  assert.equal(f.calls.reads - reads, 1);
  f.api.deviceId = 'other-fixture-account';
  f.records.get('a').name = 'Other';
  const other = createCloudService({ getApi: async () => f.api, cacheDir: dir });
  assert.equal((await other.list({})).entries[0].name, 'Other');
});

test('cursor rejects recovered coverage rather than silently skipping a newly readable item', async () => {
  const f = fixture([{ id: 'a', name: 'A', fail: true }, { id: 'b', name: 'B' }, { id: 'c', name: 'C' }]);
  const first = await f.service.list({ limit: 1 });
  assert.equal(first.partial, true);
  f.records.get('a').fail = false;
  await assert.rejects(f.service.list({ limit: 1, cursor: first.next_cursor }), { code: 'STALE_CURSOR' });
});

test('strict boolean arguments cannot accidentally include trash', async () => {
  const f = fixture([]);
  await assert.rejects(f.service.list({ include_trash: 'false' }), { code: 'BAD_ARGS' });
  await assert.rejects(f.service.list({ refresh: 'true' }), { code: 'BAD_ARGS' });
});

test('write verification bypasses raw cache and refuses a mismatched uploaded file kind', async t => {
  const f = await storageFixture();
  let clears = 0;
  f.raw.clearCache = () => { clears++; };
  const service = createCloudService({ getApi: async () => f.api });
  await service.mkdir({ name: 'Readback' });
  assert.equal(clears, 1);
  const dir = await scratch(t), path = join(dir, 'source.pdf');
  await writeFile(path, '%PDF-1.7\nfixture');
  const putFile = f.raw.putFile;
  f.raw.putFile = (id, data) => putFile(id.replace(/\.pdf$/, '.epub'), data);
  await assert.rejects(service.upload_pdf({ name: 'Wrong kind', path }), { code: 'VERIFY_FAILED' });
});

const bytes = value => Buffer.from(JSON.stringify(value));
function fixture(items) {
  const records = new Map(items.map(item => [item.id, structuredClone(item)]));
  const calls = { refs: [], reads: 0 };
  const api = {
    async listRefs(refresh) { calls.refs.push(refresh); return [...records.values()].map(x => ({ id: x.id, hash: x.hash ?? `ref-${x.id}` })); },
    raw: {
      async getEntries(ref) {
        calls.reads++;
        const item = records.get(ref.id);
        if (item.fail) throw new Error('metadata unavailable');
        return { entries: ['metadata', 'content', ...(item.files ?? [])].map(ext => ({ id: `${ref.id}.${ext}`, hash: `${ref.hash}-${ext}` })) };
      },
      async getHash(ref) {
        const ext = ref.id.split('.').pop();
        const item = records.get(ref.id.slice(0, -(ext.length + 1)));
        if (ext === 'metadata') return bytes({ visibleName: item.name, type: item.type ?? 'DocumentType', parent: item.parent ?? '', pinned: false, ...(item.metadata ?? {}) });
        if (ext === 'content') {
          if (item.badContent) return Buffer.from('{');
          return bytes({ fileType: item.kind ?? 'notebook', tags: item.tags ?? null, ...(item.content ?? {}) });
        }
        return Buffer.from('original fixture');
      },
      clearCache() { calls.clears = (calls.clears ?? 0) + 1; },
    },
  };
  return { api, records, calls, service: createCloudService({ getApi: async () => api }) };
}

test('search includes name and normalized tags, pagination is stable and trash is inherited', async () => {
  const f = fixture([
    { id: 'c', name: 'Gamma', tags: [{ name: 'Work' }, { tag: 'Legacy' }, 'plain'], files: ['pdf'] },
    { id: 'a', name: 'Work Alpha', tags: null },
    { id: 'b', name: 'Beta', tags: [{ name: 'work' }] },
    { id: 't', name: 'Deleted Folder', type: 'CollectionType', parent: 'trash' },
    { id: 'd', name: 'Work Hidden', parent: 't' },
  ]);
  const first = await f.service.list({ query: 'WORK', limit: 2 });
  assert.deepEqual(first.entries.map(x => x.id), ['a', 'b']);
  assert.equal(first.matched, 3);
  assert.equal(first.truncated, true);
  assert.equal(first.code, 'TRUNCATED');
  assert.equal(first.next_offset, 2);
  assert.ok(first.next_cursor);
  const last = await f.service.list({ query: 'WORK', limit: 2, cursor: first.next_cursor });
  assert.deepEqual(last.entries.map(x => x.id), ['c']);
  assert.equal(last.entries[0].type, 'file:pdf');
  assert.equal(last.next_cursor, null);
  assert.deepEqual((await f.service.list({ query: 'legacy', include_tags: true })).entries[0].tags, ['Work', 'Legacy', 'plain']);
  assert.equal((await f.service.list({ include_trash: true, parent: 't' })).entries[0].trashed, true);
  f.records.get('a').hash = 'updated-ref';
  await assert.rejects(f.service.list({ query: 'WORK', cursor: first.next_cursor }), { code: 'STALE_CURSOR' });
  await assert.rejects(f.service.list({ offset: -1 }), { code: 'BAD_ARGS' });
});

test('tag failures disclose incomplete search coverage and are not cached', async () => {
  const f = fixture([{ id: 'a', name: 'Alpha', badContent: true }]);
  const result = await f.service.list({ include_tags: true });
  assert.equal(result.partial, true);
  assert.equal(result.entries.length, 1);
  assert.deepEqual(result.tagErrors, ['a']);
  f.records.get('a').badContent = false;
  f.records.get('a').tags = [{ name: 'fresh' }];
  assert.equal((await f.service.list({ query: 'fresh' })).entries.length, 1);
});

test('listing refresh bypasses successful metadata cache and retries failed refs', async () => {
  const f = fixture([{ id: 'a', name: 'Alpha' }, { id: 'b', name: 'Beta', fail: true }]);
  const first = await f.service.list({});
  assert.equal(first.partial, true);
  assert.equal(first.complete, false);
  assert.equal(first.code, 'PARTIAL_COVERAGE');
  assert.equal(first.coverage.failed, 1);
  assert.deepEqual(first.failedIds, ['b']);
  f.records.get('b').fail = false;
  const second = await f.service.list({});
  assert.equal(second.entries.length, 2);
  assert.equal(second.partial, false);
  f.records.get('a').name = 'Fresh';
  const refreshed = await f.service.list({ refresh: true });
  assert.equal(refreshed.entries.find(x => x.id === 'a').name, 'Fresh');
  assert.deepEqual(f.calls.refs, [true, true, true]);
  assert.equal(f.calls.clears, 1);
});
