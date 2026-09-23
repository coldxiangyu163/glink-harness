/**
 * HTTP surface of the pi-glink host.
 *
 * The browser talks to the same endpoints the smoke script uses; nothing here
 * knows about pi. Endpoint shapes are documented in README.md.
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
  // multi-tenant contract: an authentication gateway in front of this host
  // injects the short-lived identity from the session cookie it already holds,
  // so the workbench needs no login page and no token in the page.
  const init = { method, headers: { Accept: 'application/json' }, credentials: 'same-origin' };
  if (body !== null) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
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
    listRuns: () => call('/api/runs', options),
    getRun: (runId) => call(`/api/runs/${encodeURIComponent(runId)}`, options),
    startRun: (prompt) => call('/api/runs', { ...options, method: 'POST', body: { prompt } }),
    stopRun: (runId) =>
      call(`/api/runs/${encodeURIComponent(runId)}/stop`, { ...options, method: 'POST' }),
    eventsUrl: (runId) => `/api/runs/${encodeURIComponent(runId)}/events`,
    reportUrl: (runId) => `/api/runs/${encodeURIComponent(runId)}/report`,
  };
}
