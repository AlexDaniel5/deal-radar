// Small shared helpers. No dependencies, no network calls of its own.

export const $ = (s) => document.querySelector(s);

/** Escape for use as text inside HTML. */
export const esc = (s) => String(s == null ? '' : s).replace(/</g, '&lt;');

/** Escape for use inside a double-quoted HTML attribute. */
export const escAttr = (s) =>
  String(s == null ? '' : s)
    .replace(/&/g, '&amp;')
    .replace(/"/g, '&quot;')
    .replace(/</g, '&lt;');

/**
 * Make a scraped URL safe to put in an href.
 *
 * Listing URLs come from scraped marketplace HTML, so they are untrusted twice
 * over: a quote would break out of the attribute, and a `javascript:` scheme
 * would run on click. Escaping alone does not stop the second one, so the
 * scheme is checked too; anything else renders as plain text.
 */
export function safeUrl(url) {
  const s = String(url == null ? '' : url);
  return /^https?:\/\//i.test(s) ? escAttr(s) : '';
}

/** A link when the URL is usable, plain text when it is not. */
export function linkTo(url, label) {
  const href = safeUrl(url);
  const text = esc(label);
  return href ? `<a href="${href}" target="_blank" rel="noopener">${text}</a>` : text;
}

export function fmtDate(ts) {
  return ts
    ? new Date(ts * 1000).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
    : '?';
}

export const fmtMoney = (n) => (n != null ? '$' + Math.round(n) : '?');

// --- connection state --------------------------------------------------------

const offlineListeners = new Set();
let offline = false;

/** Notified with `true` when we lose contact with the server, `false` on recovery. */
export function onConnectionChange(fn) {
  offlineListeners.add(fn);
  fn(offline);
}

function setOffline(next) {
  if (next === offline) return;
  offline = next;
  for (const fn of offlineListeners) fn(offline);
}

export const isOffline = () => offline;

/**
 * fetch() with a timeout, JSON handling, and connection tracking.
 *
 * Previously every call was a bare fetch with no error handling: if the server
 * died, the status dot froze on its last value and the tables kept showing
 * stale data with nothing to say otherwise.
 *
 * All writes send `Content-Type: application/json` — the server requires it so
 * that a plain cross-site form POST (which cannot set that header without a
 * preflight) can't drive this API.
 */
export async function api(path, { method = 'GET', body, timeout = 8000 } = {}) {
  const opts = { method, signal: AbortSignal.timeout(timeout) };
  if (method !== 'GET' && method !== 'HEAD') {
    opts.headers = { 'Content-Type': 'application/json' };
    opts.body = JSON.stringify(body ?? {});
  }
  let response;
  try {
    response = await fetch(path, opts);
  } catch (err) {
    setOffline(true);
    throw new ApiError('Could not reach deal-radar.', { cause: err, offline: true });
  }
  setOffline(false);
  const text = await response.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = null;
  }
  if (!response.ok) {
    const message = (data && (data.error || data.message)) || `request failed (${response.status})`;
    throw new ApiError(message, { status: response.status, data });
  }
  return data;
}

/** Plain-text GET (the raw config editor), with the same connection tracking. */
export async function apiText(path, { timeout = 8000 } = {}) {
  try {
    const r = await fetch(path, { signal: AbortSignal.timeout(timeout) });
    setOffline(false);
    return await r.text();
  } catch (err) {
    setOffline(true);
    throw new ApiError('Could not reach deal-radar.', { cause: err, offline: true });
  }
}

export class ApiError extends Error {
  constructor(message, { status = 0, data = null, offline = false, cause } = {}) {
    super(message, { cause });
    this.name = 'ApiError';
    this.status = status;
    this.data = data;
    this.offline = offline;
  }
}
