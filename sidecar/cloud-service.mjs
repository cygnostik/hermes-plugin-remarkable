import { open, mkdir, link, rename, unlink, readFile } from 'node:fs/promises';
import { dirname, basename, join } from 'node:path';
import { randomUUID } from 'node:crypto';
import { isDeepStrictEqual } from 'node:util';
import { AsyncLocalStorage } from 'node:async_hooks';
import { assertRequestActive, guardCommit } from './request-context.mjs';
import { GenerationError, HashNotFoundError } from 'rmapi-js';

// Cloud operations over an injected rmapi-js API. No credentials or I/O at import.
export class CloudError extends Error {
  constructor(code, message, details = {}) { super(message); this.code = code; this.details = details; }
}
const error = (code, message, details) => new CloudError(code, message, details);
const decode = data => JSON.parse(new TextDecoder().decode(data));
const text = (value, field, empty = false) => {
  if (typeof value !== 'string' || (!empty && !value.trim()) || value.includes('\0')) throw error('BAD_ARGS', `${field} must be a ${empty ? '' : 'nonempty '}string`);
  return value;
};
const integer = (value, fallback, min, max) => {
  value ??= fallback;
  if (!Number.isSafeInteger(value) || value < min || value > max) throw error('BAD_ARGS', `Integer must be between ${min} and ${max}`);
  return value;
};
const refPairs = refs => refs.map(r => [r.id, r.hash]).sort((a, b) => a[0].localeCompare(b[0]));
const rootSnapshots = new AsyncLocalStorage();
const snapshotGuarded = new WeakSet();
function withRootSnapshot(api, fn) {
  const raw = api.raw;
  if (!snapshotGuarded.has(raw)) {
    const getRoot = raw.getRootHash, getEntries = raw.getEntries, putRoot = raw.putRootHash;
    const scope = () => {
      const current = rootSnapshots.getStore();
      return current?.raw === raw ? current : undefined;
    };
    const conflict = () => error('VERSION_CONFLICT', 'Library changed since hierarchy validation; inspect fresh metadata before retrying', { retry_safe: false });
    raw.getRootHash = async function (...args) {
      const snapshot = scope();
      const root = await getRoot.apply(this, args);
      if (snapshot) {
        if (snapshot.root && !isDeepStrictEqual(snapshot.root, root)) throw conflict();
        snapshot.root ??= [...root];
      }
      return root;
    };
    raw.getEntries = function (ref, ...args) {
      const snapshot = scope();
      // rmapi-js can use its shared private root cache without another raw
      // getRootHash call. Check the actual immutable root used for the merge.
      if (snapshot && ref.id === 'root' && ref.hash !== snapshot.root?.[0]) throw conflict();
      return getEntries.call(this, ref, ...args);
    };
    raw.putRootHash = async function (hash, generation, ...args) {
      const snapshot = scope();
      if (snapshot && generation !== snapshot.root?.[1]) throw conflict();
      try { return await putRoot.call(this, hash, generation, ...args); }
      catch (cause) {
        // Translate at the raw boundary, before rmapi-js can retry its root
        // merge using stale ancestry. Preserve ambiguous acknowledgements.
        if (snapshot && (cause instanceof GenerationError || cause instanceof HashNotFoundError || [409, 412].includes(cause.status))) throw conflict();
        throw cause;
      }
    };
    snapshotGuarded.add(raw);
  }
  // Async-local state, not temporarily swapped methods: overlapping commands
  // (even on a shared API instance) must never overwrite each other's snapshot.
  return rootSnapshots.run({ raw }, fn);
}
async function mapBounded(items, fn, concurrency = 12) {
  let next = 0;
  const results = new Array(items.length);
  await Promise.all(Array.from({ length: Math.min(items.length, concurrency) }, async () => {
    while (next < items.length) { const i = next++; results[i] = await fn(items[i]); }
  }));
  return results;
}

