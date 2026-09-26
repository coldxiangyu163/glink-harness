/**
 * Shared browser flows for the acceptance dimensions.
 *
 * Every flow drives the real UI (typing, clicking, keyboard) — the suite never
 * reaches around the front-end to fabricate a state.
 */

import { visible } from './harness.mjs';

export const VIEWPORT = { width: 1512, height: 900 };

/** Create a fresh browser context/page wired into the suite's error collection. */
export async function newSession(ctx, { viewport = VIEWPORT } = {}) {
  const context = await ctx.browser.newContext({ viewport });
  const page = await context.newPage();
  ctx.suite.watch(page);
  const consoleErrors = [];
  page.on('console', (message) => {
    if (message.type() === 'error') consoleErrors.push(message.text());
  });
  return { context, page, consoleErrors };
}

export async function gotoHome(page, baseUrl) {
  await page.goto(baseUrl, { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(
    () => !document.querySelector('#app-shell')?.hidden || !document.querySelector('#auth-view')?.hidden,
    { timeout: 15_000 },
  );
  await page.waitForTimeout(300);
}

/** Sign in through the real form (accounts mode). */
export async function signIn(ctx, page, userKey = 'alphaAdmin') {
  const user = ctx.fixture.users[userKey];
  await gotoHome(page, ctx.baseUrl);
  await page.fill('#login-email', user.email);
  await page.fill('#login-password', ctx.fixture.password);
  await page.click('#login-submit');
  await page.waitForSelector('#app-shell:not([hidden])', { timeout: 15_000 });
  await page.waitForTimeout(400);
  return user;
}

/** Wait for the shell (local mode or an already-signed-in page). */
export async function waitShell(page, timeout = 15_000) {
  await page.waitForSelector('#app-shell:not([hidden])', { timeout });
  await page.waitForTimeout(300);
}

export async function submitPrompt(page, prompt) {
  await page.fill('#prompt-input', prompt);
  await page.waitForTimeout(120);
  await page.press('#prompt-input', 'Enter');
}

export async function waitRunStatus(page, statuses, timeout = 40_000) {
  const selector = statuses.map((status) => `#run-view[data-status="${status}"]`).join(', ');
  await page.waitForSelector(selector, { timeout });
  await page.waitForTimeout(300);
}

/** Open a run from the sidebar list (the only UI entry point). */
export async function openRunFromList(page, runId) {
  await page.click(`#run-list [data-run-id="${runId}"]`);
  await page.waitForSelector(`#run-view[data-status]`, { timeout: 15_000 });
  await page.waitForTimeout(700);
}

/** Run ids currently listed in the sidebar, in DOM order. */
export async function listedRunIds(page) {
  return page.$$eval('#run-list [data-run-id]', (nodes) => nodes.map((node) => node.dataset.runId));
}

/** First run id in the sidebar list. */
export async function firstListedRunId(page) {
  const ids = await listedRunIds(page);
  return ids[0] ?? null;
}

/** Read the run id currently rendered in `#run-view` (from the list selection). */
export async function currentRunId(page) {
  return page.evaluate(() => {
    const row = document.querySelector('#run-list [aria-current="page"], #run-list .is-current');
    return row?.dataset.runId ?? null;
  });
}

export async function isVisible(page, selector) {
  return visible(page, selector);
}

/** The CSRF token of the current accounts session (for fetch-based API probes). */
export async function csrfToken(page) {
  return page.evaluate(async () => {
    const response = await fetch('/api/me', { headers: { Accept: 'application/json' } });
    if (!response.ok) return null;
    const payload = await response.json();
    return payload.csrfToken ?? null;
  });
}
