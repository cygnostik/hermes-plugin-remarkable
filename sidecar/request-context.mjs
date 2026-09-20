import { AsyncLocalStorage } from 'node:async_hooks';

export const requestContext = new AsyncLocalStorage();
export function assertRequestActive() {
  if (requestContext.getStore()?.expired) {
    throw Object.assign(new Error('Request expired; no further write may start'), { code: 'REQUEST_EXPIRED' });
  }
}
const guarded = new WeakSet();
export function guardCommit(api) {
  if (api?.raw && !guarded.has(api.raw)) {
    // rmapi-js has no cancellation parameter. Guard the actual publication
    // boundary, including when high-level putPdf/putFolder are still running.
    const putRoot = api.raw.putRootHash;
    if (typeof putRoot === 'function') api.raw.putRootHash = function (...args) { assertRequestActive(); return putRoot.apply(this, args); };
    guarded.add(api.raw);
  }
  return api;
}
