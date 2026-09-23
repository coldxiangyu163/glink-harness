/**
 * Sidebar account area: the real organisation/name of the signed-in identity,
 * its account menu, and the admin's invitation dialog.
 *
 * The identity always comes from the server (`GET /api/me`); nothing here reads
 * the URL, run history or browser storage to guess an organisation, and there is
 * deliberately no organisation switcher (the product has one identity per
 * session). In `gateway` mode the area shows the gateway-provided identity only —
 * no logout, because this host does not own that session.
 */

const ROLE_LABELS = { member: '成员', admin: '管理员' };

function initialOf(identity) {
  const source = identity?.user?.name || identity?.tenant?.name || identity?.user?.id || '';
  const text = String(source).trim();
  return text ? Array.from(text)[0] : '·';
}

export function createAccountArea({ api, onLogout = null, onNotice = null } = {}) {
  const el = (id) => document.getElementById(id);
  const area = el('account-area');
  const button = el('account-button');
  const avatar = el('account-avatar');
  const orgNode = el('account-org');
  const nameNode = el('account-name');
  const menu = el('account-menu');
  const menuOrg = el('account-menu-org');
  const menuEmail = el('account-menu-email');
  const menuRole = el('account-menu-role');
  const inviteItem = el('account-invite');
  const logoutItem = el('account-logout');

  const dialog = el('invite-dialog');
  const form = el('invite-form');
  const emailInput = el('invite-email');
  const roleSelect = el('invite-role');
  const errorNode = el('invite-error');
  const submitButton = el('invite-submit');
  const cancelButton = el('invite-cancel');
  const result = el('invite-result');
  const resultEmail = el('invite-result-email');
  const linkInput = el('invite-link');
  const copyButton = el('invite-copy');
  const doneButton = el('invite-done');

  let identity = null;
  let menuOpen = false;
  let dialogOpen = false;
  let busy = false;
  let created = false;

  const isAdmin = () => identity?.role === 'admin' || identity?.user?.role === 'admin';
  const isAccounts = () => identity?.mode === 'accounts';

  function announce(message, level = 'info') {
    if (onNotice) onNotice(message, level);
  }

  function setError(message) {
    if (!errorNode) return;
    errorNode.textContent = message ?? '';
    errorNode.hidden = !message;
  }

  function setBusy(value) {
    busy = value;
    if (submitButton) {
      submitButton.disabled = value;
      submitButton.textContent = value ? '正在生成…' : '生成邀请';
    }
  }

  function labelRole() {
    if (!menuRole) return;
    const role = identity?.user?.role ?? identity?.role ?? '';
    menuRole.textContent = `角色：${ROLE_LABELS[role] ?? role}`;
  }

  function setIdentity(next) {
    identity = next && next.mode !== 'local' && next.authenticated !== false ? next : null;
    const visible = Boolean(identity);
    if (area) area.hidden = !visible;
    if (!visible) {
      closeMenu();
      closeDialog();
      return;
    }
    if (avatar) avatar.textContent = initialOf(identity);
    const name = identity.user?.name ?? identity.user?.id ?? '';
    const org = identity.tenant?.name ?? identity.tenant?.id ?? '';
    if (orgNode) orgNode.textContent = org;
    if (nameNode) nameNode.textContent = name;
    if (menuOrg) menuOrg.textContent = `组织：${org}`;
    if (menuEmail) {
      const email = identity.user?.email;
      menuEmail.textContent = email ? `邮箱：${email}` : `账号：${identity.user?.id ?? ''}`;
      menuEmail.hidden = !email;
    }
    labelRole();
    if (inviteItem) {
      inviteItem.hidden = !(isAccounts() && isAdmin());
    }
    if (logoutItem) logoutItem.hidden = !isAccounts();
    if (button) {
      button.setAttribute(
        'aria-label',
        visible ? `账号：${name}（${org}）` : '账号'
      );
      if (button.dataset) button.dataset.tip = `${name} · ${org}`;
    }
  }

  // -- account menu ----------------------------------------------------------

  function openMenu() {
    if (!menu || menuOpen) return;
    menuOpen = true;
    menu.hidden = false;
    if (button) button.setAttribute('aria-expanded', 'true');
  }

  function closeMenu({ focus = false } = {}) {
    if (!menu || !menuOpen) return;
    menuOpen = false;
    menu.hidden = true;
    if (button) button.setAttribute('aria-expanded', 'false');
    if (focus && button && typeof button.focus === 'function') button.focus();
  }

  if (button) {
    button.addEventListener('click', (event) => {
      if (event && typeof event.stopPropagation === 'function') event.stopPropagation();
      if (menuOpen) closeMenu({ focus: true });
      else openMenu();
    });
  }
  const onDocumentClick = () => closeMenu();
  const onDocumentKeydown = (event) => {
    if (event?.key !== 'Escape') return;
    if (dialogOpen) {
      closeDialog({ focus: true });
    } else if (menuOpen) {
      closeMenu({ focus: true });
    }
  };
  const onWindowBlur = () => closeMenu();
  if (typeof document !== 'undefined' && document.addEventListener) {
    document.addEventListener('click', onDocumentClick);
    document.addEventListener('keydown', onDocumentKeydown);
  }
  if (typeof window !== 'undefined' && window.addEventListener) {
    window.addEventListener('blur', onWindowBlur);
  }
  if (logoutItem) {
    logoutItem.addEventListener('click', () => {
      closeMenu();
      if (onLogout) onLogout();
    });
  }

  // -- invitation dialog -----------------------------------------------------

  function openDialog() {
    closeMenu();
    if (!dialog) return;
    dialogOpen = true;
    created = false;
    setError('');
    if (form) form.hidden = false;
    if (result) result.hidden = true;
    if (linkInput) linkInput.value = '';
    if (emailInput) emailInput.value = '';
    if (roleSelect) roleSelect.value = 'member';
    dialog.hidden = false;
    if (dialog.dataset) dialog.dataset.open = 'true';
    dialog.inert = false;
    if (emailInput && typeof emailInput.focus === 'function') emailInput.focus();
  }

  function closeDialog({ focus = false } = {}) {
    if (!dialog || !dialogOpen) return;
    dialogOpen = false;
    dialog.hidden = true;
    if (dialog.dataset) dialog.dataset.open = 'false';
    dialog.inert = true;
    setError('');
    if (form) form.hidden = false;
    if (result) result.hidden = true;
    if (inviteItem && focus && typeof inviteItem.focus === 'function') inviteItem.focus();
  }

  async function submitInvite(event) {
    if (event && typeof event.preventDefault === 'function') event.preventDefault();
    if (busy || !dialogOpen) return;
    const email = String(emailInput?.value ?? '').trim();
    const role = roleSelect?.value === 'admin' ? 'admin' : 'member';
    setError('');
    if (!email) {
      setError('请填写受邀人的邮箱。');
      if (emailInput && emailInput.focus) emailInput.focus();
      return;
    }
    setBusy(true);
    try {
      const payload = await api.createInvitation(email, role);
      const invitation = payload?.invitation ?? {};
      created = true;
      const path = `/register?invite=${encodeURIComponent(invitation.code ?? '')}`;
      const origin = typeof location !== 'undefined' && location.origin ? location.origin : '';
      if (linkInput) {
        linkInput.value = `${origin}${path}`;
        linkInput.readOnly = true;
        if (linkInput.setAttribute) linkInput.setAttribute('aria-readonly', 'true');
      }
      if (resultEmail) {
        resultEmail.textContent = `邀请已生成：${invitation.email ?? email}（${ROLE_LABELS[role] ?? role}）。邀请码只显示这一次。`;
      }
      if (form) form.hidden = true;
      if (result) result.hidden = false;
      if (doneButton && typeof doneButton.focus === 'function') doneButton.focus();
    } catch (error) {
      if (error?.code === 'invalid_email') setError('邮箱格式不正确。');
      else if (error?.code === 'invalid_role') setError('角色只能是成员或管理员。');
      else if (error?.code === 'email_in_use') setError('这个邮箱已经注册，不需要再邀请。');
      else if (error?.code === 'forbidden') setError('只有管理员可以邀请成员。');
      else if (error?.code === 'rate_limited') setError('尝试次数过多，请稍后再试。');
      else if (error?.status === 401) setError('登录状态已过期，请重新登录。');
      else if (error?.status === 0 || error?.status >= 500) setError('服务暂时连不上，请稍后重试。');
      else setError('邀请没有生成，请稍后重试。');
    } finally {
      setBusy(false);
    }
  }

  async function copyLink() {
    const value = String(linkInput?.value ?? '');
    if (!value) return;
    try {
      if (typeof navigator !== 'undefined' && navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(value);
        announce('邀请链接已复制。', 'success');
      } else {
        throw new Error('clipboard unavailable');
      }
    } catch {
      try {
        if (linkInput && typeof linkInput.select === 'function') linkInput.select();
      } catch {
        /* selection is best-effort */
      }
      announce('复制没有成功，请手动选择链接复制。', 'warning');
    }
  }

  if (inviteItem) {
    inviteItem.addEventListener('click', (event) => {
      if (event && typeof event.stopPropagation === 'function') event.stopPropagation();
      openDialog();
    });
  }
  if (form) form.addEventListener('submit', submitInvite);
  if (cancelButton) cancelButton.addEventListener('click', () => closeDialog({ focus: true }));
  if (doneButton) doneButton.addEventListener('click', () => closeDialog({ focus: true }));
  if (copyButton) copyButton.addEventListener('click', copyLink);
  const scrim = el('invite-scrim');
  if (scrim) scrim.addEventListener('click', () => closeDialog({ focus: true }));

  return {
    setIdentity,
    openDialog,
    closeDialog,
    dispose() {
      if (typeof document !== 'undefined' && document.removeEventListener) {
        document.removeEventListener('click', onDocumentClick);
        document.removeEventListener('keydown', onDocumentKeydown);
      }
      if (typeof window !== 'undefined' && window.removeEventListener) {
        window.removeEventListener('blur', onWindowBlur);
      }
    },
    get identity() {
      return identity;
    },
    get menuOpen() {
      return menuOpen;
    },
    get dialogOpen() {
      return dialogOpen;
    },
    get inviteCreated() {
      return created;
    },
  };
}
