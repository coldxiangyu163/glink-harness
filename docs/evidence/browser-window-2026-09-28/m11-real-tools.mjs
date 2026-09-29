// PLAYWRIGHT_PATH=... node m11-real-tools.mjs <base-url> <run-id> <out-dir>
import { createRequire } from 'node:module';
import { writeFileSync } from 'node:fs';
import { join } from 'node:path';
const { chromium } = createRequire(import.meta.url)(process.env.PLAYWRIGHT_PATH);
const [base, id, out] = process.argv.slice(2);
const browser = await chromium.launch();
try {
  const page = await browser.newPage({ viewport: { width: 1512, height: 900 } });
  await page.goto(base);
  await page.waitForSelector(`#run-list [data-run-id="${id}"]`);
  await page.click(`#run-list [data-run-id="${id}"]`);
  await page.waitForSelector('#run-view[data-status="finished"] .pi-tool-name', { state: 'attached', timeout: 30000 });
  const toggle = page.locator('#run-view .pi-process-toggle').first();
  if (await toggle.getAttribute('aria-expanded') === 'false') await toggle.click();
  const result = await page.evaluate(() => ({
    status: document.querySelector('#run-view')?.dataset.status,
    tools: [...document.querySelectorAll('#run-view .pi-tool:not(.pi-subagent) .pi-tool-head')]
      .map(el => ({ action: el.querySelector('.pi-tool-name')?.textContent?.trim(), object: el.querySelector('.pi-tool-args')?.textContent?.trim()?.slice(0, 100) })),
    outputVisible: !!document.querySelector('#run-view [aria-label="生成的文件"]:not([hidden])'),
  }));
  await page.screenshot({ path: join(out, 'm11-real-tools.png'), fullPage: true });
  writeFileSync(join(out, 'm11-real-tools.json'), JSON.stringify(result, null, 2) + '\n');
  console.log(JSON.stringify({ status: result.status, count: result.tools.length, actions: [...new Set(result.tools.map(x => x.action))], outputVisible: result.outputVisible }));
  if (result.status !== 'finished' || !result.tools.some(x => x.action === '执行') || !result.outputVisible) process.exitCode = 1;
} finally { await browser.close(); }
