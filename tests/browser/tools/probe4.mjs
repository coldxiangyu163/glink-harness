/**
 * Probe: what does keyboard focus look like on the composer and on the
 * report/progress surfaces? (Decides whether the "managed input" ring
 * assertion is fair.)
 *
 * Usage: node tests/browser/tools/probe4.mjs
 */

import { chromium } from 'playwright';
import { mkdtempSync, mkdirSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { startHost } from '../support/host.mjs';

const REPO_ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..', '..');
const outDir = mkdtempSync(join(tmpdir(), 'pi-glink-probe4-'));
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
await page.waitForTimeout(500);

const dump = {};
await page.reload({ waitUntil: 'domcontentloaded' });
await page.waitForTimeout(800);

const focusState = () =>
  page.evaluate(() => {
    const node = document.activeElement;
    const composer = document.querySelector('.prompt-input');
    const composerStyle = composer ? getComputedStyle(composer) : null;
    const send = document.querySelector('#send-button');
    const sendStyle = send ? getComputedStyle(send) : null;
    return {
      active: { id: node.id, tag: node.tagName, managed: node.hasAttribute?.('data-focus-ring-managed') ?? false },
      composer: composerStyle
        ? {
            outline: `${composerStyle.outlineStyle} ${composerStyle.outlineWidth} ${composerStyle.outlineColor}`,
            border: composerStyle.borderTopColor,
            borderWidth: composerStyle.borderTopWidth,
            focusWithin: composer.matches(':focus-within'),
            boxShadow: composerStyle.boxShadow,
          }
        : null,
      senderOutline: sendStyle ? `${sendStyle.outlineStyle} ${sendStyle.outlineWidth} ${sendStyle.outlineColor}` : null,
    };
  });

const sequence = [];
for (let i = 0; i < 8; i += 1) {
  await page.keyboard.press('Tab');
  await page.waitForTimeout(120);
  sequence.push(await focusState());
  if (sequence.at(-1).active.id === 'prompt-input') {
    await page.screenshot({ path: join(outDir, 'composer-focus.png') });
  }
}
dump.tabSequence = sequence;
writeFileSync(join(outDir, 'probe4.json'), `${JSON.stringify(dump, null, 2)}\n`, 'utf8');
console.log(JSON.stringify(dump, null, 2));
await browser.close();
console.log('host exit', JSON.stringify(await host.stop()));
