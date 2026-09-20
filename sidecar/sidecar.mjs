#!/usr/bin/env node
/** JSON-lines bridge over pinned rmapi-js. Importing never enrolls or writes. */
import { createInterface } from 'node:readline';
import { readFile, writeFile, mkdir, rename, unlink, stat } from 'node:fs/promises';
import { dirname, join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { randomUUID } from 'node:crypto';
import { register } from 'rmapi-js';
import { createSessionProvider } from './session-provider.mjs';
import { assertRequestActive } from './request-context.mjs';
import { createCloudService, CloudError } from './cloud-service.mjs';
import { createDispatcher, failureResponse } from './protocol.mjs';

export function createHandlers(options = {}) {
  const home = process.env.HERMES_HOME || (process.platform === 'win32'
    ? join(process.env.LOCALAPPDATA || join(process.env.USERPROFILE || '.', 'AppData', 'Local'), 'hermes')
    : join(process.env.HOME || '.', '.hermes'));
  const tokenFile = options.tokenFile || process.env.RMAPI_JS_TOKEN_FILE || join(home, 'credentials', 'remarkable', 'device-token');
  const cacheDir = options.cacheDir || process.env.RMAPI_JS_CACHE_DIR || join(home, 'remarkable-plugin', 'cache');
  const apiFactory = options.apiFactory || createSessionProvider({ tokenFile, auth: options.auth, session: options.session, now: options.now });
  const registerDevice = options.registerDevice || register;
  let cachedApi, cachedToken;
  const getApi = options.getApi || (async () => {
    let token;
    try { token = (await readFile(tokenFile, 'utf8')).trim(); }
    catch (cause) { if (cause.code === 'ENOENT') throw new CloudError('NO_TOKEN', 'Not enrolled; use the secure reMarkable enrollment flow'); throw cause; }
    if (!token) throw new CloudError('NO_TOKEN', 'Token file is empty; re-enroll');
    // Never let the dependency transparently replay ambiguous cloud writes.
    const apiOptions = { maxGenerationRetries: 0, maxTransientRetries: 0, maxCacheSize: 20000 };
    // The durable provider checks identity and expiry on every operation. Preserve
    // the existing device-token factory injection contract for offline callers.
    if (!options.apiFactory) return apiFactory(token, apiOptions);
    if (!cachedApi || cachedToken !== token) {
      cachedToken = token;
      cachedApi = Promise.resolve().then(() => apiFactory(token, apiOptions))
        .catch(cause => { cachedApi = undefined; throw cause; });
    }
    return cachedApi;
  });
  const service = createCloudService({ getApi, cacheDir });
  return {
    ...service,
    async status() {
      let enrolled = false;
      try { enrolled = (await stat(tokenFile)).isFile(); } catch (cause) { if (cause.code !== 'ENOENT') throw cause; }
      let library = null, cloud = null, cloud_code = null;
      if (enrolled) {
        try { library = (await (await getApi()).listRefs(true)).length; cloud = 'ok'; }
        catch (cause) { const failure = failureResponse(null, cause); cloud = 'error'; cloud_code = failure.code; }
      }
      return { enrolled, library, cloud, cloud_code, tokenFile };
    },
    // Internal-only: invoked by the secure enrollment CLI, not model tools.
    async enroll(args = {}) {
      if (typeof args.code !== 'string' || !/^[a-zA-Z0-9]{8}$/.test(args.code)) throw new CloudError('BAD_CODE', 'Enrollment requires the eight-character one-time code');
      const token = await registerDevice(args.code, { deviceDesc: process.platform === 'win32' ? 'desktop-windows' : process.platform === 'darwin' ? 'desktop-macos' : 'desktop-linux' });
      if (typeof token !== 'string' || !token.trim()) throw new CloudError('ENROLL_FAILED', 'Enrollment returned no device token');
      await mkdir(dirname(tokenFile), { recursive: true, mode: 0o700 });
      const temp = `${tokenFile}.${randomUUID()}.part`;
      try {
        await writeFile(temp, token, { flag: 'wx', mode: 0o600 });
        assertRequestActive();
        await rename(temp, tokenFile);
      } finally { await unlink(temp).catch(e => { if (e.code !== 'ENOENT') throw e; }); }
      if (await readFile(tokenFile, 'utf8') !== token) throw new CloudError('ENROLL_FAILED', 'Saved enrollment could not be verified');
      // Explicit enrollment invalidates temporary credentials even if a service
      // returns the same device token. Never remove the durable device token.
      await unlink(`${tokenFile}.session.json`).catch(cause => { if (cause.code !== 'ENOENT') throw cause; });
      cachedApi = undefined;
      return { enrolled: true };
    },
  };
}

export async function runStdio({ input = process.stdin, output = process.stdout, handlers = createHandlers() } = {}) {
  const dispatch = createDispatcher(handlers);
  const lines = createInterface({ input, terminal: false, crlfDelay: Infinity });
  for await (const line of lines) {
    if (!line.trim()) continue;
    const response = await dispatch(line);
    await new Promise((done, reject) => output.write(JSON.stringify(response) + '\n', cause => cause ? reject(cause) : done()));
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  runStdio().catch(() => { process.stderr.write('Sidecar protocol stream failed\n'); process.exitCode = 1; });
}
