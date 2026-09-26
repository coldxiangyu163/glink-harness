/**
 * Probe: why is the file card (`.pi-output`) hidden when a finished run is
 * replayed from history? Dumps the answer region's state right after a live
 * finish and right after a history replay.
 *
 * Usage: node tests/browser/tools/probe3.mjs
 */

import { chromium } from 'playwright';
import { mkdtempSync, mkdirSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { startHost } from '../support/host.mjs';

const REPO_ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..', '..');
const outDir = mkdtempSync(join(tmpdir(), 'pi-glink-probe3-'));
const stateDir = join(outDir, 'state');
mkdirSync(stateDir, { recursive: true });
const host = await startHost({ repoRoot: REPO_ROOT, mode: 'accounts', stateDir, logFile: join(outDir, 'fixture.log') });
const browser = await chromium.launch();
const context = await browser.newContext({ viewport: { width: 1512, height: 900 } });
const page = await context.newPage();
await page.goto(host.baseUrl, { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(500);
await page.fill('#login-email', host.users.alphaAdmin.email);
await page.fill('#login-password', host.password);
await page.click('#login-submit');
await page.waitForSelector('#app-shell:not([hidden])', { timeout: 15_000 });
await page.waitForTimeout(400);

const dumpAnswer = () =>
  page.evaluate(() => {
    const outputs = [...document.querySelectorAll('#run-view .pi-output')].map((node) => ({
      cls: node.className,
      hidden: node.hidden,
      display: getComputedStyle(node).display,
      text: node.innerText.replace(/\n+/g, ' | ').slice(0, 120),
      label: node.getAttribute('aria-label'),
    }));
    return {
      status: document.querySelector('#run-view')?.dataset.status,
      answerHead: {
        exists: Boolean(document.querySelector('#run-view .pi-answer-head')),
        hidden: document.querySelector('#run-view .pi-answer-head')?.hidden,
      },
      actions: {
        exists: Boolean(document.querySelector('#run-view .pi-answer-actions')),
        hidden: document.querySelector('#run-view .pi-answer-actions')?.hidden,
        labels: [...document.querySelectorAll('#run-view .pi-answer-actions > *')].map((node) => node.getAttribute('aria-label')),
      },
      outputs,
      answerText: document.querySelector('#run-view .pi-answer-body')?.innerText?.slice(0, 60),
      reportButtonCount: document.querySelectorAll('#run-view .pi-report-button').length,
      viewText: document.querySelector('#run-view')?.innerText?.replace(/\n+/g, ' | ').slice(0, 300),
    };
  });

await page.fill('#prompt-input', '[scenario:default] 探测文件卡');
await page.press('#prompt-input', 'Enter');
await page.waitForSelector('#run-view[data-status="finished"]', { timeout: 30_000 });
await page.waitForTimeout(1000);
const dump = { live: await dumpAnswer() };

await page.click('#new-research');
await page.waitForTimeout(400);
const runId = await page.evaluate(async () => {
  const response = await fetch('/api/runs', { headers: { Accept: 'application/json' } });
  const payload = await response.json();
  return (payload.runs ?? []).find((run) => String(run.prompt ?? '').includes('探测文件卡'))?.runId ?? null;
});
await page.click(`#run-list [data-run-id="${runId}"]`);
await page.waitForTimeout(1500);
dump.replay = await dumpAnswer();
dump.runId = runId;
dump.detail = await page.evaluate(async (id) => {
  const response = await fetch(`/api/runs/${id}`, { headers: { Accept: 'application/json' } });
  return response.json();
}, runId);
dump.eventsTail = await page.evaluate(async (id) => {
  const response = await fetch(`/api/runs/${id}/events`, { headers: { Accept: 'text/event-stream' } });
  const text = await response.text();
  return text.split('\n').filter((line) => line.startsWith('data:')).slice(-2);
}, runId);

writeFileSync(join(outDir, 'probe3.json'), `${JSON.stringify(dump, null, 2)}\n`, 'utf8');
console.log(JSON.stringify(dump, null, 2));
await browser.close();
console.log('host exit', JSON.stringify(await host.stop()));