export function createCloudService({ getApi, cacheDir }) {
  const provider = getApi;
  getApi = async () => guardCommit(await provider());
  const cache = new Map();
  let cacheOwner, diskLoaded = false;
  async function loadDiskCache(owner) {
    if (owner !== cacheOwner) { cache.clear(); diskLoaded = false; cacheOwner = owner; }
    if (diskLoaded || !cacheDir || !owner) return;
    diskLoaded = true;
    try {
      const saved = JSON.parse(await readFile(join(cacheDir, 'metadata-cache.json'), 'utf8'));
      if (saved.schema !== 1 || saved.owner !== owner || !Array.isArray(saved.items)) return;
      for (const read of saved.items) {
        const item = read?.item;
        if (item && ['id', 'version', 'name', 'parent', 'type'].every(k => typeof item[k] === 'string')
          && Array.isArray(item.tags) && item.tags.every(t => typeof t === 'string') && typeof item.deleted === 'boolean'
          && typeof read.tagsLoaded === 'boolean') cache.set(item.id, read);
      }
    } catch { /* Missing/corrupt caches trigger a real walk, never omissions. */ }
  }
  async function saveDiskCache(owner) {
    if (!cacheDir || !owner) return null;
    const temp = join(cacheDir, `.metadata-cache.${randomUUID()}.part`);
    try {
      await mkdir(cacheDir, { recursive: true, mode: 0o700 });
      const handle = await open(temp, 'wx', 0o600);
      try {
        await handle.writeFile(JSON.stringify({ schema: 1, owner, items: [...cache.values()].map(({ item, tagsLoaded }) => ({ item, tagsLoaded })) }));
        await handle.sync();
      } finally { await handle.close(); }
      await rename(temp, join(cacheDir, 'metadata-cache.json'));
      return null;
    } catch { return 'CACHE_WRITE_FAILED'; }
    finally { await unlink(temp).catch(() => {}); }
  }
  async function readItem(api, ref, includeTags = false) {
    const { entries } = await api.raw.getEntries(ref);
    const entry = entries.find(e => e.id === `${ref.id}.metadata`);
    if (!entry) throw error('INVALID_METADATA', 'Item has no metadata');
    const md = decode(await api.raw.getHash(entry));
    if (!md || typeof md.visibleName !== 'string' || typeof md.parent !== 'string' || typeof md.type !== 'string') {
      throw error('INVALID_METADATA', 'Item metadata is incomplete');
    }
    const kind = entries.some(e => e.id.endsWith('.epub')) ? 'epub' : entries.some(e => e.id.endsWith('.pdf')) ? 'pdf' : 'notebook';
    const item = { id: ref.id, version: ref.hash, name: md.visibleName,
      type: md.type === 'CollectionType' ? 'folder' : md.type === 'DocumentType' ? `file:${kind}` : md.type,
      parent: md.parent, pinned: !!md.pinned, tags: [], deleted: !!md.deleted,
      lastModified: md.lastModified ?? null, lastOpened: md.lastOpened ?? null };
    let tagError = false;
    if (includeTags) {
      try {
        const ct = entries.find(e => e.id === `${ref.id}.content`);
        if (!ct && md.type === 'DocumentType') throw error('INVALID_CONTENT', 'Missing document content');
        const content = ct ? decode(await api.raw.getHash(ct)) : {};
        const tags = [...(content.tags ?? []), ...(content.pageTags ?? [])];
        item.tags = [...new Set(tags.map(t => typeof t === 'string' ? t : t?.name ?? t?.tag).filter(t => typeof t === 'string'))];
      } catch { tagError = true; }
    }
    return { item, md, entries, tagsLoaded: includeTags && !tagError, tagError };
  }
  async function findRef(api, id) {
    text(id, 'id');
    const ref = (await api.listRefs(true)).find(ref => ref.id === id);
    if (!ref) throw error('NOT_FOUND', 'Item does not exist', { item_id: id });
    return ref;
  }
  async function validateParent(api, parent, forbiddenId) {
    text(parent, 'parent', true);
    const refs = new Map((await api.listRefs(true)).map(r => [r.id, r]));
    const seen = new Set();
    let id = parent;
    while (id !== '') {
      if (id === 'trash') throw error('INVALID_PARENT', 'Destination is in trash');
      if (id === forbiddenId || seen.has(id)) throw error('INVALID_PARENT', 'Destination would create a folder cycle');
      seen.add(id);
      const ref = refs.get(id);
      if (!ref) throw error('NOT_FOUND', 'Destination folder or ancestor not found');
      const { item } = await readItem(api, ref);
      if (item.type !== 'folder') throw error('NOT_FOLDER', 'Destination must be a folder');
      if (item.deleted) throw error('INVALID_PARENT', 'Destination folder is deleted');
      id = item.parent;
    }
  }
  async function verify(api, ref, expected, expectedKind) {
    // Readback must observe the newly published root, not the pre-write snapshot.
    return rootSnapshots.run(undefined, async () => {
      cache.delete(ref.id);
      try {
        api.raw.clearCache();
        const current = await findRef(api, ref.id);
        const { item, md } = await readItem(api, current);
        if ((expectedKind && item.type !== `file:${expectedKind}`) || current.hash !== ref.hash || Object.entries(expected).some(([key, value]) => !isDeepStrictEqual(md[key], value))) {
          throw error('VERIFY_FAILED', 'Committed item does not match requested metadata');
        }
        return { id: ref.id, name: item.name, parent: item.parent, version: current.hash, entry: item, verified: true };
      } catch (cause) {
        throw error('VERIFY_FAILED', 'Write returned but exact metadata could not be verified; inspect item before retrying', { item_id: ref.id, outcome: 'unknown', retry_safe: false });
      }
    });
  }
  async function writeOnce(fn, details = {}) {
    try { assertRequestActive(); return await fn(); }
    catch (cause) {
      if (cause instanceof CloudError) throw cause;
      if (cause instanceof GenerationError || cause instanceof HashNotFoundError || [409, 412].includes(cause.status)) {
        throw error('VERSION_CONFLICT', 'Library changed; inspect fresh metadata before retrying', { ...details, retry_safe: false });
      }
      throw error('WRITE_OUTCOME_UNKNOWN', 'Write outcome is unknown; inspect the library before any retry', { ...details, outcome: 'unknown', retry_safe: false });
    }
  }
  async function upload(args, kind) {
    text(args.path, 'path'); text(args.name, 'name');
    const parent = args.parent ?? '';
    const data = await readFile(args.path);
    if (kind === 'pdf') {
      if (!data.subarray(0, 1024).includes(Buffer.from('%PDF-'))) throw error('INVALID_FILE', 'File has no PDF header');
    } else {
      try {
        const { default: JSZip } = await import('jszip');
        const zip = await JSZip.loadAsync(data);
        if (await zip.file('mimetype')?.async('string') !== 'application/epub+zip' || !zip.file('META-INF/container.xml')) throw new Error();
      } catch { throw error('INVALID_FILE', 'File is not an EPUB container'); }
    }
    const api = await getApi();
    return withRootSnapshot(api, async () => {
      await validateParent(api, parent);
      const ref = await writeOnce(() => kind === 'pdf' ? api.putPdf(args.name, data, { parent, refresh: true }) : api.putEpub(args.name, data, { parent, refresh: true }), { name: args.name, parent });
      return { ...await verify(api, ref, { visibleName: args.name, parent, type: 'DocumentType' }, kind), bytes: data.length };
    });
  }
  return {
    async changes(args = {}) {
      const baseline = args.baseline ?? undefined;
      if (baseline !== undefined && (!baseline || baseline.schema !== 1 || !Array.isArray(baseline.refs)
        || baseline.refs.some(r => !r || typeof r.id !== 'string' || typeof r.version !== 'string')
        || new Set(baseline.refs.map(r => r.id)).size !== baseline.refs.length)) {
        throw error('BAD_ARGS', 'baseline must be a snapshot returned by changes');
      }
      const refs = await (await getApi()).listRefs(true);
      const snapshot = { schema: 1, refs: refPairs(refs).map(([id, version]) => ({ id, version })) };
      const before = new Map((baseline?.refs ?? []).map(r => [r.id, r.version]));
      const after = new Map(snapshot.refs.map(r => [r.id, r.version]));
      const added = [], modified = [], removed = [];
      for (const [id, version] of after) {
        if (!before.has(id)) added.push(id);
        else if (before.get(id) !== version) modified.push(id);
      }
      for (const id of before.keys()) if (!after.has(id)) removed.push(id);
      removed.sort();
      return { snapshot, added, modified, removed, changed: [...added, ...modified, ...removed].sort(), complete: true };
    },
    async manage(args = {}) {
      text(args.id, 'id');
      if (!['rename', 'move', 'trash', 'restore'].includes(args.action)) throw error('BAD_ARGS', 'Only rename, move, trash and restore are supported');
      if (args.confirm !== true) throw error('CONFIRM_REQUIRED', 'Management requires confirm:true');
      if (args.expected_version !== undefined) text(args.expected_version, 'expected_version');
      if (args.action === 'rename') text(args.name, 'name');
      if (args.action === 'move') text(args.parent, 'parent', true);
      const api = await getApi();
      return withRootSnapshot(api, async () => {
        const ref = await findRef(api, args.id);
        if (args.expected_version !== undefined && args.expected_version !== ref.hash) throw error('VERSION_CONFLICT', 'Item changed since the supplied version');
        const { md, entries } = await readItem(api, ref);
        const update = {};
        if (args.action === 'rename') update.visibleName = args.name;
        if (args.action === 'trash') update.parent = 'trash';
        if (args.action === 'restore' || args.action === 'move') {
          if (args.action === 'move' && (md.parent === 'trash' || md.deleted)) throw error('BAD_ARGS', 'Use restore for a trashed item');
          update.parent = args.parent ?? '';
          await validateParent(api, update.parent, ref.id);
          if (args.action === 'restore') update.deleted = false;
        }
        // Preserve unknown metadata verbatim, rather than round-tripping a strict
        // upstream schema that can silently strip fields introduced by firmware.
        const updated = { ...md, ...update, version: (Number.isSafeInteger(md.version) ? md.version : 0) + 1, metadatamodified: true };
        const nextRef = await writeOnce(async () => {
          const [root, generation, schema] = await api.raw.getRootHash();
          const rootEntries = (await api.raw.getEntries({ id: 'root', hash: root })).entries;
          const index = rootEntries.findIndex(e => e.id === ref.id && e.hash === ref.hash);
          if (index === -1) throw error('VERSION_CONFLICT', 'Item changed before metadata write');
          const mdIndex = entries.findIndex(e => e.id === `${ref.id}.metadata`);
          const pendingMd = await api.raw.putFile(entries[mdIndex].id, Buffer.from(JSON.stringify(updated)));
          await pendingMd[Symbol.asyncDispose]();
          entries[mdIndex] = pendingMd;
          const pendingItem = await api.raw.putEntries(ref.id, entries, schema);
          await pendingItem[Symbol.asyncDispose]();
          rootEntries[index] = pendingItem;
          const pendingRoot = await api.raw.putEntries('root', rootEntries, 4);
          await pendingRoot[Symbol.asyncDispose]();
          await api.raw.putRootHash(pendingRoot.hash, generation);
          return { id: ref.id, hash: pendingItem.hash };
        }, { item_id: ref.id });
        return { ...await verify(api, nextRef, updated), action: args.action };
      });
    },
    async mkdir(args = {}) {
      text(args.name, 'name');
      const parent = args.parent ?? '';
      const api = await getApi();
      return withRootSnapshot(api, async () => {
        await validateParent(api, parent);
        const ref = await writeOnce(() => api.putFolder(args.name, { parent }, true), { name: args.name, parent });
        return verify(api, ref, { visibleName: args.name, parent, type: 'CollectionType' });
      });
    },
    async upload_pdf(args = {}) { return upload(args, 'pdf'); },
    async upload_epub(args = {}) { return upload(args, 'epub'); },
    async download(args = {}) {
      text(args.id, 'id'); text(args.outPath, 'outPath');
      const format = args.format ?? 'original';
      if (!['original', 'archive'].includes(format)) throw error('BAD_ARGS', 'format must be original or archive');
      if (args.overwrite !== undefined && typeof args.overwrite !== 'boolean') throw error('BAD_ARGS', 'overwrite must be boolean');
      const api = await getApi();
      const ref = await findRef(api, args.id);
      const { item } = await readItem(api, ref);
      if (!item.type.startsWith('file:')) throw error('NOT_DOCUMENT', 'Only documents can be downloaded');
      const kind = format === 'archive' ? 'archive' : item.type.slice(5);
      if (kind === 'notebook') throw error('NO_ORIGINAL', 'Notebook has no original PDF/EPUB; use archive');
      const data = Buffer.from(await (kind === 'archive' ? api.getDocumentArchive(ref) : kind === 'epub' ? api.getEpub(ref) : api.getPdf(ref)));
      await mkdir(dirname(args.outPath), { recursive: true });
      const temp = join(dirname(args.outPath), `.${basename(args.outPath)}.${randomUUID()}.part`);
      let handle;
      try {
        handle = await open(temp, 'wx', 0o600);
        await handle.writeFile(data); await handle.sync(); await handle.close(); handle = null;
        // Hard-link publishes atomically and fails if ANY target already exists.
        // Never fall back to a racy exists-then-rename or a partial exclusive copy.
        assertRequestActive();
        if (args.overwrite === true) await rename(temp, args.outPath);
        else await link(temp, args.outPath);
      } catch (cause) {
        if (cause.code === 'EEXIST') throw error('EXISTS', 'Destination exists; set overwrite:true to replace it');
        throw cause;
      } finally { if (handle) await handle.close(); await unlink(temp).catch(e => { if (e.code !== 'ENOENT') throw e; }); }
      return { written: args.outPath, bytes: data.byteLength, kind, format, version: ref.hash };
    },
    async list(args = {}) {
      for (const flag of ['refresh', 'include_tags', 'include_trash']) if (args[flag] !== undefined && typeof args[flag] !== 'boolean') throw error('BAD_ARGS', `${flag} must be boolean`);
      const limit = integer(args.limit, 60, 1, 500);
      let offset = integer(args.offset, 0, 0, Number.MAX_SAFE_INTEGER);
      const query = args.query === undefined ? '' : text(args.query, 'query', true).toLowerCase();
      const parent = args.parent == null ? null : text(args.parent, 'parent', true);
      const filter = [parent, query, !!args.include_trash, !!args.include_tags];
      const includeTags = !!args.include_tags || !!query;
      const api = await getApi();
      await loadDiskCache(api.deviceId);
      if (args.refresh) { cache.clear(); api.raw.clearCache(); }
      // Always refresh the mutable root. Immutable item refs are safe cache keys.
      const refs = await api.listRefs(true);
      const pairs = refPairs(refs);
      let cursor;
      if (args.cursor !== undefined) {
        try { cursor = JSON.parse(Buffer.from(text(args.cursor, 'cursor'), 'base64url').toString('utf8')); }
        catch { throw error('BAD_ARGS', 'Invalid cursor'); }
        if (JSON.stringify(cursor.filter) !== JSON.stringify(filter)) throw error('BAD_ARGS', 'Cursor filters differ');
        if (JSON.stringify(cursor.refs) !== JSON.stringify(pairs)) throw error('STALE_CURSOR', 'Library changed; restart pagination');
        offset = integer(cursor.offset, 0, 0, Number.MAX_SAFE_INTEGER);
      }
      const failedIds = [], tagErrors = [];
      let hits = 0;
      const walked = await mapBounded(refs, async ref => {
        const cached = cache.get(ref.id);
        if (cached?.item.version === ref.hash && (!includeTags || cached.tagsLoaded)) { hits++; return cached.item; }
        try {
          const read = await readItem(api, ref, includeTags);
          cache.set(ref.id, read);
          if (read.tagError) tagErrors.push(ref.id);
          return read.item;
        } catch { failedIds.push(ref.id); cache.delete(ref.id); return null; }
      });
      const live = new Set(refs.map(r => r.id));
      for (const id of cache.keys()) if (!live.has(id)) cache.delete(id);
      const cacheWarning = await saveDiskCache(api.deviceId);
      const items = walked.filter(Boolean);
      const byId = new Map(items.map(item => [item.id, item]));
      const hierarchyErrors = [];
      const trashed = item => {
        const seen = new Set();
        while (item) {
          if (item.deleted || item.parent === 'trash') return true;
          if (seen.has(item.id)) { hierarchyErrors.push(item.id); return null; }
          seen.add(item.id);
          if (item.parent === '') return false;
          if (!byId.has(item.parent)) { hierarchyErrors.push(item.id); return null; }
          item = byId.get(item.parent);
        }
        return false;
      };
      let selected = items.map(item => ({ ...item, tags: [...item.tags], trashed: trashed(item) }));
      selected = selected.filter(item => (args.include_trash || item.trashed !== true) && (parent === null || item.parent === parent)
        && (!query || item.name.toLowerCase().includes(query) || item.tags.some(t => t.toLowerCase().includes(query))));
      selected.sort((a, b) => a.id.localeCompare(b.id));
      const selection = selected.map(x => x.id);
      if (cursor && !isDeepStrictEqual(cursor.selection, selection)) throw error('STALE_CURSOR', 'Listing coverage changed; restart pagination');
      const matched = selected.length;
      const entries = selected.slice(offset, offset + limit);
      const more = offset + entries.length < matched;
      const partial = !!(failedIds.length || tagErrors.length || hierarchyErrors.length);
      const nextOffset = more ? offset + entries.length : null;
      const nextCursor = more ? Buffer.from(JSON.stringify({ filter, refs: pairs, selection, offset: nextOffset })).toString('base64url') : null;
      return { entries, matched, totalRefs: refs.length, cached: hits === refs.length, offset, limit,
        cache_warning: cacheWarning, truncated: more, has_more: more, next_offset: nextOffset, next_cursor: nextCursor, partial, complete: !partial,
        code: partial ? 'PARTIAL_COVERAGE' : more ? 'TRUNCATED' : null,
        failedIds: failedIds.sort(), tagErrors: tagErrors.sort(), hierarchyErrors: [...new Set(hierarchyErrors)].sort(), walkErrors: failedIds.length,
        coverage: { total: refs.length, read: items.length, failed: failedIds.length, tag_failed: tagErrors.length } };
    },
  };
}
