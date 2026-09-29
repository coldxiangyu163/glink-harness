// Run against the isolated five-turn REAL model chain: PLAYWRIGHT_PATH=... node m8-stream-count.mjs <base-url> <leaf-id> <out-dir>
import { createRequire } from 'node:module';
import { writeFileSync } from 'node:fs';
import { join } from 'node:path';
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_PATH);
const [base, leaf, out] = process.argv.slice(2);
const browser = await chromium.launch();
try {
  const page = await browser.newPage({ viewport: { width: 1512, height: 900 } });
  await page.addInitScript(() => {
    const Original = window.EventSource;
    const active = new Set();
    const timeline = [];
    let peak = 0;
    let next = 0;
    window.__streamAudit = { active, timeline, peak: () => peak };
    window.EventSource = class extends Original {
      constructor(url, options) {
        super(url, options);
        this.auditId = ++next;
        active.add(this.auditId);
        peak = Math.max(peak, active.size);
        timeline.push({ event: 'open', id: this.auditId, url: String(url), active: active.size, at: performance.now() });
      }
      close() {
        if (active.delete(this.auditId)) timeline.push({ event: 'close', id: this.auditId, active: active.size, at: performance.now() });
        super.close();
      }
    };
  });
  await page.goto(base);
  await page.waitForSelector(`#run-list [data-run-id="${leaf}"]`);
  await page.click(`#run-list [data-run-id="${leaf}"]`);
  await page.waitForFunction(() => document.querySelectorAll('#earlier-turns .pi-exchange').length === 4);
  await page.waitForFunction(() => window.__streamAudit.timeline.filter(x => x.event === 'close').length >= 5, null, { timeout: 120000 });
  const result = await page.evaluate(() => ({
    exchanges: document.querySelectorAll('#earlier-turns .pi-exchange').length,
    currentStatus: document.querySelector('#run-view')?.dataset.status,
    openCount: window.__streamAudit.timeline.filter(x => x.event === 'open').length,
    closeCount: window.__streamAudit.timeline.filter(x => x.event === 'close').length,
    peak: window.__streamAudit.peak(),
    active: window.__streamAudit.active.size,
    timeline: window.__streamAudit.timeline,
  }));
  await page.screenshot({ path: join(out, 'm8-history.png'), fullPage: true });
  writeFileSync(join(out, 'm8-stream-count.json'), JSON.stringify(result, null, 2) + '\n');
  console.log(JSON.stringify({ exchanges: result.exchanges, currentStatus: result.currentStatus, openCount: result.openCount, closeCount: result.closeCount, peak: result.peak, active: result.active }));
  if (result.exchanges !== 4 || result.openCount < 5 || result.peak > 2 || result.active !== 0) process.exitCode = 1;
} finally { await browser.close(); }
