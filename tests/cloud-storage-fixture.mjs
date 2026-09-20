// Stateful immutable-blob fixture. Real rmapi-js high-level methods execute;
// only the raw storage boundary is injected. Never contacts a cloud endpoint.
import { session, GenerationError } from '../sidecar/node_modules/rmapi-js/dist/index.js';
export async function storageFixture(options = {}) {
  const fakeToken = `fixture.${Buffer.from(JSON.stringify({ 'device-id': 'test-device' })).toString('base64url')}.fixture`;
  const api = session(fakeToken, { maxGenerationRetries: 0, maxTransientRetries: 0, ...options });
  let serial = 0, generation = 1;
  const blobs = new Map(), indexes = new Map();
  const calls = { commits: 0, puts: 0, disposed: 0 };
  const version = () => `fixture-ref-${++serial}`;
  const pending = (id, hash, persist, extra = {}) => ({ id, hash, type: 0, subfiles: 0, size: 1, ...extra,
    async [Symbol.asyncDispose]() { calls.disposed++; persist(); } });
  let root;
  const raw = {
    async getRootHash() { return [root, generation, 4]; },
    async getEntries(ref) {
      const value = indexes.get(ref.hash);
      if (!value) throw new Error('Index not uploaded before access');
      return structuredClone(value);
    },
    async getHash(ref) {
      const value = blobs.get(ref.hash);
      if (!value) throw new Error('Blob not uploaded before access');
      return Buffer.from(value);
    },
    async getMetadata(ref) { return JSON.parse((await raw.getHash(ref)).toString()); },
    async putFile(id, data) {
      calls.puts++;
      const hash = version(), copy = Buffer.from(data);
      return pending(id, hash, () => blobs.set(hash, copy));
    },
    async putMetadata(id, value) { return raw.putFile(id, Buffer.from(JSON.stringify(value))); },
    async putContent(id, value) { return raw.putFile(id, Buffer.from(JSON.stringify(value))); },
    async putEntries(id, entries, schema) {
      if (id === 'root' && schema !== 4) throw new Error('Root must use schema 4');
      const hash = version();
      const safe = entries.map(({ id, hash, type, subfiles, size }) => ({ id, hash, type, subfiles, size }));
      return pending(id, hash, () => indexes.set(hash, { entries: safe }), { subfiles: entries.length });
    },
    async putRootHash(hash, expected) {
      calls.commits++;
      if (calls.conflict || expected !== generation) throw new GenerationError();
      if (!indexes.has(hash)) throw new Error('Root not uploaded before commit');
      if (!calls.ignoreCommit) { root = hash; generation++; }
      if (calls.loseResponse) throw Object.assign(new Error('lost response'), { code: 'ETIMEDOUT' });
      return [root, generation];
    },
    clearCache() {},
  };
  api.raw = raw;
  const empty = await raw.putEntries('root', [], 4);
  await empty[Symbol.asyncDispose](); root = empty.hash;
  const newClient = (options = {}) => {
    const client = session(fakeToken, { maxGenerationRetries: 0, maxTransientRetries: 0, ...options });
    client.raw = raw;
    return client;
  };
  return { api, calls, raw, blobs, indexes, newClient };
}
