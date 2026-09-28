/**
 * Browser regression for the rewritten run view.
 *
 * Drives a real run through the real UI: type a prompt, let pi execute, then
 * assert the three surfaces the task contract names — thinking folds, tool
 * cards with streamed output, assistant text — plus the terminal state and the
 * report preview. Takes a screenshot as evidence.
 *
 * Usage (host must be running):
 *   node tests/browser/run-smoke.mjs [--base-url http://127.0.0.1:7861]
 *
 * Playwright is resolved from the npx cache so the repository needs no
 * dependency for this check; set PLAYWRIGHT_PATH to override.
 */

import { createRequire } from 'node:module';
import { existsSync, readdirSync, writeFileSync, mkdirSync } from 'node:fs';
import { homedir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const REPO_ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const DEFAULT_PROMPT =
  '先在本工作区创建 notes.md，写入三行关于 pi-glink 的事实，然后基于它写出 report.md。';
const TERMINAL_TIMEOUT = 300_000;

function findPlaywright() {
  if (process.env.PLAYWRIGHT_PATH) return process.env.PLAYWRIGHT_PATH;
  const npxRoot = join(homedir(), '.npm', '_npx');
  if (!existsSync(npxRoot)) return null;
  const candidates = readdirSync(npxRoot)
    .map((entry) => join(npxRoot, entry, 'node_modules', 'playwright'))
    .filter((path) => existsSync(join(path, 'package.json')));
  return candidates.at(-1) ?? null;
}

function argValue(flag, fallback) {
  const index = process.argv.indexOf(flag);
  return index >= 0 && process.argv[index + 1] ? process.argv[index + 1] : fallback;
}

const playwrightPath = findPlaywright();
if (!playwrightPath) {
  console.error('[fail] 未找到 playwright；设置 PLAYWRIGHT_PATH 或先 npx playwright --version');
  process.exit(2);
}
const require = createRequire(import.meta.url);
const { chromium } = require(playwrightPath);

const baseUrl = argValue('--base-url', 'http://127.0.0.1:7861');
const prompt = argValue('--prompt', DEFAULT_PROMPT);

const evidenceDir = join(REPO_ROOT, 'docs', 'evidence');
mkdirSync(evidenceDir, { recursive: true });

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
const consoleErrors = [];
page.on('pageerror', (error) => consoleErrors.push(String(error)));
page.on('console', (message) => {
  if (message.type() === 'error') consoleErrors.push(message.text());
});

let exitCode = 1;
try {
  await page.goto(baseUrl, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('#app-shell:not([hidden])', { timeout: 15_000 });
  const serviceAlert = await page.evaluate(() => (document.querySelector('#sidebar-foot')?.hidden ? '' : document.querySelector('#service-alert-text')?.textContent));
  if (serviceAlert) throw new Error(`服务告警：${serviceAlert}`);
  console.log('[ok] 工作台已加载（无服务告警）');

  await page.fill('#prompt-input', prompt);
  await page.click('#send-button');
  console.log('[ok] 已提交 run，等待执行…');

  await page.waitForSelector('#run-view[data-status="finished"]', { timeout: TERMINAL_TIMEOUT });
  await page.waitForTimeout(400);
  const status = (await page.textContent('.pi-process-label'))?.trim();
  console.log(`[ok] run 收尾：${status}`);
  // 过程折叠在收尾后自动收起；展开它再统计（标准库 Thinking：跑完收起、永远可再展开）。
  if ((await page.getAttribute('.pi-process-toggle', 'aria-expanded')) !== 'true') {
    await page.click('.pi-process-toggle');
    await page.waitForTimeout(600);
  }

  const counts = await page.evaluate(() => ({
    thinking: document.querySelectorAll('.pi-thinking').length,
    thinkingOpen: document.querySelectorAll('.pi-thinking[open]').length,
    tools: document.querySelectorAll('.pi-tool').length,
    toolsDone: document.querySelectorAll('.pi-tool[data-status="done"]').length,
    toolsWithText: [...document.querySelectorAll('.pi-tool')].filter((node) => {
      const output = node.querySelector('.pi-tool-output')?.textContent.trim() ?? '';
      const result = node.querySelector('.pi-tool-result')?.textContent.trim() ?? '';
      return output !== '' || result !== '';
    }).length,
    toolsRenderingTextTwice: [...document.querySelectorAll('.pi-tool')].filter((node) => {
      // After the terminal frame the streamed preview is dropped when it repeats
      // the result; a card must never show the same text in both fields.
      const output = node.querySelector('.pi-tool-output')?.textContent.trim() ?? '';
      const result = node.querySelector('.pi-tool-result')?.textContent.trim() ?? '';
      return output !== '' && output === result;
    }).length,
    texts: document.querySelectorAll('.pi-text').length,
    textChars: [...document.querySelectorAll('.pi-text')].reduce((total, node) => total + node.textContent.length, 0),
    turns: document.querySelectorAll('.pi-turn').length,
    stages: document.querySelectorAll('.pi-stage').length,
    answer: document.querySelectorAll('.pi-answer-body .pi-text').length,
    draftBadge: !document.querySelector('.pi-answer-head')?.hidden,
    reportButton: !document.querySelector('[aria-label="生成的文件"]')?.hidden,
    stopHidden: document.querySelector('#stop-run')?.hidden,
    runListEntries: document.querySelectorAll('#run-list [data-run-id]').length,
  }));
  console.log('[ok] DOM 统计', JSON.stringify(counts));

  // Thinking folds must be expandable and carry text.
  if (counts.thinking > 0) {
    await page.click('.pi-thinking summary');
    const thinkingText = (await page.textContent('.pi-thinking-body'))?.trim() ?? '';
    console.log(`[ok] 思考折叠可展开，${thinkingText.length} 字`);
  }

  if (!counts.reportButton) throw new Error('报告按钮不可见');
  await page.click('.pi-report-button');
  await page.waitForSelector('#preview-article h1', { timeout: 15_000 });
  const previewTitle = (await page.textContent('#preview-article h1'))?.trim();
  console.log(`[ok] 报告预览已打开：${previewTitle}`);

  // 预览抽屉必须真的占据第三列（缺失 preview-open 时列宽为 0 并横向溢出）。
  const drawer = await page.evaluate(() => {
    const shell = document.querySelector('#app-shell');
    const preview = document.querySelector('#preview-drawer');
    return {
      previewOpen: shell.classList.contains('preview-open'),
      previewWidth: Math.round(preview.getBoundingClientRect().width),
      scrollWidth: document.documentElement.scrollWidth,
      clientWidth: document.documentElement.clientWidth,
    };
  });
  console.log('[ok] 抽屉布局', JSON.stringify(drawer));
  if (!drawer.previewOpen) throw new Error('preview-open 未设置：第三列宽度会为 0');
  if (drawer.previewWidth < 300) throw new Error(`预览抽屉宽度异常: ${drawer.previewWidth}px`);
  if (drawer.scrollWidth > drawer.clientWidth + 1) {
    throw new Error(`页面横向溢出: ${drawer.scrollWidth} > ${drawer.clientWidth}`);
  }
  await page.click('#preview-close');
  await page.waitForFunction(() => document.querySelector('#app-shell')?.classList.contains('preview-open') === false, { timeout: 5000 });

  // 历史 run 重放：打开一个已结束的 run，时间线必须完整回放（曾在首帧后误关 SSE）。
  const historyIds = await page.evaluate(() => [...document.querySelectorAll('#run-list [data-run-id]')].map((n) => n.dataset.runId));
  if (!historyIds.length) throw new Error('没有历史 run 可验证重放');
  await page.click('#run-list [data-run-id]');
  await page.waitForSelector('#run-view[data-status="finished"]', { timeout: 30_000 });
  await page.waitForTimeout(1500); // 等重放把剩余帧画完
  const replay = await page.evaluate(() => ({
    items: document.querySelectorAll('.pi-process-list > *, .pi-answer-body > *').length,
    tools: document.querySelectorAll('.pi-tool').length,
    texts: document.querySelectorAll('.pi-text').length,
    status: document.querySelector('#run-view')?.dataset.status,
    unavailable: document.querySelector('.pi-outcome')?.hidden === false && /读不到/.test(document.querySelector('.pi-outcome')?.textContent ?? ''),
  }));
  console.log('[ok] 历史 run 重放', JSON.stringify(replay));
  if (replay.items < 2) throw new Error(`历史重放时间线不完整: ${replay.items} 项`);
  if (replay.status !== 'finished') throw new Error(`历史 run 状态异常: ${replay.status}`);

  const screenshot = join(evidenceDir, 'browser-run.png');
  await page.screenshot({ path: screenshot, fullPage: true });
  console.log(`[ok] 截图：${screenshot}`);

  const summaryPath = join(evidenceDir, 'browser-run.json');
  writeFileSync(
    summaryPath,
    `${JSON.stringify({ baseUrl, prompt, status, counts, drawer, replay, previewTitle, consoleErrors }, null, 2)}\n`,
    'utf8',
  );
  console.log(`[ok] 证据：${summaryPath}`);

  const ok =
    counts.tools > 0 &&
    counts.toolsWithText > 0 &&
    counts.toolsRenderingTextTwice === 0 &&
    counts.texts > 0 &&
    counts.reportButton &&
    counts.answer === 1 &&
    counts.draftBadge === true &&
    counts.stopHidden === true &&
    !replay.unavailable &&
    drawer.previewOpen &&
    drawer.previewWidth >= 300 &&
    drawer.scrollWidth <= drawer.clientWidth + 1 &&
    replay.items >= 2 &&
    consoleErrors.length === 0;
  if (!ok) throw new Error(`断言未通过：${JSON.stringify(counts)} consoleErrors=${consoleErrors.length}`);
  console.log('[done] 浏览器回归通过');
  exitCode = 0;
} catch (error) {
  console.error(`[fail] ${error.message}`);
  try {
    await page.screenshot({ path: join(evidenceDir, 'browser-run-failure.png'), fullPage: true });
  } catch {
    /* screenshot is best effort */
  }
} finally {
  await browser.close();
  if (consoleErrors.length) console.error('[warn] 控制台错误:', consoleErrors.slice(0, 5));
}

process.exit(exitCode);
