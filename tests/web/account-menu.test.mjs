/**
 * Sidebar account area regressions: real identity display, menu behaviour and
 * the admin invitation dialog.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';

import { createAccountArea } from '../../web/modules/account.mjs';
import { createDomStub } from './dom-stub.mjs';

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

function harness({ createInvitation = null } = {}) {
  const dom = createDomStub();
  dom.install();
  const notices = [];
  const logouts = [];
  const calls = [];
  const api = {
    createInvitation: async (email, role) => {
      calls.push({ email, role });
      if (createInvitation) return createInvitation(email, role);
      return {
        invitation: { code: 'invite-code-value', email, role, tenant: { id: 'tenant-alpha', name: '组织甲' } },
      };
    },
  };
  const area = createAccountArea({
    api,
    onLogout: () => logouts.push(true),
    onNotice: (message, level) => notices.push({ message, level }),
  });
  // Nest the account shell the way index.html does, so the stub can model the
  // rule behind F-1 — a control inside a hidden container cannot take focus —
  // instead of treating every element as focusable. The menu starts hidden, as
  // in the markup.
  const shellArea = dom.getElement('account-area');
  const menu = dom.getElement('account-menu');
  menu.hidden = true;
  menu.append(dom.getElement('account-invite'), dom.getElement('account-logout'));
  shellArea.append(dom.getElement('account-button'), menu);
  return { dom, area, api, calls, notices, logouts };
}

const ACCOUNTS_ADMIN = {
  mode: 'accounts',
  authenticated: true,
  user: { id: 'u1', name: '张明', email: 'admin@example.test', role: 'admin' },
  tenant: { id: 'tenant-alpha', name: '组织甲' },
  csrfToken: 'csrf',
};

test('an admin identity shows the real organisation, name and both menu actions', (t) => {
  const h = harness();
  t.after(() => h.dom.restore());
  h.area.setIdentity(ACCOUNTS_ADMIN);
  assert.equal(h.dom.getElement('account-area').hidden, false);
  assert.equal(h.dom.getElement('account-avatar').textContent, '张');
  assert.equal(h.dom.getElement('account-org').textContent, '组织甲');
  assert.equal(h.dom.getElement('account-name').textContent, '张明');
  assert.equal(h.dom.getElement('account-menu-email').textContent, '邮箱：admin@example.test');
  assert.match(h.dom.getElement('account-menu-role').textContent, /管理员/);
  assert.equal(h.dom.getElement('account-invite').hidden, false);
  assert.equal(h.dom.getElement('account-logout').hidden, false);
  assert.match(h.dom.getElement('account-button').getAttribute('aria-label'), /张明/);
  assert.deepEqual(h.calls, []);
});

test('a member sees no invitation entry', (t) => {
  const h = harness();
  t.after(() => h.dom.restore());
  h.area.setIdentity({ ...ACCOUNTS_ADMIN, user: { ...ACCOUNTS_ADMIN.user, role: 'member' } });
  assert.equal(h.dom.getElement('account-invite').hidden, true);
  assert.equal(h.dom.getElement('account-logout').hidden, false);
  assert.match(h.dom.getElement('account-menu-role').textContent, /成员/);
});

test('gateway mode shows identity only: no logout this host does not own', (t) => {
  const h = harness();
  t.after(() => h.dom.restore());
  h.area.setIdentity({
    mode: 'gateway',
    authenticated: true,
    user: { id: 'user-a', name: 'user-a', role: 'member' },
    tenant: { id: 'tenant-alpha', name: 'tenant-alpha' },
    csrfToken: null,
  });
  assert.equal(h.dom.getElement('account-area').hidden, false);
  assert.equal(h.dom.getElement('account-org').textContent, 'tenant-alpha');
  assert.equal(h.dom.getElement('account-invite').hidden, true);
  assert.equal(h.dom.getElement('account-logout').hidden, true);
});

test('local mode and an expired identity hide the account area entirely', (t) => {
  const h = harness();
  t.after(() => h.dom.restore());
  h.area.setIdentity({ mode: 'local', authenticated: false });
  assert.equal(h.dom.getElement('account-area').hidden, true);
  h.area.setIdentity(null);
  assert.equal(h.dom.getElement('account-area').hidden, true);
  assert.equal(h.area.identity, null);
});

test('the account menu opens, closes on Escape and on an outside click', (t) => {
  const h = harness();
  t.after(() => h.dom.restore());
  h.area.setIdentity(ACCOUNTS_ADMIN);
  h.dom.getElement('account-button').dispatch('click', { stopPropagation() {} });
  assert.equal(h.area.menuOpen, true);
  assert.equal(h.dom.getElement('account-menu').hidden, false);
  assert.equal(h.dom.getElement('account-button').getAttribute('aria-expanded'), 'true');
  h.dom.document.dispatch('keydown', { key: 'Escape' });
  assert.equal(h.area.menuOpen, false);
  assert.equal(h.dom.getElement('account-menu').hidden, true);

  h.dom.getElement('account-button').dispatch('click', { stopPropagation() {} });
  assert.equal(h.area.menuOpen, true);
  h.dom.document.dispatch('click', {});
  assert.equal(h.area.menuOpen, false);
});

test('logout goes through the callback after closing the menu', (t) => {
  const h = harness();
  t.after(() => h.dom.restore());
  h.area.setIdentity(ACCOUNTS_ADMIN);
  h.dom.getElement('account-logout').dispatch('click', {});
  assert.deepEqual(h.logouts, [true]);
  assert.equal(h.area.menuOpen, false);
});

test('the invitation dialog generates a one-time code and a copyable link', async (t) => {
  const h = harness();
  t.after(() => h.dom.restore());
  h.area.setIdentity(ACCOUNTS_ADMIN);
  h.area.openDialog();
  assert.equal(h.area.dialogOpen, true);
  assert.equal(h.dom.getElement('invite-dialog').hidden, false);

  h.dom.getElement('invite-email').value = 'new@example.test';
  h.dom.getElement('invite-role').value = 'admin';
  h.dom.getElement('invite-form').dispatch('submit', { preventDefault() {} });
  await tick();
  assert.deepEqual(h.calls, [{ email: 'new@example.test', role: 'admin' }]);
  assert.equal(h.area.inviteCreated, true);
  assert.equal(h.dom.getElement('invite-form').hidden, true);
  assert.equal(h.dom.getElement('invite-result').hidden, false);
  assert.equal(
    h.dom.getElement('invite-link').value,
    'http://127.0.0.1:43210/register#invite=invite-code-value'
  );
  assert.equal(h.dom.getElement('invite-link').value.includes('?'), false, 'no token in the query string');
  assert.equal(h.dom.getElement('invite-link').readOnly || h.dom.getElement('invite-link').hasAttribute('readonly'), true);

  h.dom.getElement('invite-copy').dispatch('click', {});
  await tick();
  assert.equal(h.notices.at(-1).message, '邀请链接已复制。');

  h.dom.getElement('invite-done').dispatch('click', {});
  assert.equal(h.area.dialogOpen, false);
  assert.equal(h.dom.getElement('invite-dialog').hidden, true);
});

test('invitation failures explain the reason without leaking anything else', async (t) => {
  const h = harness({
    createInvitation: async () => {
      throw Object.assign(new Error('email already registered'), { code: 'email_in_use', status: 409 });
    },
  });
  t.after(() => h.dom.restore());
  h.area.setIdentity(ACCOUNTS_ADMIN);
  h.area.openDialog();
  h.dom.getElement('invite-email').value = 'taken@example.test';
  h.dom.getElement('invite-form').dispatch('submit', { preventDefault() {} });
  await tick();
  assert.match(h.dom.getElement('invite-error').textContent, /已经注册/);
  assert.equal(h.dom.getElement('invite-result').hidden, true);

  h.dom.getElement('invite-email').value = '';
  h.dom.getElement('invite-form').dispatch('submit', { preventDefault() {} });
  await tick();
  assert.match(h.dom.getElement('invite-error').textContent, /请填写/);
});

test('Escape closes the invite dialog and returns focus to the entry that opened it (F-1)', (t) => {
  const h = harness();
  t.after(() => h.dom.restore());
  h.area.setIdentity(ACCOUNTS_ADMIN);
  // The path a person takes: account menu → 邀请成员 → Esc.
  h.dom.getElement('account-button').dispatch('click', { stopPropagation() {} });
  h.dom.getElement('account-invite').dispatch('click', { stopPropagation() {} });
  assert.equal(h.area.dialogOpen, true);
  assert.equal(h.area.menuOpen, false, 'opening the dialog puts the menu away');

  h.dom.document.dispatch('keydown', { key: 'Escape' });
  assert.equal(h.area.dialogOpen, false);
  assert.equal(
    h.dom.document.activeElement?.id,
    'account-invite',
    'focus must land back on the 邀请成员 entry, not on <body>',
  );
  assert.equal(
    h.dom.getElement('account-menu').hidden,
    false,
    'a hidden entry cannot hold focus, so the menu has to be shown again',
  );
});

test('the explicit close control hands focus back to the same entry (F-1)', (t) => {
  const h = harness();
  t.after(() => h.dom.restore());
  h.area.setIdentity(ACCOUNTS_ADMIN);
  h.dom.getElement('account-button').dispatch('click', { stopPropagation() {} });
  h.dom.getElement('account-invite').dispatch('click', { stopPropagation() {} });
  assert.equal(h.area.dialogOpen, true);

  h.dom.getElement('invite-cancel').dispatch('click', {});
  assert.equal(h.area.dialogOpen, false);
  assert.equal(h.dom.document.activeElement?.id, 'account-invite');
});

test('dispose detaches the document listeners', (t) => {
  const h = harness();
  h.area.setIdentity(ACCOUNTS_ADMIN);
  h.dom.getElement('account-button').dispatch('click', { stopPropagation() {} });
  assert.equal(h.area.menuOpen, true);
  h.area.dispose();
  h.dom.document.dispatch('click', {});
  // The listener is gone, so the menu stays as it was — no callback into a
  // disposed module.
  assert.equal(h.area.menuOpen, true);
  h.dom.restore();
});
