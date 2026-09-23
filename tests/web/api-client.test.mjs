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
  assert.equal(calls[0].init.headers['X-Pi-Glink-CSRF'], undefined);
  assert.equal(getCsrfToken(), null);

  setCsrfToken('csrf-token-value');
  await api.startRun('有令牌');
  assert.equal(calls[1].init.headers['X-Pi-Glink-CSRF'], 'csrf-token-value');
  await api.stopRun('run-1');
  assert.equal(calls[2].init.headers['X-Pi-Glink-CSRF'], 'csrf-token-value');

  // GETs never carry the token.
  await api.me();
  assert.equal(calls[3].init.headers['X-Pi-Glink-CSRF'], undefined);

  setCsrfToken('');
  assert.equal(getCsrfToken(), null);
  await api.startRun('清空后');
  assert.equal(calls[4].init.headers['X-Pi-Glink-CSRF'], undefined);
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
