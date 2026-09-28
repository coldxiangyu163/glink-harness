/**
 * HTTP surface of the pi-glink host.
 *
 * The browser talks to the same endpoints the smoke script uses; nothing here
 * knows about pi. Endpoint shapes are documented in README.md.
 *
 * Two identity modes share this client:
 *   · gateway: the authentication gateway injects the identity it already
 *     holds; the workbench sends no credential at all;
 *   · accounts: the server sets an `HttpOnly` session cookie, and every
 *     state-changing request carries the session's CSRF token in a header.
 *     The token lives in this module's closure only — never in localStorage,
 *     sessionStorage, the URL or the DOM.
 */

export class ApiError extends Error {
  constructor(message, { status = 0, code = null, payload = null } = {}) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.payload = payload;
  }
}

/** The accounts-mode CSRF token for the current session (memory only). */
let csrfToken = null;

export function setCsrfToken(token) {
  csrfToken = typeof token === 'string' && token ? token : null;
}

export function getCsrfToken() {
  return csrfToken;
}

async function parse(response) {
  const text = await response.text();
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return { raw: text };
  }
}

async function call(path, { method = 'GET', body = null, fetchImpl = globalThis.fetch } = {}) {
  // Same-origin credentials travel by default; making it explicit documents the
  // multi-tenant contract: either an authentication gateway in front of this
  // host injects the short-lived identity from the session cookie it already
  // holds, or the host's own `HttpOnly` session cookie travels here.
  const init = { method, headers: { Accept: 'application/json' }, credentials: 'same-origin' };
  if (body !== null) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
  if (method !== 'GET' && csrfToken) init.headers['X-Pi-Glink-CSRF'] = csrfToken;
  const response = await fetchImpl(path, init);
  const payload = await parse(response);
  if (!response.ok) {
    throw new ApiError(payload?.message ?? payload?.error ?? `HTTP ${response.status}`, {
      status: response.status,
      code: payload?.error ?? null,
      payload,
    });
  }
  return payload;
}

export function createApi({ fetchImpl = globalThis.fetch } = {}) {
  const options = { fetchImpl };
  return {
    /** Host health: pi availability, configured model, active run. */
    health: () => call('/api/health', options),
    /** Public config: executor, model, configured flags. Never credentials. */
    config: () => call('/api/config', options),
    /** Current identity, or a 401 whose payload names the identity mode. */
    me: () => call('/api/me', options),
    /**
     * The research history, newest first. `limit`/`cursor` ask for one page;
     * the answer carries `nextCursor`/`hasMore` when the host has more to
     * give (`workspace.mjs` keeps the loaded pages and shows «显示更多»).
     */
    listRuns: ({ limit = null, cursor = null } = {}) => {
      const query = new URLSearchParams();
      if (limit) query.set('limit', String(limit));
      if (cursor) query.set('cursor', cursor);
      const suffix = query.toString();
      return call(`/api/runs${suffix ? `?${suffix}` : ''}`, options);
    },
    getRun: (runId) => call(`/api/runs/${encodeURIComponent(runId)}`, options),
    startRun: (prompt, parentRunId) => call('/api/runs', {
      ...options, method: 'POST', body: { prompt, ...(parentRunId ? { parentRunId } : {}) },
    }),
    stopRun: (runId) =>
      call(`/api/runs/${encodeURIComponent(runId)}/stop`, { ...options, method: 'POST', body: {} }),
    // -- accounts mode -------------------------------------------------------
    login: (email, password) => call('/api/auth/login', {
      ...options, method: 'POST', body: { email, password },
    }),
    register: ({ invite, email, name, password }) => call('/api/auth/register', {
      ...options, method: 'POST', body: { invite, email, name, password },
    }),
    logout: () => call('/api/auth/logout', { ...options, method: 'POST', body: {} }),
    // The invitation code is a capability that can create a member/admin
    // account: it travels in a JSON body, never in a URL or query string.
    checkInvite: (invite) => call('/api/invitations/check', {
      ...options, method: 'POST', body: { token: invite },
    }),
    createInvitation: (email, role) => call('/api/admin/invitations', {
      ...options, method: 'POST', body: { email, role },
    }),
    eventsUrl: (runId) => `/api/runs/${encodeURIComponent(runId)}/events`,
    reportUrl: (runId) => `/api/runs/${encodeURIComponent(runId)}/report`,
    childFileUrl: (runId, n, name) =>
      `/api/runs/${encodeURIComponent(runId)}/children/${encodeURIComponent(String(n))}/${encodeURIComponent(name)}`,
  };
}
