/**
 * Second probe: interaction flows that need sequencing (stop, follow-up,
 * invitation registration, cross-tenant access, local mode).
 *
 * Usage: node tests/browser/tools/probe2.mjs
 */

import { chromium } from 'playwright';
import { mkdtempSync, mkdirSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { startHost } from '../support/host.mjs';

const REPO_ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..', '..');
const outDir = mkdtempSync(join(tmpdir(), 'glink-harness-probe2-'));
const dump = {};
const snap = async (name, fn) => {
  try {
    dump[name] = await fn();
  } catch (error) {
    dump[name] = { error: String(error) };
  }
};

async function signIn(page, host, user) {
  await page.goto(host.baseUrl, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(600);
  await page.fill('#login-email', host.users[user].email);
  await page.fill('#login-password', host.password);
  await page.click('#login-submit');
  await page.waitForSelector('#app-shell:not([hidden])', { timeout: 15_000 });
  await page.waitForTimeout(500);
}

// ── accounts fixture: stop / follow-up / invitation / cross-tenant ──────────
{
  const stateDir = join(outDir, 'accounts-state');
  mkdirSync(stateDir, { recursive: true });
  const host = await startHost({ repoRoot: REPO_ROOT, mode: 'accounts', stateDir, logFile: join(outDir, 'accounts.log') });
  const browser = await chromium.launch();
  const ctxA = await browser.newContext({ viewport: { width: 1512, height: 900 } });
  const page = await ctxA.newPage();
  await signIn(page, host, 'alphaAdmin');

  // a first run to have history + a parent for the follow-up
  await page.fill('#prompt-input', '[scenario:default] 历史与追问的父问题');
  await page.press('#prompt-input', 'Enter');
  await page.waitForSelector('#run-view[data-status="finished"]', { timeout: 30_000 });
  await page.waitForTimeout(800);
  await snap('runUrl', () => page.evaluate(() => location.href));
  const parentId = await page.evaluate(() => document.querySelector('#run-list [data-run-id]')?.dataset.runId);

  // stop flow
  await page.click('#new-research');
  await page.waitForTimeout(300);
  await page.fill('#prompt-input', '[scenario:slow] 探测停止');
  await page.press('#prompt-input', 'Enter');
  await page.waitForSelector('#run-view[data-status="running"]', { timeout: 20_000 });
  await page.waitForTimeout(1200);
  await snap('slowRunning', () =>
    page.evaluate(() => ({
      status: document.querySelector('#run-view')?.dataset.status,
      stopVisible: !document.querySelector('#stop-run')?.hidden,
      sendHidden: document.querySelector('#send-button')?.hidden,
      readOnly: document.querySelector('#prompt-input')?.readOnly,
      processLabel: document.querySelector('.glink-process-label')?.textContent,
    })),
  );
  await page.click('#stop-run');
  await page.waitForSelector('#run-view[data-status="cancelled"]', { timeout: 30_000 });
  await page.waitForTimeout(900);
  await snap('stopped', () =>
    page.evaluate(() => ({
      status: document.querySelector('#run-view')?.dataset.status,
      tone: document.querySelector('.glink-outcome')?.dataset.tone,
      text: document.querySelector('.glink-outcome')?.innerText?.replace(/\n/g, ' | '),
      icon: document.querySelector('.glink-outcome-icon .icon')?.dataset.icon,
      stopHidden: document.querySelector('#stop-run')?.hidden,
      sendVisible: !document.querySelector('#send-button')?.hidden,
    })),
  );
  await page.screenshot({ path: join(outDir, 'stopped.png') });

  // follow-up on the first (finished) run
  await page.click(`#run-list [data-run-id="${parentId}"]`);
  await page.waitForTimeout(900);
  await snap('replay', () =>
    page.evaluate(() => ({
      status: document.querySelector('#run-view')?.dataset.status,
      answer: document.querySelector('.glink-answer-body')?.innerText?.slice(0, 80),
      outputHidden: document.querySelector('[aria-label="生成的文件"]')?.hidden,
      outputText: document.querySelector('[aria-label="生成的文件"]')?.innerText?.slice(0, 120),
      distance: (() => {
        const scroll = document.querySelector('#conversation-scroll');
        return Math.round(scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight);
      })(),
      foldExpanded: document.querySelector('.glink-process-toggle')?.getAttribute('aria-expanded'),
      composerPlaceholder: document.querySelector('#prompt-input')?.placeholder,
    })),
  );
  await page.fill('#prompt-input', '[scenario:tools] 追问：继续核查');
  await page.press('#prompt-input', 'Enter');
  await page.waitForSelector('#run-view[data-status="finished"]', { timeout: 30_000 });
  await page.waitForTimeout(1000);
  await snap('followup', () =>
    page.evaluate(() => ({
      status: document.querySelector('#run-view')?.dataset.status,
      earlierTurns: document.querySelector('#earlier-turns')?.innerText?.replace(/\n/g, ' | ').slice(0, 300),
      earlierCount: document.querySelectorAll('#earlier-turns > *').length,
      runListRows: document.querySelectorAll('#run-list [data-run-id]').length,
      url: location.href,
    })),
  );
  await page.screenshot({ path: join(outDir, 'followup.png') });

  // invitation dialog
  await page.click('#account-button');
  await page.waitForTimeout(300);
  await page.click('#account-invite');
  await page.waitForSelector('#invite-dialog[data-open="true"]', { timeout: 10_000 });
  await page.waitForTimeout(400);
  await snap('inviteDialog', () =>
    page.evaluate(() => ({
      open: document.querySelector('#invite-dialog')?.dataset.open,
      title: document.querySelector('#invite-title')?.textContent,
      formHidden: document.querySelector('#invite-form')?.hidden,
      roleOptions: [...document.querySelectorAll('#invite-role option')].map((node) => node.textContent),
    })),
  );
  await page.fill('#invite-email', 'member-one@example.test');
  await page.selectOption('#invite-role', 'member');
  await page.click('#invite-submit');
  await page.waitForSelector('#invite-result:not([hidden])', { timeout: 10_000 });
  await page.waitForTimeout(400);
  const inviteLink = await page.inputValue('#invite-link');
  await snap('inviteResult', () =>
    page.evaluate(() => ({
      note: document.querySelector('#invite-result-email')?.textContent,
      link: document.querySelector('#invite-link')?.value?.slice(0, 120),
      copyVisible: !document.querySelector('#invite-copy')?.hidden,
    })),
  );
  await page.click('#invite-done');
  await page.waitForTimeout(300);

  // registration in a fresh context (no cookies)
  const ctxB = await browser.newContext({ viewport: { width: 1512, height: 900 } });
  const pageB = await ctxB.newPage();
  await pageB.goto(inviteLink, { waitUntil: 'domcontentloaded' });
  await pageB.waitForTimeout(800);
  await snap('registerForm', () =>
    pageB.evaluate(() => ({
      url: location.href.replace(/token=[^&]+/, 'token=…'),
      authVisible: !document.querySelector('#auth-view')?.hidden,
      registerVisible: !document.querySelector('#register-form')?.hidden,
      loginVisible: !document.querySelector('#login-form')?.hidden,
      org: document.querySelector('#register-org')?.textContent,
      email: document.querySelector('#register-email')?.value,
      missing: !document.querySelector('#register-missing')?.hidden,
    })),
  );
  await pageB.fill('#register-name', '成员一号');
  await pageB.fill('#register-password', 'member-one-password');
  await pageB.fill('#register-confirm', 'member-one-password');
  await pageB.click('#register-submit');
  try {
    await pageB.waitForSelector('#app-shell:not([hidden])', { timeout: 8_000 });
  } catch {
    await snap('registerFailure', () =>
      pageB.evaluate(() => ({
        url: location.href.replace(/token=[^&]+/, 'token=…'),
        error: document.querySelector('#register-error')?.textContent,
        errorHidden: document.querySelector('#register-error')?.hidden,
        notice: document.querySelector('#auth-notice')?.textContent,
        fields: {
          name: document.querySelector('#register-name')?.value,
          email: document.querySelector('#register-email')?.value,
        },
        body: document.body.innerText.replace(/\n+/g, ' | ').slice(0, 400),
      })),
    );
  }
  await pageB.waitForTimeout(600);
  await snap('registered', () =>
    pageB.evaluate(() => ({
      org: document.querySelector('#account-org')?.textContent,
      name: document.querySelector('#account-name')?.textContent,
      inviteMenuHidden: document.querySelector('#account-invite')?.hidden,
      logoutHidden: document.querySelector('#account-logout')?.hidden,
      rows: document.querySelectorAll('#run-list [data-run-id]').length,
    })),
  );

  // reuse the same invite link
  const ctxC = await browser.newContext();
  const pageC = await ctxC.newPage();
  await pageC.goto(inviteLink, { waitUntil: 'domcontentloaded' });
  await pageC.waitForTimeout(900);
  await snap('inviteReused', () =>
    pageC.evaluate(() => ({
      registerVisible: !document.querySelector('#register-form')?.hidden,
      missing: !document.querySelector('#register-missing')?.hidden,
      notice: document.querySelector('#auth-notice')?.textContent,
      noticeHidden: document.querySelector('#auth-notice')?.hidden,
      body: document.body.innerText.replace(/\n+/g, ' | ').slice(0, 300),
    })),
  );

  // cross-tenant: beta admin tries alpha's run
  const ctxD = await browser.newContext({ viewport: { width: 1512, height: 900 } });
  const pageD = await ctxD.newPage();
  await signIn(pageD, host, 'betaAdmin');
  await snap('betaList', () =>
    pageD.evaluate(() => ({
      rows: document.querySelectorAll('#run-list [data-run-id]').length,
      empty: document.querySelector('#run-list .empty-state')?.textContent ?? null,
    })),
  );
  const crossTenant = await pageD.evaluate(async (runId) => {
    const detail = await fetch(`/api/runs/${runId}`, { headers: { Accept: 'application/json' } });
    const events = await fetch(`/api/runs/${runId}/events`, { headers: { Accept: 'text/event-stream' } });
    const report = await fetch(`/api/runs/${runId}/report`);
    const children = await fetch(`/api/runs/${runId}/children`);
    const stop = await fetch(`/api/runs/${runId}/stop`, { method: 'POST' });
    return {
      detail: detail.status,
      events: events.status,
      report: report.status,
      children: children.status,
      stop: stop.status,
    };
  }, parentId);
  dump.crossTenant = crossTenant;
  await browser.close();
  const exit = await host.stop();
  dump.accountsHostExit = exit;
}

// ── local fixture: single-user surface ──────────────────────────────────────
{
  const stateDir = join(outDir, 'local-state');
  mkdirSync(stateDir, { recursive: true });
  const host = await startHost({ repoRoot: REPO_ROOT, mode: 'local', stateDir, logFile: join(outDir, 'local.log') });
  const browser = await chromium.launch();
  const context = await browser.newContext({ viewport: { width: 1512, height: 900 } });
  const page = await context.newPage();
  await page.goto(host.baseUrl, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(800);
  await snap('localBoot', () =>
    page.evaluate(() => ({
      authHidden: document.querySelector('#auth-view')?.hidden,
      shellVisible: !document.querySelector('#app-shell')?.hidden,
      accountAreaHidden: document.querySelector('#account-area')?.hidden,
      me: null,
    })),
  );
  dump.localMe = await page.evaluate(async () => (await fetch('/api/me')).json());
  await page.fill('#prompt-input', '[scenario:default] 本地模式冒烟');
  await page.press('#prompt-input', 'Enter');
  await page.waitForSelector('#run-view[data-status="finished"]', { timeout: 30_000 });
  await page.waitForTimeout(600);
  await snap('localRun', () =>
    page.evaluate(() => ({
      status: document.querySelector('#run-view')?.dataset.status,
      answer: document.querySelector('.glink-answer-body')?.innerText?.slice(0, 80),
      rows: document.querySelectorAll('#run-list [data-run-id]').length,
    })),
  );
  await browser.close();
  const exit = await host.stop();
  dump.localHostExit = exit;
}

writeFileSync(join(outDir, 'probe2.json'), `${JSON.stringify(dump, null, 2)}\n`, 'utf8');
console.log(JSON.stringify(dump, null, 2));
console.log(`probe2 written to ${outDir}`);
