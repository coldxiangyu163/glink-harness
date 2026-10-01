/**
 * HTTP client contract: what the browser actually sends.
 *
 * The host rejects state-changing requests that are not `application/json`
 * (`unsupported_media_type`) and, once a session exists, requires the
 * session-bound CSRF header. Those are easy to get wrong in a way no
 * server-side test sees — the Python tests build their own requests — so the
 * client's own request shape is pinned here.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';

import { createApi, setCsrfToken, getCsrfToken } from '../../web/modules/api.mjs';

function recorder() {
  const calls = [];
  const fetchImpl = async (url, init) => {
    calls.push({ url, init });
    return {
      ok: true,
      status: 200,
      text: async () => '{"ok":true}',
    };
  };
  return { calls, api: createApi({ fetchImpl }) };
}

test('every mutating call is JSON with same-origin credentials', async () => {
  const { calls, api } = recorder();
  setCsrfToken(null);
  await api.login('member@example.test', 'correct horse battery');
  await api.register({ invite: 't', email: 'e@example.test', name: 'n', password: 'p'.repeat(12) });
  await api.createInvitation('new@example.test', 'member');
  await api.checkInvite('secret-invite-token');
  await api.startRun('研究问题');
  await api.stopRun('run-1');
  await api.logout();
  assert.equal(calls.length, 7);
  for (const { init } of calls) {
    assert.equal(init.method, 'POST');
    assert.equal(init.headers['Content-Type'], 'application/json');
    assert.equal(init.credentials, 'same-origin');
    assert.equal(typeof init.body, 'string');
    assert.doesNotThrow(() => JSON.parse(init.body));
  }
  assert.deepEqual(JSON.parse(calls[5].init.body), {}, 'logout must carry a JSON body, not an empty one');

  // The invitation check posts the token in the body: it must never appear in a
  // URL, query string or Referer.
  const check = calls.find((item) => item.url === '/api/invitations/check');
  assert.ok(check, 'checkInvite must call the check endpoint');
  assert.equal(check.url.includes('secret-invite-token'), false);
  assert.equal(check.url.includes('?'), false);
  assert.deepEqual(JSON.parse(check.init.body), { token: 'secret-invite-token' });
  assert.equal(check.init.headers['Content-Type'], 'application/json');
});

test('the CSRF header only appears when a session supplied a token', async () => {
  const { calls, api } = recorder();
  setCsrfToken(null);
  await api.startRun('无令牌');
  assert.equal(calls[0].init.headers['X-Glink-Harness-CSRF'], undefined);
  assert.equal(getCsrfToken(), null);

  setCsrfToken('csrf-token-value');
  await api.startRun('有令牌');
  assert.equal(calls[1].init.headers['X-Glink-Harness-CSRF'], 'csrf-token-value');
  await api.stopRun('run-1');
  assert.equal(calls[2].init.headers['X-Glink-Harness-CSRF'], 'csrf-token-value');

  // GETs never carry the token.
  await api.me();
  assert.equal(calls[3].init.headers['X-Glink-Harness-CSRF'], undefined);

  setCsrfToken('');
  assert.equal(getCsrfToken(), null);
  await api.startRun('清空后');
  assert.equal(calls[4].init.headers['X-Glink-Harness-CSRF'], undefined);
});

test('403 csrf_invalid refreshes the accounts token and retries the same POST once', async () => {
  const calls = [];
  setCsrfToken('stale');
  const api = createApi({ onCsrfInvalid: async () => { setCsrfToken((await api.me()).csrfToken); return true; }, fetchImpl: async (path, init) => {
    calls.push({ path, init });
    if (path === '/api/me') return { ok: true, status: 200, text: async () => '{"csrfToken":"fresh"}' };
    return calls.filter((call) => call.path === path).length === 1
      ? { ok: false, status: 403, text: async () => '{"error":"csrf_invalid"}' }
      : { ok: true, status: 200, text: async () => '{"run":{"id":"new"}}' };
  } });
  const result = await api.startRun('同一个问题');
  assert.equal(result.run.id, 'new');
  assert.deepEqual(calls.map(({ path }) => path), ['/api/runs', '/api/me', '/api/runs']);
  assert.equal(calls[0].init.headers['X-Glink-Harness-CSRF'], 'stale');
  assert.equal(calls[2].init.headers['X-Glink-Harness-CSRF'], 'fresh');
  assert.equal(calls[2].init.body, calls[0].init.body);
  assert.equal(getCsrfToken(), 'fresh');
  setCsrfToken(null);
});

test('a second csrf_invalid reaches the failure surface without a refresh loop', async () => {
  const paths = [];
  setCsrfToken('stale');
  const api = createApi({ onCsrfInvalid: async () => { setCsrfToken((await api.me()).csrfToken); return true; }, fetchImpl: async (path, init) => {
    paths.push({ path, init });
    if (path === '/api/me') return { ok: true, status: 200, text: async () => '{"csrfToken":"fresh"}' };
    return { ok: false, status: 403, text: async () => '{"error":"csrf_invalid"}' };
  } });
  await assert.rejects(() => api.stopRun('r1'), (error) => error.status === 403 && error.code === 'csrf_invalid');
  assert.deepEqual(paths.map(({ path }) => path), ['/api/runs/r1/stop', '/api/me', '/api/runs/r1/stop']);
  assert.equal(paths[2].init.headers['X-Glink-Harness-CSRF'], 'fresh');
  setCsrfToken(null);
});

test('GET, 401, and other 403 failures never refresh or retry', async () => {
  for (const [method, status, code] of [['GET', 403, 'csrf_invalid'], ['POST', 401, 'unauthenticated'], ['POST', 403, 'forbidden']]) {
    let calls = 0;
    let refreshes = 0;
    const api = createApi({ onCsrfInvalid: async () => { refreshes += 1; return true; }, fetchImpl: async () => {
      calls += 1;
      return { ok: false, status, text: async () => JSON.stringify({ error: code }) };
    } });
    await assert.rejects(() => method === 'GET' ? api.me() : api.startRun('问题'), (error) => error.status === status && error.code === code);
    assert.equal(calls, 1);
    assert.equal(refreshes, 0);
  }
});

test('a failed response becomes an ApiError carrying status and code', async () => {
  const api = createApi({
    fetchImpl: async () => ({
      ok: false,
      status: 401,
      text: async () => '{"error":"unauthenticated","mode":"accounts"}',
    }),
  });
  await assert.rejects(
    () => api.me(),
    (error) => {
      assert.equal(error.status, 401);
      assert.equal(error.code, 'unauthenticated');
      assert.equal(error.payload.mode, 'accounts');
      return true;
    }
  );
});
