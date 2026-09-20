import { open, readFile, lstat, rename, unlink } from 'node:fs/promises';
import { constants } from 'node:fs';
import { dirname } from 'node:path';
import { randomUUID } from 'node:crypto';
import { assertRequestActive } from './request-context.mjs';
import { auth as exchange, session as fromSession } from 'rmapi-js';

const MAX_RECORD_BYTES = 64 * 1024;
const MAX_TOKEN_BYTES = 16 * 1024;
const EXPIRY_MARGIN_MS = 60_000;

// Expiry is scheduling metadata, not signature verification. The private
// credential directory is the trust boundary; rmapi-js constructs the client.
function reusable(token, now) {
  if (typeof token !== 'string' || token.length > MAX_TOKEN_BYTES || !/^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/.test(token)) return false;
  try {
    const claims = JSON.parse(Buffer.from(token.split('.')[1], 'base64url').toString('utf8'));
    return Number.isSafeInteger(claims?.exp) && claims.exp > 0 && claims.exp <= Number.MAX_SAFE_INTEGER / 1000 && claims.exp * 1000 > now + EXPIRY_MARGIN_MS;
  } catch { return false; }
}

const privateToUser = info => process.platform === 'win32' || ((info.mode & 0o077) === 0 && info.uid === process.getuid());

async function assertCredentialDirectory(path) {
  const info = await lstat(dirname(path));
  if (!info.isDirectory() || info.isSymbolicLink() || !privateToUser(info)) {
    throw Object.assign(new Error('Session credentials require a private directory'), { code: 'EACCES' });
  }
  // Windows inherits the credential directory ACL. POSIX mode bits cannot
  // configure Windows ACLs: the caller must supply a user-private directory.
}

async function readRecord(path) {
  let file;
  try {
    const before = await lstat(path);
    if (!before.isFile() || before.isSymbolicLink() || before.nlink !== 1 || !privateToUser(before)) return null;
    file = await open(path, constants.O_RDONLY | (constants.O_NOFOLLOW || 0));
    const info = await file.stat();
    if (!info.isFile() || info.nlink !== 1 || !privateToUser(info) || info.ino !== before.ino || info.dev !== before.dev || info.size > MAX_RECORD_BYTES) return null;
    const bytes = Buffer.alloc(MAX_RECORD_BYTES + 1);
    let size = 0, count;
    do {
      ({ bytesRead: count } = await file.read(bytes, size, bytes.length - size, size));
      size += count;
    } while (count && size < bytes.length);
    if (size > MAX_RECORD_BYTES) return null;
    try { return JSON.parse(bytes.subarray(0, size).toString('utf8')); }
    catch { return null; }
  } catch (cause) {
    if (cause.code === 'ENOENT') return null;
    throw cause;
  } finally { await file?.close(); }
}

async function writeRecord(path, record) {
  const temp = `${path}.${randomUUID()}.part`;
  let file;
  try {
    assertRequestActive();
    file = await open(temp, 'wx', 0o600);
    await file.writeFile(JSON.stringify(record), 'utf8');
    await file.sync();
    await file.close(); file = undefined;
    assertRequestActive();
    await rename(temp, path);
  } finally {
    await file?.close();
    await unlink(temp).catch(cause => { if (cause.code !== 'ENOENT') throw cause; });
  }
}

async function assertIdentity(tokenFile, deviceToken) {
  if ((await readFile(tokenFile, 'utf8')).trim() !== deviceToken) {
    throw Object.assign(new Error('Device credential changed during authentication; start a new operation'), { status: 401 });
  }
}

/** Session credentials live beside the device token, never in metadata cache. */
export function createSessionProvider({ tokenFile, auth = exchange, session = fromSession, now = Date.now } = {}) {
  const sessionFile = `${tokenFile}.session.json`;
  return async (deviceToken, options) => {
    if (typeof deviceToken !== 'string' || !deviceToken || Buffer.byteLength(deviceToken, 'utf8') > MAX_TOKEN_BYTES) {
      throw Object.assign(new Error('Device credential is invalid'), { status: 401 });
    }
    await assertCredentialDirectory(sessionFile);
    const cached = await readRecord(sessionFile);
    if (cached?.version === 1 && cached.deviceToken === deviceToken && reusable(cached.sessionToken, now())) {
      await assertIdentity(tokenFile, deviceToken);
      try { return session(cached.sessionToken, options); }
      catch { /* Invalid cached claims: refresh before any cloud operation. */ }
    }
    const sessionToken = await auth(deviceToken);
    if (!reusable(sessionToken, now())) throw Object.assign(new Error('Cloud returned an unusable session credential'), { status: 401 });
    await assertIdentity(tokenFile, deviceToken);
    const api = session(sessionToken, options);
    await writeRecord(sessionFile, { version: 1, deviceToken, sessionToken });
    return api;
  };
}
