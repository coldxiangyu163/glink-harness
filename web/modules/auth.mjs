/**
 * Unauthenticated entry: login, invitation registration, and the two recovery
 * faces (gateway-only deployment, service unreachable).
 *
 * The workbench shell stays hidden until `GET /api/me` answers 200: this view is
 * the only thing shown before that, so a previous identity's run list, report or
 * SSE can never flash on screen. It never writes the invitation code or any
 * credential to localStorage/sessionStorage, and it strips the invite from the
 * URL as soon as the server has validated it.
 *
 * All copy lives here; `workspace.mjs` only decides when to show which mode.
 */

const MIN_PASSWORD = 10;

const MODE_TITLES = {
  login: '登录研究工作台',
  register: '创建账号',
  'register-missing': '创建账号',
  gateway: '研究工作台',
  unavailable: '暂时连不上服务',
};

export function createAuthView({ api, onSession = null } = {}) {
  const el = (id) => document.getElementById(id);
  const root = el('auth-view');
  const title = el('auth-title');
  const notice = el('auth-notice');
  const loginForm = el('login-form');
  const loginEmail = el('login-email');
  const loginPassword = el('login-password');
  const loginError = el('login-error');
  const loginSubmit = el('login-submit');
  const loginFoot = el('login-foot');
  const registerForm = el('register-form');
  const registerOrg = el('register-org');
  const registerEmail = el('register-email');
  const registerName = el('register-name');
  const registerPassword = el('register-password');
  const registerConfirm = el('register-confirm');
  const registerError = el('register-error');
  const registerSubmit = el('register-submit');
  const registerFoot = el('register-foot');
  const registerMissing = el('register-missing');
  const gatewayNote = el('gateway-note');
  const unavailableNote = el('unavailable-note');
  const retryButton = el('auth-retry');

  let sessionHandler = onSession;
  let state = 'login';
  let inviteCode = '';
  let invitedEmail = '';
  let busy = false;

  function showNotice(message) {
    notice.textContent = message ?? '';
    notice.hidden = !message;
  }

  function setError(node, message) {
    if (!node) return;
    node.textContent = message ?? '';
    node.hidden = !message;
    if (node.setAttribute) node.setAttribute('aria-hidden', message ? 'false' : 'true');
  }

  function setBusy(node, value, busyLabel, idleLabel) {
    if (!node) return;
    node.disabled = value;
    if (busyLabel && idleLabel) node.textContent = value ? busyLabel : idleLabel;
  }

  function clearForms() {
    setError(loginError, '');
    setError(registerError, '');
    if (loginPassword) loginPassword.value = '';
    if (registerPassword) registerPassword.value = '';
    if (registerConfirm) registerConfirm.value = '';
    if (registerName) registerName.value = '';
  }

  function setVisible(node, visible) {
    if (node) node.hidden = !visible;
  }

  /** Bring the invitation out of the URL once the client has it in hand. */
  function cleanUrl(path) {
    try {
      if (typeof history !== 'undefined' && typeof history.replaceState === 'function') {
        history.replaceState({}, '', path);
      }
    } catch {
      /* a stub or a file:// context — the code still lives in memory only */
    }
  }

  /** Render one of the states; every other face is hidden first. */
  function render(next, { notice: message = '' } = {}) {
    state = next;
    if (root) root.hidden = false;
    if (title) title.textContent = MODE_TITLES[next] ?? MODE_TITLES.login;
    if (typeof document !== 'undefined') document.title = MODE_TITLES[next] ?? '研究工作台';
    setVisible(loginForm, next === 'login');
    setVisible(loginFoot, next === 'login');
    setVisible(registerForm, next === 'register');
    setVisible(registerFoot, next === 'register' || next === 'register-missing');
    setVisible(registerMissing, next === 'register-missing');
    setVisible(gatewayNote, next === 'gateway');
    setVisible(unavailableNote, next === 'unavailable');
    setVisible(retryButton, next === 'unavailable');
    showNotice(message);
    if (registerFoot && next === 'register-missing') {
      // The back link is the only action on the missing-invitation face.
      registerFoot.hidden = false;
    }
  }

  function focusFirst(...nodes) {
    for (const node of nodes) {
      if (node && typeof node.focus === 'function' && !node.hidden) {
        node.focus();
        return;
      }
    }
  }

  async function enterRegister(invite) {
    inviteCode = invite || '';
    if (!inviteCode) {
      render('register-missing', { notice: '需要邀请才能创建账号，请联系组织管理员获取邀请链接。' });
      return;
    }
    let payload;
    try {
      payload = await api.checkInvite(inviteCode);
    } catch (error) {
      if (error?.status === 401) {
        render('login', { notice: '登录状态已过期，请重新登录。' });
        return;
      }
      render('unavailable', { notice: '服务暂时连不上，无法校验邀请。请稍后重试。' });
      return;
    }
    if (!payload?.valid) {
      inviteCode = '';
      render('login', { notice: '这个邀请无效或已经使用，请向管理员索取新的邀请码。' });
      return;
    }
    invitedEmail = payload.invitation.email;
    if (registerOrg) registerOrg.textContent = payload.invitation.tenant?.name ?? '';
    if (registerEmail) {
      registerEmail.value = invitedEmail;
      registerEmail.readOnly = true;
      if (registerEmail.setAttribute) registerEmail.setAttribute('aria-readonly', 'true');
    }
    render('register');
    cleanUrl('/register');
    focusFirst(registerName);
  }

  async function submitLogin(event) {
    if (event && typeof event.preventDefault === 'function') event.preventDefault();
    if (busy) return;
    const email = String(loginEmail?.value ?? '').trim();
    const password = String(loginPassword?.value ?? '');
    setError(loginError, '');
    if (!email || !password) {
      setError(loginError, '请输入邮箱和密码。');
      focusFirst(!email ? loginEmail : loginPassword);
      return;
    }
    busy = true;
    setBusy(loginSubmit, true, '正在登录…', '登录');
    try {
      const me = await api.login(email, password);
      if (sessionHandler) await sessionHandler(me);
    } catch (error) {
      if (error?.status === 401 && error?.code === 'invalid_credentials') {
        setError(loginError, '邮箱或密码不正确，请重试。');
        focusFirst(loginPassword);
      } else if (error?.code === 'rate_limited') {
        setError(loginError, '尝试次数过多，请稍后再试。');
      } else if (error?.code === 'account_disabled' || error?.code === 'tenant_disabled') {
        setError(loginError, '账号或组织已停用，请联系管理员。');
      } else if (error?.status === 0 || error?.status >= 500) {
        setError(loginError, '服务暂时连不上，请稍后重试。');
      } else {
        setError(loginError, '登录没有成功，请稍后重试。');
      }
    } finally {
      busy = false;
      setBusy(loginSubmit, false, '正在登录…', '登录');
    }
  }

  function validateRegistration() {
    const name = String(registerName?.value ?? '').trim();
    const password = String(registerPassword?.value ?? '');
    const confirm = String(registerConfirm?.value ?? '');
    if (!invitedEmail) return '邀请信息不完整，请重新打开邀请链接。';
    if (!name) {
      focusFirst(registerName);
      return '请填写姓名。';
    }
    if (name.length > 64) {
      focusFirst(registerName);
      return '姓名最多 64 个字符。';
    }
    if (password.length < MIN_PASSWORD) {
      focusFirst(registerPassword);
      return `密码至少需要 ${MIN_PASSWORD} 个字符。`;
    }
    if (password.length > 200) {
      focusFirst(registerPassword);
      return '密码最多 200 个字符。';
    }
    if (password !== confirm) {
      focusFirst(registerConfirm);
      return '两次输入的密码不一致。';
    }
    return '';
  }

  async function submitRegister(event) {
    if (event && typeof event.preventDefault === 'function') event.preventDefault();
    if (busy) return;
    setError(registerError, '');
    const problem = validateRegistration();
    if (problem) {
      setError(registerError, problem);
      return;
    }
    busy = true;
    setBusy(registerSubmit, true, '正在创建…', '创建账号');
    try {
      await api.register({
        invite: inviteCode,
        email: invitedEmail,
        name: String(registerName.value).trim(),
        password: String(registerPassword.value),
      });
      const email = invitedEmail;
      inviteCode = '';
      clearForms();
      if (loginEmail) loginEmail.value = email;
      render('login', { notice: '账号已创建，请用刚才的邮箱登录。' });
      cleanUrl('/login');
      focusFirst(loginPassword);
    } catch (error) {
      if (error?.code === 'invitation_invalid') {
        inviteCode = '';
        render('login', { notice: '这个邀请无效或已经使用，请向管理员索取新的邀请码。' });
      } else if (error?.code === 'invalid_password') {
        setError(registerError, `密码至少需要 ${MIN_PASSWORD} 个字符。`);
        focusFirst(registerPassword);
      } else if (error?.code === 'invalid_name') {
        setError(registerError, '请填写姓名（最多 64 个字符）。');
        focusFirst(registerName);
      } else if (error?.code === 'email_in_use') {
        setError(registerError, '这个邮箱已经注册，请直接登录。');
        focusFirst(loginFoot);
      } else if (error?.code === 'rate_limited') {
        setError(registerError, '尝试次数过多，请稍后再试。');
      } else if (error?.status === 0 || error?.status >= 500) {
        setError(registerError, '服务暂时连不上，请稍后重试。');
      } else {
        setError(registerError, '创建账号没有成功，请稍后重试。');
      }
    } finally {
      busy = false;
      setBusy(registerSubmit, false, '正在创建…', '创建账号');
    }
  }

  function goToRegister(event) {
    if (event && typeof event.preventDefault === 'function') event.preventDefault();
    render('register-missing', { notice: '需要邀请才能创建账号，请联系组织管理员获取邀请链接。' });
    cleanUrl('/register');
  }

  function goToLogin(event) {
    if (event && typeof event.preventDefault === 'function') event.preventDefault();
    inviteCode = '';
    invitedEmail = '';
    render('login');
    cleanUrl('/login');
    focusFirst(loginEmail);
  }

  if (loginForm) loginForm.addEventListener('submit', submitLogin);
  if (registerForm) registerForm.addEventListener('submit', submitRegister);
  const toRegister = el('to-register');
  if (toRegister) toRegister.addEventListener('click', goToRegister);
  const toLogin = el('to-login');
  if (toLogin) toLogin.addEventListener('click', goToLogin);
  if (retryButton) {
    retryButton.addEventListener('click', () => {
      if (typeof location !== 'undefined' && typeof location.reload === 'function') location.reload();
    });
  }

  return {
    /** Show one of the unauthenticated faces. */
    async show(options = {}) {
      const target = options.mode ?? 'login';
      clearForms();
      if (target === 'register') {
        render('register', { notice: options.notice ?? '' });
        await enterRegister(options.invite ?? '');
        return;
      }
      if (target === 'register-missing') {
        render('register-missing', {
          notice: options.notice ?? '需要邀请才能创建账号，请联系组织管理员获取邀请链接。',
        });
        return;
      }
      if (target === 'unavailable') {
        render('unavailable', { notice: options.notice ?? '服务暂时连不上，请稍后重试。' });
        return;
      }
      if (target === 'gateway') {
        render('gateway', {
          notice: options.notice ?? '需要通过组织的认证网关访问，请从统一入口登录后重试。',
        });
        return;
      }
      render('login', { notice: options.notice ?? '' });
      focusFirst(loginEmail);
    },
    /** Hide the view (the workbench is taking over). */
    hide() {
      if (root) root.hidden = true;
      clearForms();
      inviteCode = '';
      invitedEmail = '';
    },
    /** Register the post-login callback; `app.js` wires it to the workspace. */
    onSession(handler) {
      sessionHandler = handler;
    },
    get mode() {
      return state;
    },
    get invite() {
      return inviteCode;
    },
  };
}
