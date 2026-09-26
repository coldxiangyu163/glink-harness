/**
 * Identity dimension (accounts mode): unauthenticated face, sign-in, account
 * menu, invitation → registration, sign-out — all through the production UI.
 */

import { readFileSync } from 'node:fs';
import { newSession, gotoHome, signIn, waitShell, csrfToken } from '../support/flows.mjs';

export async function run(ctx) {
  const { suite, baseUrl } = ctx;
  suite.dimension('identity');

  // ── 1. unauthenticated entry ──────────────────────────────────────────────
  const { context: anonContext, page: anon } = await newSession(ctx);
  await gotoHome(anon, baseUrl);
  const anonFace = await anon.evaluate(() => ({
    authVisible: !document.querySelector('#auth-view')?.hidden,
    shellHidden: document.querySelector('#app-shell')?.hidden === true,
    title: document.querySelector('#auth-title')?.textContent,
    loginVisible: !document.querySelector('#login-form')?.hidden,
    registerHidden: document.querySelector('#register-form')?.hidden === true,
    gitwayNoteHidden: document.querySelector('#gateway-note')?.hidden,
    bodyText: document.body.innerText.replace(/\n+/g, ' | ').slice(0, 200),
    shellLeak: document.body.innerText.includes('今天要研究什么') || document.body.innerText.includes('研究记录'),
  }));
  suite.record(
    '未登录入口：单卡登录面、工作台外壳不揭示、也无历史闪现',
    anonFace.authVisible && anonFace.shellHidden && anonFace.title === '登录研究工作台' &&
      anonFace.loginVisible && anonFace.registerHidden && !anonFace.shellLeak,
    anonFace,
  );
  const anonApi = await anon.evaluate(async () => {
    const response = await fetch('/api/runs', { headers: { Accept: 'application/json' } });
    const payload = await response.json().catch(() => ({}));
    return { status: response.status, mode: payload.mode, error: payload.error };
  });
  suite.record(
    '未登录 API：/api/runs 401 且带 mode=accounts',
    anonApi.status === 401 && anonApi.mode === 'accounts',
    anonApi,
  );

  // ── 2. rejected sign-in ───────────────────────────────────────────────────
  const admin = ctx.fixture.users.alphaAdmin;
  await anon.fill('#login-email', admin.email);
  await anon.fill('#login-password', 'definitely-wrong');
  await anon.click('#login-submit');
  await anon.waitForTimeout(900);
  const badLogin = await anon.evaluate(() => ({
    error: document.querySelector('#login-error')?.textContent,
    errorHidden: document.querySelector('#login-error')?.hidden,
    shellHidden: document.querySelector('#app-shell')?.hidden === true,
    passwordValue: document.querySelector('#login-password')?.value,
  }));
  suite.record(
    '登录失败：字段旁给出同形错误、不进入工作台',
    badLogin.errorHidden === false && /不正确/.test(badLogin.error || '') && badLogin.shellHidden,
    badLogin,
  );

  // ── 3. successful sign-in ─────────────────────────────────────────────────
  await signIn(ctx, anon);
  const signedIn = await anon.evaluate(() => ({
    accountVisible: !document.querySelector('#account-area')?.hidden,
    org: document.querySelector('#account-org')?.textContent,
    name: document.querySelector('#account-name')?.textContent,
    avatar: document.querySelector('#account-avatar')?.textContent,
    view: document.querySelector('#app-shell')?.dataset.view,
  }));
  suite.record(
    '登录成功：账号区显示组织与姓名（首字母头像）',
    signedIn.accountVisible && signedIn.org === '组织甲' && signedIn.name === '管理员甲' &&
      signedIn.avatar === '管' && signedIn.view === 'home',
    signedIn,
  );
  const cookies = await anonContext.cookies(baseUrl);
  const sessionCookie = cookies.find((cookie) => /session/i.test(cookie.name));
  suite.record(
    '会话 Cookie：HttpOnly 且非 Secure 的 loopback 会话（口令不进 localStorage）',
    Boolean(sessionCookie) && sessionCookie.httpOnly === true,
    sessionCookie ? { name: sessionCookie.name, httpOnly: sessionCookie.httpOnly, sameSite: sessionCookie.sameSite } : null,
  );
  const storage = await anon.evaluate(() => ({
    keys: Object.keys(localStorage).filter((key) => /token|session|auth|key|password/i.test(key)),
  }));
  suite.record('浏览器存储：没有会话/凭据类键', storage.keys.length === 0, storage);

  // ── 4. account menu ───────────────────────────────────────────────────────
  await anon.click('#account-button');
  await anon.waitForTimeout(300);
  const menu = await anon.evaluate(() => ({
    hidden: document.querySelector('#account-menu')?.hidden,
    expanded: document.querySelector('#account-button')?.getAttribute('aria-expanded'),
    org: document.querySelector('#account-menu-org')?.textContent,
    email: document.querySelector('#account-menu-email')?.textContent,
    role: document.querySelector('#account-menu-role')?.textContent,
    inviteVisible: !document.querySelector('#account-invite')?.hidden,
    logoutVisible: !document.querySelector('#account-logout')?.hidden,
  }));
  suite.record(
    '账号菜单：管理员可见组织/邮箱/角色 + 邀请成员 + 退出登录',
    menu.hidden === false && menu.expanded === 'true' &&
      menu.org === '组织：组织甲' && menu.email === `邮箱：${admin.email}` &&
      menu.role === '角色：管理员' && menu.inviteVisible && menu.logoutVisible,
    menu,
  );
  await anon.keyboard.press('Escape');
  await anon.waitForTimeout(250);

  // ── 5. invite dialog + one-time link ──────────────────────────────────────
  await anon.click('#account-button');
  await anon.waitForTimeout(250);
  await anon.click('#account-invite');
  await anon.waitForSelector('#invite-dialog[data-open="true"]', { timeout: 10_000 });
  await anon.waitForTimeout(300);
  const dialog = await anon.evaluate(() => ({
    open: document.querySelector('#invite-dialog')?.dataset.open,
    title: document.querySelector('#invite-title')?.textContent,
    roles: [...document.querySelectorAll('#invite-role option')].map((node) => node.textContent),
  }));
  suite.record(
    '邀请对话框：标题、角色选项（成员/管理员）',
    dialog.open === 'true' && dialog.title === '邀请成员' &&
      dialog.roles.join(',') === '成员,管理员',
    dialog,
  );
  const inviteEmail = 'browser-member@example.test';
  await anon.fill('#invite-email', inviteEmail);
  await anon.selectOption('#invite-role', 'member');
  await anon.click('#invite-submit');
  await anon.waitForSelector('#invite-result:not([hidden])', { timeout: 10_000 });
  await anon.waitForTimeout(300);
  const invite = await anon.evaluate(() => ({
    note: document.querySelector('#invite-result-email')?.textContent,
    link: document.querySelector('#invite-link')?.value,
    linkReadonly: document.querySelector('#invite-link')?.readOnly,
  }));
  const tokenMatch = /#invite=([A-Za-z0-9_-]+)/.exec(invite.link || '');
  suite.record(
    '邀请结果：只显示一次的链接（token 在 URL hash，不进请求行）',
    Boolean(tokenMatch) && /只显示这一次/.test(invite.note || '') && invite.linkReadonly === true,
    { note: invite.note, link: (invite.link || '').replace(/#invite=.*/, '#invite=…') },
  );
  await anon.click('#invite-done');
  await anon.waitForTimeout(300);
  // The dialog closes on Escape and returns focus to its opener.
  await anon.click('#account-button');
  await anon.waitForTimeout(250);
  await anon.click('#account-invite');
  await anon.waitForSelector('#invite-dialog[data-open="true"]', { timeout: 10_000 });
  await anon.waitForTimeout(300);
  await anon.keyboard.press('Escape');
  await anon.waitForTimeout(300);
  const inviteEsc = await anon.evaluate(() => ({
    open: document.querySelector('#invite-dialog')?.dataset.open,
    hidden: document.querySelector('#invite-dialog')?.hidden,
    focus: document.activeElement?.id,
    focusTag: document.activeElement?.tagName,
    inviteVisible: document.querySelector('#account-invite')?.offsetParent !== null,
    accountButtonVisible: document.querySelector('#account-button')?.offsetParent !== null,
  }));
  suite.record(
    '邀请对话框：Esc 关闭并把焦点还给入口按钮',
    inviteEsc.hidden === true && inviteEsc.open === 'false' && inviteEsc.focus === 'account-invite',
    inviteEsc,
  );
  const menuStillOpen = await anon.evaluate(() => document.querySelector('#account-menu')?.hidden === false);
  if (menuStillOpen) {
    await anon.keyboard.press('Escape');
    await anon.waitForTimeout(250);
  }

  // the invitation token must never appear in a request line
  if (ctx.fixture.requestLog) {
    const logText = readFileSync(ctx.fixture.requestLog, 'utf8');
    suite.record(
      '服务端请求日志不含邀请 token（hash 只留在浏览器）',
      tokenMatch ? !logText.includes(tokenMatch[1]) : true,
      { logBytes: logText.length },
    );
  } else {
    suite.skip('服务端请求日志不含邀请 token', 'fixture 未开启 --request-log');
  }

  // ── 6. registration through the invite link ───────────────────────────────
  const { context: memberContext, page: member } = await newSession(ctx);
  await member.goto(invite.link, { waitUntil: 'domcontentloaded' });
  await member.waitForTimeout(800);
  // Field-level validation first: a short password and a mismatched
  // confirmation must be refused next to the fields, without consuming the
  // invitation.
  await member.fill('#register-name', '浏览器成员');
  await member.fill('#register-password', 'short');
  await member.fill('#register-confirm', 'short');
  await member.click('#register-submit');
  await member.waitForTimeout(500);
  const shortPassword = await member.evaluate(() => ({
    error: document.querySelector('#register-error')?.textContent,
    errorHidden: document.querySelector('#register-error')?.hidden,
    stillOnForm: !document.querySelector('#register-form')?.hidden,
  }));
  await member.fill('#register-password', 'browser-member-password');
  await member.fill('#register-confirm', 'browser-member-password-x');
  await member.click('#register-submit');
  await member.waitForTimeout(500);
  const mismatch = await member.evaluate(() => ({
    error: document.querySelector('#register-error')?.textContent,
    stillOnForm: !document.querySelector('#register-form')?.hidden,
  }));
  suite.record(
    '注册校验：过短密码与两次不一致都在字段旁拒绝、不提交',
    shortPassword.stillOnForm && /10/.test(shortPassword.error || '') &&
      mismatch.stillOnForm && /不一致/.test(mismatch.error || ''),
    { short: shortPassword, mismatch },
  );
  const registerForm = await member.evaluate(() => ({
    authVisible: !document.querySelector('#auth-view')?.hidden,
    registerVisible: !document.querySelector('#register-form')?.hidden,
    loginHidden: document.querySelector('#login-form')?.hidden === true,
    org: document.querySelector('#register-org')?.textContent,
    email: document.querySelector('#register-email')?.value,
    emailReadonly: document.querySelector('#register-email')?.readOnly,
    missingHidden: document.querySelector('#register-missing')?.hidden === true,
  }));
  suite.record(
    '邀请链接 → 注册表单：组织与受邀邮箱只读预填',
    registerForm.authVisible && registerForm.registerVisible && registerForm.loginHidden &&
      registerForm.org === '组织甲' && registerForm.email === inviteEmail &&
      registerForm.emailReadonly && registerForm.missingHidden,
    registerForm,
  );
  const memberPassword = 'browser-member-password';
  await member.fill('#register-name', '浏览器成员');
  await member.fill('#register-password', memberPassword);
  await member.fill('#register-confirm', memberPassword);
  await member.click('#register-submit');
  await member.waitForSelector('#auth-notice:not([hidden])', { timeout: 12_000 });
  await member.waitForTimeout(400);
  const registered = await member.evaluate(() => ({
    notice: document.querySelector('#auth-notice')?.textContent,
    loginVisible: !document.querySelector('#login-form')?.hidden,
    shellHidden: document.querySelector('#app-shell')?.hidden === true,
  }));
  suite.record(
    '注册成功：回到登录面并提示用新账号登录（不自动登录）',
    /账号已创建/.test(registered.notice || '') && registered.loginVisible && registered.shellHidden,
    registered,
  );

  // ── 7. the member signs in: no invite menu, empty history ─────────────────
  await member.fill('#login-email', inviteEmail);
  await member.fill('#login-password', memberPassword);
  await member.click('#login-submit');
  await member.waitForSelector('#app-shell:not([hidden])', { timeout: 15_000 });
  await member.waitForTimeout(500);
  await member.click('#account-button');
  await member.waitForTimeout(300);
  const memberMenu = await member.evaluate(() => ({
    name: document.querySelector('#account-name')?.textContent,
    role: document.querySelector('#account-menu-role')?.textContent,
    inviteHidden: document.querySelector('#account-invite')?.hidden,
    logoutVisible: !document.querySelector('#account-logout')?.hidden,
    rows: document.querySelectorAll('#run-list [data-run-id]').length,
    emptyTitle: document.querySelector('#run-list .empty-state-title')?.textContent,
  }));
  suite.record(
    '普通成员：菜单无「邀请成员」、有退出；历史隔离为空',
    memberMenu.name === '浏览器成员' && memberMenu.role === '角色：成员' &&
      memberMenu.inviteHidden === true && memberMenu.logoutVisible &&
      memberMenu.rows === 0 && memberMenu.emptyTitle === '还没有研究记录',
    memberMenu,
  );
  ctx.member = { context: memberContext, page: member };

  // ── 8. the invitation is one-time ─────────────────────────────────────────
  const { context: reuseContext, page: reuse } = await newSession(ctx);
  await reuse.goto(invite.link, { waitUntil: 'domcontentloaded' });
  await reuse.waitForTimeout(900);
  const reused = await reuse.evaluate(() => ({
    notice: document.querySelector('#auth-notice')?.textContent,
    registerHidden: document.querySelector('#register-form')?.hidden === true,
    body: document.body.innerText.replace(/\n+/g, ' | ').slice(0, 200),
  }));
  suite.record(
    '邀请码一次性：再次打开提示无效/已使用且不出注册表单',
    /无效或已经使用/.test(reused.notice || '') && reused.registerHidden,
    reused,
  );
  await reuseContext.close();

  // ── 9. sign-out ───────────────────────────────────────────────────────────
  const menuOpen = await anon.evaluate(() => document.querySelector('#account-menu')?.hidden === false);
  if (!menuOpen) {
    await anon.click('#account-button');
    await anon.waitForTimeout(300);
  }
  await anon.click('#account-logout');
  await anon.waitForSelector('#auth-view:not([hidden])', { timeout: 10_000 });
  await anon.waitForTimeout(600);
  const signedOut = await anon.evaluate(() => ({
    authVisible: !document.querySelector('#auth-view')?.hidden,
    shellHidden: document.querySelector('#app-shell')?.hidden === true,
    historyLeak: document.body.innerText.includes('研究记录') || document.body.innerText.includes('管理员甲'),
  }));
  suite.record(
    '注销：回到登录面、账号与历史内容不闪现',
    signedOut.authVisible && signedOut.shellHidden && !signedOut.historyLeak,
    signedOut,
  );
  const afterLogout = await anon.evaluate(async () => {
    const response = await fetch('/api/runs', { headers: { Accept: 'application/json' } });
    return response.status;
  });
  suite.record('注销后 API：会话立即失效（401）', afterLogout === 401, afterLogout);
  await anon.reload({ waitUntil: 'domcontentloaded' });
  await anon.waitForTimeout(700);
  const reloadAfterLogout = await anon.evaluate(() => ({
    authVisible: !document.querySelector('#auth-view')?.hidden,
    shellHidden: document.querySelector('#app-shell')?.hidden === true,
  }));
  suite.record(
    '注销后刷新：仍是登录面（服务端会话已废止）',
    reloadAfterLogout.authVisible && reloadAfterLogout.shellHidden,
    reloadAfterLogout,
  );

  await anonContext.close();
  return { memberEmail: inviteEmail, memberPassword };
}
