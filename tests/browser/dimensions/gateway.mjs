/**
 * Gateway dimension: the multi-tenant deployment surface without in-app
 * accounts. The browser never carries an identity assertion, so this covers
 * exactly what a user without gateway credentials sees.
 */

import { newSession, gotoHome } from '../support/flows.mjs';
import { chromeText } from '../support/harness.mjs';

export async function run(ctx) {
  const { suite, baseUrl } = ctx;
  suite.dimension('gateway-identity');

  const { context, page } = await newSession(ctx);
  await gotoHome(page, baseUrl);
  const face = await page.evaluate(() => ({
    authVisible: !document.querySelector('#auth-view')?.hidden,
    shellHidden: document.querySelector('#app-shell')?.hidden === true,
    title: document.querySelector('#auth-title')?.textContent,
    loginHidden: document.querySelector('#login-form')?.hidden === true,
    registerHidden: document.querySelector('#register-form')?.hidden === true,
    gatewayNote: document.querySelector('#gateway-note')?.hidden === false
      ? document.querySelector('#gateway-note')?.textContent
      : null,
    unavailableHidden: document.querySelector('#unavailable-note')?.hidden === true,
    retryHidden: document.querySelector('#auth-retry')?.hidden === true,
    historyLeak: document.body.innerText.includes('研究记录') || document.body.innerText.includes('今天要研究什么'),
  }));
  suite.record(
    '网关模式未认证入口：只有「从统一入口访问」的说明，没有站内表单',
    face.authVisible && face.shellHidden && face.loginHidden && face.registerHidden &&
      /认证网关/.test(face.gatewayNote || '') && face.unavailableHidden && !face.historyLeak,
    face,
  );

  const me = await page.evaluate(async () => {
    const response = await fetch('/api/me', { headers: { Accept: 'application/json' } });
    return { status: response.status, body: await response.json().catch(() => ({})) };
  });
  suite.record(
    '/api/me：401 + mode=gateway（前端据 mode 选择网关说明）',
    me.status === 401 && me.body.mode === 'gateway',
    me,
  );

  const writes = await page.evaluate(async () => {
    const run = await fetch('/api/runs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Origin: location.origin },
      body: JSON.stringify({ prompt: '网关模式下的匿名研究尝试' }),
    });
    const runs = await fetch('/api/runs', { headers: { Accept: 'application/json' } });
    return { run: run.status, list: runs.status };
  });
  suite.record(
    '网关模式：匿名创建 / 列表读取都被拒绝（401）',
    writes.run === 401 && writes.list === 401,
    writes,
  );

  const crossSite = await page.evaluate(async () => {
    const response = await fetch('/api/runs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Origin: 'https://evil.example' },
      body: JSON.stringify({ prompt: '跨站尝试' }),
    });
    return response.status;
  });
  // Without an identity the request is answered 401 before the origin check
  // runs; either refusal is safe, and neither creates anything.
  suite.record(
    '网关模式：跨站来源的 POST 被拒绝（401 未认证或 403 来源）',
    crossSite === 401 || crossSite === 403,
    crossSite,
  );

  const chrome = await chromeText(page);
  const leaks = ['pi-glink', 'inproc', '内核', 'session ', 'openai/'].filter((word) => chrome.includes(word));
  suite.record('网关入口文字不暴露内核/执行面/会话事实', leaks.length === 0, leaks);

  await suite.shot(page, 'gateway-face');
  await context.close();
}
