/**
 * Fetch wrapper with auth headers, retry, error normalization, and meta/cached awareness.
 * Matches the backend's {data, errors, meta} contract where applicable.
 */

import { store } from './state.js';

let authToken = null;

export function setAuthToken(token) {
  authToken = token;
}

function buildHeaders() {
  const headers = { 'Content-Type': 'application/json' };
  if (authToken) {
    headers['Authorization'] = `Bearer ${authToken}`;
  }
  return headers;
}

async function request(method, path, body = null, retries = 2) {
  const opts = { method, headers: buildHeaders() };
  if (body) opts.body = JSON.stringify(body);

  let lastError = null;
  for (let attempt = 0; attempt <= retries; attempt++) {
    try {
      const resp = await fetch(path, opts);
      const data = await resp.json();

      if (!resp.ok) {
        const msg = data?.error || data?.errors?.[0]?.message || `HTTP ${resp.status}`;
        if (resp.status >= 500 && attempt < retries) {
          await sleep(800 * (attempt + 1));
          continue;
        }
        throw new ApiError(msg, resp.status, data);
      }

      return data;
    } catch (e) {
      if (e instanceof ApiError) throw e;
      lastError = e;
      if (attempt < retries) {
        await sleep(800 * (attempt + 1));
        continue;
      }
    }
  }
  throw new ApiError(lastError?.message || 'Network error', 0, null);
}

function sleep(ms) {
  return new Promise(r => setTimeout(r, ms));
}

class ApiError extends Error {
  constructor(message, status, data) {
    super(message);
    this.status = status;
    this.data = data;
  }
}

export async function apiGet(path) {
  return request('GET', path);
}

export async function apiPost(path, body) {
  return request('POST', path, body);
}

/**
 * Fetch a data slice with loading/error state management.
 * Updates the store automatically.
 */
export async function fetchSlice(sliceKey, path) {
  store.setLoading(sliceKey, true);
  store.setError(sliceKey, null);
  try {
    const result = await apiGet(path);
    const data = result?.data !== undefined ? result.data : result;
    const meta = result?.meta || null;
    store.setData(sliceKey, data, meta);
    return data;
  } catch (e) {
    store.setError(sliceKey, e.message);
    store.setLoading(sliceKey, false);
    throw e;
  }
}

/**
 * Connect to an SSE endpoint. Returns an object with { close() }.
 */
export function connectSSE(path, body, onEvent, onError) {
  const ctrl = new AbortController();

  fetch(path, {
    method: 'POST',
    headers: buildHeaders(),
    body: JSON.stringify(body),
    signal: ctrl.signal,
  }).then(async (resp) => {
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop();

      for (const line of lines) {
        if (line.startsWith('data: ')) {
          try {
            const event = JSON.parse(line.slice(6));
            onEvent(event);
          } catch (e) { /* skip non-JSON lines */ }
        }
      }
    }
  }).catch((e) => {
    if (e.name !== 'AbortError' && onError) onError(e);
  });

  return { close: () => ctrl.abort() };
}

export { ApiError };
