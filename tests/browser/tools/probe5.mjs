/**
 * Probe: composer focus style after the shell dimension's exact sequence
 * (breakpoint sweep → reload → Tab). Decides whether the managed-input focus
 * assertion is stable.
 *
 * Usage: node tests/browser/tools/probe5.mjs
 */

import { chromium } from 'playwright';
import { mkdtempSync, mkdirSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { startHost } from '../support/host.mjs';

const REPO_ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..', '..');
const outDir = mkdtempSync(join(tmpdir(), 'glink-harness-probe5-'));
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
await page.waitForTimeout(600);

// replicate the breakpoint sweep
for (const width of [1512, 1250, 1050, 1049, 900, 721, 720, 600]) {
  await page.setViewportSize({ width, height: 900 });
  await page.waitForTimeout(320);
}
await page.setViewportSize({ width: 1512, height: 900 });
await page.waitForTimeout(300);
await page.click('#sidebar-toggle');
await page.waitForTimeout(350);
await page.reload({ waitUntil: 'domcontentloaded' });
await page.waitForTimeout(700);

const rows = [];
for (let index = 0; index < 8; index += 1) {
  await page.keyboard.press('Tab');
  await page.waitForTimeout(140);
  rows.push(
    await page.evaluate(() => {
      const node = document.activeElement;
      const composer = document.querySelector('.prompt-input');
      const styles = composer ? getComputedStyle(composer) : null;
      const all = [...document.querySelectorAll('.prompt-input')];
      return {
        active: node.id || node.className || node.tagName,
        count: all.length,
        index: all.indexOf(composer),
        path: node.className || node.tagName,
        outline: styles ? `${styles.outlineStyle} ${styles.outlineWidth} ${styles.outlineColor}` : null,
        border: styles?.borderTopColor ?? null,
        focusWithin: composer ? composer.matches(':focus-within') : false,
        hovered: composer ? composer.matches(':hover') : false,
        rect: composer ? Math.round(composer.getBoundingClientRect().height) : null,
      };
    }),
  );
}
writeFileSync(join(outDir, 'probe5.json'), `${JSON.stringify(rows, null, 2)}\n`, 'utf8');
console.log(JSON.stringify(rows, null, 1));
await browser.close();
console.log('host exit', JSON.stringify(await host.stop()));
