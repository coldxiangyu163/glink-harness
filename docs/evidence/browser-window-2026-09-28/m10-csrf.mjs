// PLAYWRIGHT_PATH=... node m10-csrf.mjs <main-repo-path> <out-dir>
// Two independent page JS realms within ONE cookie-sharing browser context, against a disposable real accounts host.
import { createRequire } from 'node:module';
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { startHost } from '../../../tests/browser/support/host.mjs';
const { chromium } = createRequire(import.meta.url)(process.env.PLAYWRIGHT_PATH);
const [repoRoot, out] = process.argv.slice(2);
const stateDir = mkdtempSync(join(tmpdir(), 'glink-browserwin-m10.'));
let host, browser;
const timeline = [];
try {
  host = await startHost({ repoRoot, mode: 'accounts', stateDir,
    logFile: join(stateDir, 'fixture.log'), requestLog: join(stateDir, 'requests.jsonl') });
  timeline.push({ event: 'host_ready', pid: host.pid, port: host.port });
  browser = await chromium.launch();
  const context = await browser.newContext({ viewport: { width: 1512, height: 900 } });
  const a = await context.newPage();
  const b = await context.newPage();
  const seen = [];
  a.on('response', async response => {
    const path = new URL(response.url()).pathname;
    if (!['/api/runs', '/api/me'].includes(path)) return;
    let error = null;
    if (response.status() >= 400) error = (await response.json().catch(() => ({}))).error;
    seen.push({ path, method: response.request().method(), status: response.status(), error });
  });
  const signIn = async (page) => {
    await page.fill('#login-email', host.users.alphaAdmin.email);
    await page.fill('#login-password', host.password);
    await page.click('#login-submit');
    await page.waitForSelector('#app-shell:not([hidden])');
  };
  await a.goto(host.baseUrl);
  await a.waitForSelector('#login-form:not([hidden])');
  await signIn(a);
  timeline.push({ event: 'A_logged_in', mode: 'accounts' });
  await b.goto(host.baseUrl);
  await b.waitForSelector('#app-shell:not([hidden])');
  await b.click('#account-button');
  await b.click('#account-logout');
  await b.waitForSelector('#login-form:not([hidden])');
  await signIn(b);
  timeline.push({ event: 'B_relogin_cookie_rotated', cookieShared: true });
  const initialCount = seen.length;
  await a.fill('#prompt-input', 'M10 CSRF 透明恢复：请简短回答一句，并写入 report.md [scenario:tools]');
  await a.press('#prompt-input', 'Enter');
  await a.waitForSelector('#run-view[data-status="finished"]', { timeout: 60000 });
  const recovery = seen.slice(initialCount);
  const recovered = recovery.some(x => x.path === '/api/runs' && x.method === 'POST' && x.status === 403 && x.error === 'csrf_invalid') &&
    recovery.some(x => x.path === '/api/me' && x.method === 'GET' && x.status === 200) &&
    recovery.some(x => x.path === '/api/runs' && x.method === 'POST' && x.status === 201);
  const successFace = await a.evaluate(() => ({ status: document.querySelector('#run-view')?.dataset.status,
    toastVisible: document.querySelector('#toast')?.hidden === false, bootFailureVisible: document.querySelector('#boot-failure')?.hidden === false }));
  timeline.push({ event: 'A_POST_recovered', requests: recovery, successFace, recovered });
  await a.screenshot({ path: join(out, 'm10-recovered.png') });
  // A real server-generated, NON-CSRF 403: override only this request's Origin to a foreign origin.
  const forbidden = [];
  await a.route('**/api/runs', async route => {
    if (route.request().method() !== 'POST') return route.continue();
    const rejected = await route.fetch({ headers: { ...route.request().headers(), Origin: 'https://foreign.invalid' } });
    await route.fulfill({ response: rejected });
  });
  a.on('response', async response => {
    if (new URL(response.url()).pathname === '/api/runs' && response.request().method() === 'POST') {
      const payload = await response.json().catch(() => ({}));
      forbidden.push({ status: response.status(), error: payload.error });
    }
  });
  await a.click('#new-research');
  await a.fill('#prompt-input', 'M10 非 CSRF 403 必须失败，不得重试');
  const deniedResponse = a.waitForResponse(r => new URL(r.url()).pathname === '/api/runs' && r.request().method() === 'POST', { timeout: 12000 });
  await a.press('#prompt-input', 'Enter');
  await deniedResponse;
  await a.waitForTimeout(200);
  const failFace = await a.evaluate(() => ({ toast: document.querySelector('#toast-text')?.textContent,
    toastHidden: document.querySelector('#toast')?.hidden,
    view: document.querySelector('#app-shell')?.dataset.view,
    status: document.querySelector('#run-view')?.dataset.status }));
  timeline.push({ event: 'non_csrf_403', requests: forbidden, failFace });
  await a.screenshot({ path: join(out, 'm10-non-csrf-403.png') });
  const result = { host: { pid: host.pid, port: host.port, mode: 'accounts' }, timeline };
  writeFileSync(join(out, 'm10-sequence.json'), JSON.stringify(result, null, 2) + '\n');
  console.log(JSON.stringify({ recovered, recovery, successFace, forbidden, failFace }));
  if (!recovered || successFace.status !== 'finished' || !forbidden.some(x => x.status === 403 && x.error === 'forbidden') || forbidden.length !== 1 || failFace.toastHidden || !failFace.toast) process.exitCode = 1;
} finally {
  if (browser) await browser.close();
  if (host) {
    const stopped = await host.stop();
    timeline.push({ event: 'host_stopped', pid: host.pid, ...stopped });
    writeFileSync(join(out, 'm10-sequence.json'), JSON.stringify({ host: { pid: host.pid, port: host.port, mode: 'accounts' }, timeline }, null, 2) + '\n');
  }
  rmSync(stateDir, { recursive: true, force: true });
  console.log(JSON.stringify({ temporaryStateRemoved: true, hostPid: host?.pid, port: host?.port }));
}
