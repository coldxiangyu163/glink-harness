// PLAYWRIGHT_PATH=... node m9-pages.mjs <base-url> <m9-injected.json> <out-dir>
import { createRequire } from 'node:module';
import { readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
const { chromium } = createRequire(import.meta.url)(process.env.PLAYWRIGHT_PATH);
const [base, seedFile, out] = process.argv.slice(2);
const seed = JSON.parse(readFileSync(seedFile, 'utf8'));
const browser = await chromium.launch();
try {
  const page = await browser.newPage({ viewport: { width: 1512, height: 900 } });
  const pages = [];
  page.on('response', async response => {
    if (!/^\/api\/runs\?limit=/.test(new URL(response.url()).pathname + new URL(response.url()).search)) return;
    if (response.status() !== 200) return;
    const body = await response.json();
    pages.push({ url: response.url(), count: body.runs?.length, ids: body.runs?.map(r => r.runId), hasMore: body.hasMore });
  });
  await page.goto(base);
  await page.waitForSelector('#run-list-more');
  const first = await page.locator('#run-list [data-run-id]').count();
  await page.screenshot({ path: join(out, 'm9-first-page.png') });
  let clicks = 0;
  while (await page.locator('#run-list-more').count()) {
    const before = pages.length;
    await page.click('#run-list-more');
    clicks++;
    await page.waitForFunction(n => document.querySelectorAll('#run-list [data-run-id]').length > n, first + (clicks - 1) * 40);
    await page.waitForFunction(n => !document.querySelector('#run-list-more')?.disabled && window.__noop !== n, -1).catch(() => {});
    if (pages.length === before) throw new Error('next page not requested');
    if (clicks > 4) throw new Error('too many pages');
  }
  const rows = await page.locator('#run-list [data-run-id]').evaluateAll(nodes => nodes.map(el => ({ id: el.dataset.runId, title: el.querySelector('.nav-title')?.textContent })));
  await page.screenshot({ path: join(out, 'm9-all-pages.png'), fullPage: true });
  await page.click(`#run-list [data-run-id="${seed.chain.at(-1)}"]`);
  await page.waitForFunction(() => document.querySelectorAll('#earlier-turns .pi-exchange').length === 3);
  const historyCount = await page.locator('#earlier-turns .pi-exchange').count();
  await page.screenshot({ path: join(out, 'm9-chain.png') });
  const allIds = [...new Set(pages.flatMap(x => x.ids))];
  const intermediate = seed.chain.slice(0, -1).filter(id => rows.some(row => row.id === id));
  const result = { clicks, pages, fetchedUnique: allIds.length, firstVisibleRows: first, visibleTopLevel: rows.length,
    leafVisible: rows.some(row => row.id === seed.chain.at(-1)), intermediateRows: intermediate,
    historyCount, oldestStandaloneVisible: rows.some(row => row.id === '000000000002') };
  writeFileSync(join(out, 'm9-pages.json'), JSON.stringify(result, null, 2) + '\n');
  console.log(JSON.stringify({ clicks, fetchedUnique: result.fetchedUnique, pageSizes: pages.map(p => p.count), visibleTopLevel: rows.length, historyCount, intermediateRows: intermediate, oldestStandaloneVisible: result.oldestStandaloneVisible }));
  if (allIds.length !== 110 || rows.length !== 107 || intermediate.length || historyCount !== 3 || !result.oldestStandaloneVisible || !result.leafVisible) process.exitCode = 1;
} finally { await browser.close(); }
