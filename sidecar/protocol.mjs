import { requestContext } from './request-context.mjs';
import { CloudError } from './cloud-service.mjs';

const WRITES = new Set(['manage', 'mkdir', 'upload_pdf', 'upload_epub', 'enroll', 'download']);
const object = x => x !== null && typeof x === 'object' && !Array.isArray(x);

export function failureResponse(id, cause) {
  if (cause instanceof CloudError) return { id, ok: false, result: cause.details, code: cause.code, error: cause.message };
  // Never echo response bodies, stacks or third-party exception messages: some
  // HTTP clients include credentials or full request headers in these strings.
  const status = Number(cause?.status);
  const code = status === 401 || status === 403 ? 'AUTH_FAILED' : Number.isInteger(status) && status >= 400 && status <= 599 ? `HTTP_${status}` :
    ['ENOENT', 'EACCES', 'EPERM', 'ENOSPC', 'EISDIR'].includes(cause?.code) ? cause.code : 'ERROR';
  return { id, ok: false, result: {}, code, error: code === 'AUTH_FAILED' ? 'Cloud authentication failed; re-enroll if needed' : 'Operation failed; no automatic retry was attempted' };
}

export function createDispatcher(handlers, { minTimeoutMs = 5000, defaultTimeoutMs = 180000 } = {}) {
  let tail = Promise.resolve(), writeBlocked = false;
  async function processLine(line) {
    let id = null, timer;
    try {
      let req;
      try { req = JSON.parse(line); } catch { throw new CloudError('BAD_JSON', 'Request is not valid JSON'); }
      if (!object(req)) throw new CloudError('BAD_REQUEST', 'Request must be an object');
      id = req.id ?? null;
      if (typeof req.op !== 'string') throw new CloudError('BAD_REQUEST', 'op must be a string');
      if (!Object.hasOwn(handlers, req.op) || typeof handlers[req.op] !== 'function') throw new CloudError('BAD_OP', 'Unknown operation');
      if (req.args !== undefined && !object(req.args)) throw new CloudError('BAD_ARGS', 'args must be an object');
      if (req.timeoutMs !== undefined && (!Number.isFinite(req.timeoutMs) || req.timeoutMs <= 0)) throw new CloudError('BAD_ARGS', 'timeoutMs must be a positive number');
      const writes = WRITES.has(req.op);
      if (writes && writeBlocked) throw new CloudError('WRITE_BLOCKED', 'A previous write is unresolved; inspect the library and restart the sidecar before another write', { retry_safe: false });
      const timeout = Math.max(minTimeoutMs, Math.min(req.timeoutMs ?? defaultTimeoutMs, 900000));
      const context = { expired: false };
      const operation = requestContext.run(context, () => Promise.resolve().then(() => handlers[req.op](req.args ?? {})));
      const deadline = new Promise((_, reject) => {
        timer = setTimeout(() => {
          context.expired = true;
          if (writes) writeBlocked = true;
          reject(new CloudError(writes ? 'WRITE_OUTCOME_UNKNOWN' : 'TIMEOUT', writes ? 'Write timed out; outcome may be applied or still in flight. Inspect before retrying' : 'Read timed out', writes ? { outcome: 'unknown', retry_safe: false, item_id: req.args?.id ?? null } : {}));
        }, timeout);
      });
      const result = await Promise.race([operation, deadline]);
      if (!object(result)) throw new CloudError('INVALID_RESULT', 'Handler returned an invalid result');
      return { id, ok: true, result };
    } catch (cause) {
      if (cause.code === 'WRITE_OUTCOME_UNKNOWN' || cause.code === 'VERIFY_FAILED') writeBlocked = true;
      return failureResponse(id, cause);
    } finally { clearTimeout(timer); }
  }
  return line => {
    // Serialize requests so simultaneous writes cannot race each other's root.
    const response = tail.then(() => processLine(line));
    tail = response.then(() => undefined, () => undefined);
    return response;
  };
}
