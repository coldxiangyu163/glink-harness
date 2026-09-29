// PLAYWRIGHT_PATH=... node ui-empty-targeted.mjs <base-url> <out-dir>
// Companion diagnostic for the unmodified legacy ui-features route '**/api/runs' missing '?limit=50'.
import { createRequire } from 'node:module';
import { writeFileSync } from 'node:fs';
import { join } from 'node:path';
const { chromium } = createRequire(import.meta.url)(process.env.PLAYWRIGHT_PATH);
const [base, out] = process.argv.slice(2);
const browser = await chromium.launch();
try {
  const page = await browser.newPage({ viewport: { width: 1512, height: 900 } });
  const intercepted = [];
  await page.route('**/api/runs?limit=*', route => {
    intercepted.push(route.request().url());
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ runs: [], hasMore: false, nextCursor: null }) });
  });
  await page.goto(base);
  await page.waitForSelector('#run-list .empty-state');
  const empty = await page.evaluate(() => {
    const node = document.querySelector('#run-list .empty-state');
    return { role: node?.getAttribute('role'), variant: node?.dataset.variant,
      image: node?.querySelector('img')?.getAttribute('src'),
      title: node?.querySelector('.empty-state-title')?.textContent,
      homeEmpty: document.querySelectorAll('#home-run-list .empty-state, #home-report-list .empty-state').length,
      rows: document.querySelectorAll('#run-list [data-run-id]').length };
  });
  await page.screenshot({ path: join(out, 'ui-empty-targeted.png') });
  const ok = intercepted.length > 0 && empty.rows === 0 && empty.role === 'status' && empty.variant === 'no-data' && /no-data\.svg$/.test(empty.image || '') && empty.title === '还没有研究记录' && empty.homeEmpty === 2;
  writeFileSync(join(out, 'ui-empty-targeted.json'), JSON.stringify({ intercepted, empty, ok }, null, 2) + '\n');
  console.log(JSON.stringify({ intercepted, empty, ok }));
  if (!ok) process.exitCode = 1;
} finally { await browser.close(); }
