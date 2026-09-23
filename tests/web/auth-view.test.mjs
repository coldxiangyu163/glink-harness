/**
 * Unauthenticated entry regressions: login, invitation registration, recovery
 * faces and the "no credential in browser storage" rule.
 *
 * Everything runs against the DOM stub and a controllable API: no browser, no
 * network. The workbench shell is not involved at all here — that boundary is
 * tested in `workspace-session.test.mjs`.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';

import { createAuthView } from '../../web/modules/auth.mjs';
import { createDomStub } from './dom-stub.mjs';

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

function apiError(status, code) {
  return Object.assign(new Error(code ?? `HTTP ${status}`), { status, code });
}

function harness({
  login = null,
  register = null,
  checkInvite = null,
  location = { pathname: '/login', search: '', origin: 'http://127.0.0.1:43210' },
} = {}) {
  const dom = createDomStub({ location });
  dom.install();
  const storageWrites = [];
  const setItem = dom.window.localStorage.setItem;
  dom.window.localStorage.setItem = (key, value) => {
    storageWrites.push([key, value]);
    return setItem(key, value);
  };
  const calls = { login: [], register: [], checkInvite: [] };
  const api = {
    checkInvite: async (invite) => {
      calls.checkInvite.push(invite);
      if (checkInvite) return checkInvite(invite);
      return { valid: false };
    },
    login: async (email, password) => {
      calls.login.push({ email, password });
      if (login) return login(email, password);
      throw apiError(401, 'invalid_credentials');
    },
    register: async (payload) => {
      calls.register.push(payload);
      if (register) return register(payload);
      return { registered: true, email: payload.email };
    },
  };
  const sessions = [];
  const view = createAuthView({ api });
  view.onSession((session) => sessions.push(session));
  return { dom, api, view, calls, sessions, storageWrites };
}

function submit(dom, id) {
  dom.getElement(id).dispatch('submit', { preventDefault() {} });
}

test('a successful login hands the session to the workspace and keeps storage untouched', async (t) => {
  const h = harness({
    login: async () => ({
      mode: 'accounts',
      authenticated: true,
      user: { id: 'u1', name: '成员' },
      tenant: { id: 'tenant-alpha', name: '组织甲' },
      csrfToken: 'csrf-token-value',
    }),
  });
  t.after(() => h.dom.restore());
  await h.view.show({ mode: 'login' });
  h.dom.getElement('login-email').value = 'member@example.test';
  h.dom.getElement('login-password').value = 'correct horse battery';
  submit(h.dom, 'login-form');
  await tick();
  assert.equal(h.sessions.length, 1);
  assert.equal(h.sessions[0].csrfToken, 'csrf-token-value');
  assert.deepEqual(h.calls.login, [{ email: 'member@example.test', password: 'correct horse battery' }]);
  assert.deepEqual(h.storageWrites, []);
});

test('a wrong password keeps the email and shows a retryable form error', async (t) => {
  const h = harness();
  t.after(() => h.dom.restore());
  await h.view.show({ mode: 'login' });
  h.dom.getElement('login-email').value = 'member@example.test';
  h.dom.getElement('login-password').value = 'wrong password!';
  submit(h.dom, 'login-form');
  await tick();
  const error = h.dom.getElement('login-error');
  assert.match(error.textContent, /邮箱或密码不正确/);
  assert.equal(error.hidden, false);
  assert.equal(h.dom.getElement('login-email').value, 'member@example.test');
  assert.equal(h.sessions.length, 0);
  // Retrying is possible: the form is not disabled.
  assert.equal(h.dom.getElement('login-submit').disabled, false);
});

test('throttling and disabled identities get their own explanations', async (t) => {
  const throttled = harness({ login: async () => { throw apiError(429, 'rate_limited'); } });
  t.after(() => throttled.dom.restore());
  await throttled.view.show({ mode: 'login' });
  throttled.dom.getElement('login-email').value = 'member@example.test';
  throttled.dom.getElement('login-password').value = 'x'.repeat(20);
  submit(throttled.dom, 'login-form');
  await tick();
  assert.match(throttled.dom.getElement('login-error').textContent, /尝试次数过多/);

  const disabled = harness({ login: async () => { throw apiError(403, 'account_disabled'); } });
  await disabled.view.show({ mode: 'login' });
  disabled.dom.getElement('login-email').value = 'member@example.test';
  disabled.dom.getElement('login-password').value = 'x'.repeat(20);
  submit(disabled.dom, 'login-form');
  await tick();
  assert.match(disabled.dom.getElement('login-error').textContent, /已停用/);
});

test('a valid invitation prefills the read-only identity and registers once', async (t) => {
  const h = harness({
    checkInvite: async () => ({
      valid: true,
      invitation: { email: 'invitee@example.test', role: 'member', tenant: { id: 'tenant-beta', name: '组织乙' } },
    }),
  });
  t.after(() => h.dom.restore());
  await h.view.show({ mode: 'register', invite: 'invite-token-value' });
  assert.equal(h.view.mode, 'register');
  assert.equal(h.dom.getElement('register-org').textContent, '组织乙');
  assert.equal(h.dom.getElement('register-email').value, 'invitee@example.test');
  assert.equal(h.dom.getElement('register-email').readOnly || h.dom.getElement('register-email').hasAttribute('readonly'), true);
  assert.deepEqual(h.calls.checkInvite, ['invite-token-value']);
  // The code is stripped from the URL as soon as the server has validated it.
  assert.equal(h.dom.location.search, '');

  h.dom.getElement('register-name').value = '受邀者';
  h.dom.getElement('register-password').value = 'correct horse battery';
  h.dom.getElement('register-confirm').value = 'something else';
  submit(h.dom, 'register-form');
  await tick();
  assert.equal(h.calls.register.length, 0, 'mismatched passwords must not reach the server');
  assert.match(h.dom.getElement('register-error').textContent, /不一致/);

  h.dom.getElement('register-confirm').value = 'correct horse battery';
  submit(h.dom, 'register-form');
  await tick();
  assert.deepEqual(h.calls.register, [{
    invite: 'invite-token-value',
    email: 'invitee@example.test',
    name: '受邀者',
    password: 'correct horse battery',
  }]);
  // Registration returns to login; no session is granted by the register call.
  assert.equal(h.view.mode, 'login');
  assert.match(h.dom.getElement('auth-notice').textContent, /账号已创建/);
  assert.equal(h.dom.getElement('login-email').value, 'invitee@example.test');
  assert.equal(h.dom.getElement('register-password').value, '');
  assert.equal(h.dom.getElement('register-confirm').value, '');
  assert.deepEqual(h.storageWrites, []);
});

test('short passwords and missing names are caught before the server', async (t) => {
  const h = harness({
    checkInvite: async () => ({
      valid: true,
      invitation: { email: 'invitee@example.test', role: 'member', tenant: { id: 't', name: '组织' } },
    }),
  });
  t.after(() => h.dom.restore());
  await h.view.show({ mode: 'register', invite: 'invite-token-value' });
  h.dom.getElement('register-name').value = '受邀者';
  h.dom.getElement('register-password').value = 'short';
  h.dom.getElement('register-confirm').value = 'short';
  submit(h.dom, 'register-form');
  await tick();
  assert.equal(h.calls.register.length, 0);
  assert.match(h.dom.getElement('register-error').textContent, /至少需要 10 个字符/);

  h.dom.getElement('register-name').value = '';
  h.dom.getElement('register-password').value = 'correct horse battery';
  h.dom.getElement('register-confirm').value = 'correct horse battery';
  submit(h.dom, 'register-form');
  await tick();
  assert.equal(h.calls.register.length, 0);
  assert.match(h.dom.getElement('register-error').textContent, /请填写姓名/);
});

test('an invalid or used invitation returns to login with an explanation', async (t) => {
  const h = harness({ checkInvite: async () => ({ valid: false }) });
  t.after(() => h.dom.restore());
  await h.view.show({ mode: 'register', invite: 'stale-token' });
  assert.equal(h.view.mode, 'login');
  assert.match(h.dom.getElement('auth-notice').textContent, /无效或已经使用/);
  assert.equal(h.view.invite, '');
});

test('registration without an invitation explains that one is required', async (t) => {
  const h = harness();
  t.after(() => h.dom.restore());
  await h.view.show({ mode: 'register' });
  assert.equal(h.view.mode, 'register-missing');
  assert.equal(h.dom.getElement('register-form').hidden, true);
  assert.match(h.dom.getElement('auth-notice').textContent, /需要邀请/);
  assert.equal(h.dom.getElement('register-foot').hidden, false);
});

test('gateway mode shows an explanation instead of a form that has no backend', async (t) => {
  const h = harness();
  t.after(() => h.dom.restore());
  await h.view.show({ mode: 'gateway' });
  assert.equal(h.view.mode, 'gateway');
  assert.equal(h.dom.getElement('login-form').hidden, true);
  assert.equal(h.dom.getElement('register-form').hidden, true);
  assert.equal(h.dom.getElement('gateway-note').hidden, false);
});

test('an unreachable service offers a retry instead of pretending to be logged out', async (t) => {
  const h = harness();
  t.after(() => h.dom.restore());
  await h.view.show({ mode: 'unavailable' });
  assert.equal(h.view.mode, 'unavailable');
  assert.equal(h.dom.getElement('unavailable-note').hidden, false);
  h.dom.getElement('auth-retry').dispatch('click', {});
  assert.equal(h.dom.location.reloaded, true);
});

test('hide clears credential fields and the invitation from memory', async (t) => {
  const h = harness({
    checkInvite: async () => ({
      valid: true,
      invitation: { email: 'invitee@example.test', role: 'member', tenant: { id: 't', name: '组织' } },
    }),
  });
  t.after(() => h.dom.restore());
  await h.view.show({ mode: 'register', invite: 'invite-token-value' });
  h.dom.getElement('register-password').value = 'correct horse battery';
  h.view.hide();
  assert.equal(h.dom.getElement('auth-view').hidden, true);
  assert.equal(h.view.invite, '');
  assert.equal(h.dom.getElement('register-password').value, '');
  assert.deepEqual(h.storageWrites, []);
});
