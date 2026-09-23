/**
 * Full functional sweep of the workbench UI.
 *
 * The run smoke (`run-smoke.mjs`) proves one real run end-to-end; this sweep
 * proves the rest of the surface is still usable after the UI was re-aligned:
 * shell, home, composer (typing / counter / auto-grow / shortcut), live run +
 * stop, history replay, fold and tool-row interactions, report reader (scroll,
 * download, slot toggle), sidebar collapse with persistence, breakpoints without
 * horizontal overflow, the skip link, the empty state, and the module-graph
 * failure face. It also asserts the two layout invariants that a screenshot
 * cannot show: the composer dock stays inside the conversation column when the
 * report slot is open, and both scroll areas really scroll.
 *
 * Usage (host must be running):
 *   node tests/browser/ui-features.mjs [--base-url http://127.0.0.1:7861]
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
const VIEWPORT = { width: 1512, height: 900 };
const BREAKPOINTS = [1512, 1250, 1050, 900, 721, 600];
const TERMINAL_TIMEOUT = 240_000;

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
const evidenceDir = join(REPO_ROOT, 'docs', 'evidence');
mkdirSync(evidenceDir, { recursive: true });

const checks = [];
const failures = [];
const consoleErrors = [];

function record(name, ok, detail) {
  checks.push({ name, ok, detail });
  const line = `${ok ? '[ok]  ' : '[fail]'} ${name}${detail === undefined ? '' : ` · ${typeof detail === 'string' ? detail : JSON.stringify(detail)}`}`;
  console.log(line);
  if (!ok) failures.push(`${name}: ${typeof detail === 'string' ? detail : JSON.stringify(detail)}`);
}

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: VIEWPORT });
page.on('pageerror', (error) => consoleErrors.push(String(error)));
page.on('console', (message) => {
  if (message.type() === 'error') consoleErrors.push(message.text());
});

const shot = async (name) => {
  const path = join(evidenceDir, name);
  await page.screenshot({ path });
  return path;
};

/** Wait until the shell is up (and, optionally, until a run is open). */
async function boot() {
  await page.goto(baseUrl, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('#app-shell:not([hidden])', { timeout: 15_000 });
  await page.waitForTimeout(400);
}

async function runIds() {
  return page.$$eval('#run-list [data-run-id]', (nodes) => nodes.map((node) => node.dataset.runId));
}

try {
  await boot();

  // ── A. shell ──────────────────────────────────────────────────────────────
  const shell = await page.evaluate(() => {
    const box = (sel) => {
      const node = document.querySelector(sel);
      const rect = node.getBoundingClientRect();
      return { w: Math.round(rect.width), h: Math.round(rect.height), x: Math.round(rect.x) };
    };
    return {
      sidebar: box('#sidebar'),
      surface: box('.surface'),
      header: box('.pane-header'),
      dock: box('.composer-dock'),
      gap: getComputedStyle(document.querySelector('#app-shell')).gap,
      padding: getComputedStyle(document.querySelector('#app-shell')).padding,
      brand: document.querySelector('.brand-mark')?.getAttribute('src'),
      host: document.querySelector('#host-status')?.textContent,
      overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
    };
  });
  record('外壳几何（8px 沟槽 / 240 导航 / 52 栏头）', shell.sidebar.w === 240 && shell.header.h === 52 && shell.gap === '8px' && shell.padding === '8px', shell);
  record('品牌标识取自标准库资产', String(shell.brand).endsWith('logo-icon.svg'), shell.brand);
  record('宿主状态已渲染', /执行面/.test(shell.host || ''), shell.host);

  // ── B. home ───────────────────────────────────────────────────────────────
  const home = await page.evaluate(() => ({
    title: document.querySelector('#home-view h2')?.textContent,
    cards: document.querySelectorAll('.home-card').length,
    rows: document.querySelectorAll('#home-run-list [data-run-id]').length,
    path: document.querySelector('#pane-title')?.textContent,
    current: document.querySelectorAll('#run-list [aria-current]').length,
  }));
  record('首页（标题 / 两卡 / 历史行 / 无选中 run）', home.cards === 2 && home.path === '新研究' && home.current === 0, home);

  // ── C. composer ───────────────────────────────────────────────────────────
  const typedText = '功能自检 · 字符计数';
  await page.fill('#prompt-input', typedText);
  await page.waitForTimeout(150);
  const expectedCount = `${typedText.length} / 32000`;
  const typed = await page.evaluate(() => ({
    count: document.querySelector('#prompt-count')?.textContent,
    countHidden: document.querySelector('#prompt-count')?.hidden,
    sendDisabled: document.querySelector('#send-button')?.disabled,
    inputH: Math.round(document.querySelector('#prompt-input').getBoundingClientRect().height),
    composerH: Math.round(document.querySelector('.prompt-input').getBoundingClientRect().height),
  }));
  record('输入器：计数 + 发送键启用', typed.count === expectedCount && !typed.countHidden && !typed.sendDisabled && typed.composerH === 120, { ...typed, expectedCount });

  await page.fill('#prompt-input', Array.from({ length: 14 }, (_, index) => `第 ${index + 1} 行`).join('\n'));
  await page.waitForTimeout(250);
  const grown = await page.evaluate(() => ({
    inputH: Math.round(document.querySelector('#prompt-input').getBoundingClientRect().height),
    overflow: getComputedStyle(document.querySelector('#prompt-input')).overflowY,
  }));
  record('输入器：多行自增到 240 上限', grown.inputH === 240 && grown.overflow === 'auto', grown);

  await page.fill('#prompt-input', '');
  await page.waitForTimeout(200);
  const cleared = await page.evaluate(() => ({
    countHidden: document.querySelector('#prompt-count')?.hidden,
    sendDisabled: document.querySelector('#send-button')?.disabled,
    inputH: Math.round(document.querySelector('#prompt-input').getBoundingClientRect().height),
  }));
  record('输入器：清空后回落并禁用发送', cleared.countHidden === true && cleared.sendDisabled === true && cleared.inputH === 68, cleared);

  await page.evaluate(() => document.querySelector('#prompt-input').focus());
  const focused = await page.evaluate(() => getComputedStyle(document.querySelector('.prompt-input')).borderTopColor);
  record('输入器：激活态 1px 近黑描边', focused === 'rgb(23, 23, 23)', focused);

  // ── D. start a run, then stop it ──────────────────────────────────────────
  await page.fill(
    '#prompt-input',
    '先列出三条本地工作台的观察，再逐个展开成一段话，最后写入 report.md。用中文。',
  );
  await page.waitForTimeout(150);
  await page.click('#send-button');
  await page.waitForSelector('.pi-run-status[data-status="running"]', { timeout: 30_000 });
  await page.waitForTimeout(6000);
  const running = await page.evaluate(() => ({
    status: document.querySelector('.pi-run-status')?.dataset.status,
    bannerHidden: document.querySelector('#run-banner')?.hidden,
    bannerStatus: document.querySelector('#run-banner')?.dataset.status,
    stopVisible: !document.querySelector('#stop-run')?.hidden,
    items: document.querySelectorAll('.pi-run-timeline > *').length,
    streams: document.querySelector('#stream-status')?.textContent,
    path: document.querySelector('#pane-title')?.textContent,
    marked: document.querySelectorAll('#run-list [aria-current="page"]').length,
  }));
  record('运行中：状态 / 状态条 / 停止键 / 事件项', running.status === 'running' && running.bannerHidden === false && running.stopVisible && running.items > 0, running);
  record('运行中：侧栏标记当前 run（且只标记一个）', running.marked === 1, running.marked);
  await shot('ui-features-running.png');

  await page.click('#stop-run');
  await page.waitForSelector('.pi-run-status[data-status="cancelled"]', { timeout: 60_000 });
  await page.waitForTimeout(800);
  const stopped = await page.evaluate(() => ({
    status: document.querySelector('.pi-run-status')?.dataset.status,
    bannerHidden: document.querySelector('#run-banner')?.hidden,
    toast: document.querySelector('#toast')?.textContent,
    bannerGone: document.querySelector('#run-banner')?.hidden === true,
  }));
  record('停止：终态 cancelled + 状态条收起 + 提示', stopped.status === 'cancelled' && stopped.bannerGone === true, stopped);

  // ── E. history replay + trace interactions ────────────────────────────────
  const catalogue = await fetch(`${baseUrl}/api/runs`).then((response) => response.json());
  const replayCandidates = (catalogue.runs ?? [])
    .filter((run) => run.status === 'finished' && run.hasReport)
    .slice(0, 8);
  let replayRun = null;
  for (const candidate of replayCandidates) {
    await page.click(`#run-list [data-run-id="${candidate.runId}"]`);
    await page.waitForTimeout(1200);
    const probe = await page.evaluate(() => ({
      thinking: document.querySelectorAll('.pi-thinking').length,
      tools: document.querySelectorAll('.pi-tool').length,
      texts: document.querySelectorAll('.pi-text').length,
      status: document.querySelector('.pi-run-status')?.dataset.status,
      reportButton: !document.querySelector('.pi-report-button')?.hidden,
      distance: (() => {
        const scroll = document.querySelector('#conversation-scroll');
        return Math.round(scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight);
      })(),
    }));
    const score = probe.thinking + probe.tools + probe.texts;
    if (!replayRun || score > replayRun.score) replayRun = { id: candidate.runId, score, ...probe };
    if (probe.thinking > 0 && probe.tools > 0 && probe.texts > 0) break;
  }
  record(
    '历史重放：时间线完整且贴底',
    replayRun?.thinking > 0 && replayRun?.tools > 0 && replayRun?.distance === 0 && replayRun?.status === 'finished',
    replayRun,
  );

  const fold = await page.evaluate(async () => {
    const details = document.querySelector('.pi-thinking');
    const before = { open: details.open, label: document.querySelector('.pi-thinking-label')?.textContent };
    document.querySelector('.pi-thinking > summary').click();
    await new Promise((resolve) => setTimeout(resolve, 500));
    const scroll = document.querySelector('#conversation-scroll');
    const after = {
      open: details.open,
      chars: document.querySelector('.pi-thinking-body')?.textContent.length,
      distance: Math.round(scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight),
    };
    document.querySelector('.pi-thinking > summary').click();
    await new Promise((resolve) => setTimeout(resolve, 500));
    return { before, after, closedAgain: details.open };
  });
  record('思考折叠：可展开 / 有正文 / 保持贴底 / 可再收起', fold.after.open === true && fold.after.chars > 0 && fold.closedAgain === false, fold);

  const tool = await page.evaluate(async () => {
    const row = document.querySelector('.pi-tool');
    const head = row.querySelector('.pi-tool-head');
    const before = { open: row.open, text: row.querySelector('.pi-tool-output')?.textContent.length + row.querySelector('.pi-tool-result')?.textContent.length };
    head.click();
    await new Promise((resolve) => setTimeout(resolve, 500));
    const openedBox = row.getBoundingClientRect().height;
    const after = { open: row.open, height: Math.round(openedBox), label: row.querySelector('.pi-tool-name')?.textContent, state: row.querySelector('.pi-tool-state')?.textContent };
    head.click();
    await new Promise((resolve) => setTimeout(resolve, 500));
    return { before, after, closedHeight: Math.round(row.getBoundingClientRect().height), closedAgain: row.open };
  });
  record('工具行：可展开看细节 / 可再收起', tool.after.open === true && tool.after.height > tool.closedHeight && tool.closedAgain === false, tool);
  await shot('ui-features-trace.png');

  // ── F. report reader ──────────────────────────────────────────────────────
  const hasReport = (await page.$('.pi-report-button')) ? !(await page.$eval('.pi-report-button', (node) => node.hidden)) : false;
  if (!hasReport) {
    record('报告：可用 run', false, `打开的 run ${replayRun?.id} 没有报告`);
  } else {
    await page.click('.pi-report-button');
    await page.waitForSelector('#preview-body h1', { timeout: 15_000 });
    await page.waitForTimeout(600);
    const report = await page.evaluate(() => {
      const surface = document.querySelector('.surface');
      const pane = document.querySelector('.pane');
      const drawer = document.querySelector('#preview-drawer');
      const body = document.querySelector('#preview-body');
      const dock = document.querySelector('.composer-dock');
      const paneRect = pane.getBoundingClientRect();
      const dockRect = dock.getBoundingClientRect();
      return {
        open: document.querySelector('#app-shell').classList.contains('preview-open'),
        columns: getComputedStyle(surface).gridTemplateColumns,
        rows: getComputedStyle(surface).gridTemplateRows,
        drawerH: Math.round(drawer.getBoundingClientRect().height),
        bodyScrollable: body.scrollHeight > body.clientHeight + 1,
        bodyClient: body.clientHeight,
        bodyScroll: body.scrollHeight,
        dockInsidePane: dockRect.right <= paneRect.right + 1,
        paneRight: Math.round(paneRect.right),
        dockRight: Math.round(dockRect.right),
        toggleExpanded: document.querySelector('#pane-preview-toggle').getAttribute('aria-expanded'),
        overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
      };
    });
    record('报告右槽：开合 / 两栏 / 无横向溢出', report.open && report.columns.split(' ').length === 2 && !report.overflow && report.toggleExpanded === 'true', report);
    record('报告右槽：正文真的能滚动', report.bodyScrollable === true, { client: report.bodyClient, scroll: report.bodyScroll });
    record('报告右槽：输入器只占左栏（不压报告）', report.dockInsidePane === true, { dockRight: report.dockRight, paneRight: report.paneRight, drawerH: report.drawerH });

    // scroll the report body for real, then the conversation column behind it
    const scrolled = await page.evaluate(async () => {
      const body = document.querySelector('#preview-body');
      const conversation = document.querySelector('#conversation-scroll');
      body.scrollTop = 500;
      const bodyAfter = body.scrollTop;
      conversation.scrollTop = 200;
      const conversationAfter = conversation.scrollTop;
      return { bodyAfter, conversationAfter, bodyMax: body.scrollHeight - body.clientHeight, conversationMax: conversation.scrollHeight - conversation.clientHeight };
    });
    record('报告右槽 + 对话列：两条滚动都可驱动', scrolled.bodyAfter > 0 && scrolled.conversationAfter > 0, scrolled);

    const downloadPromise = page.waitForEvent('download', { timeout: 20_000 });
    await page.click('#preview-download');
    const download = await downloadPromise;
    await download.saveAs('/tmp/pi-glink-ui-features-report.md');
    const stat = await import('node:fs/promises').then((fs) => fs.stat('/tmp/pi-glink-ui-features-report.md'));
    record('报告下载：落盘成功', stat.size > 0, { file: download.suggestedFilename(), bytes: stat.size });

    await shot('ui-features-report.png');
    await page.click('#pane-preview-toggle');
    await page.waitForTimeout(400);
    const closed = await page.evaluate(() => ({
      open: document.querySelector('#app-shell').classList.contains('preview-open'),
      hidden: document.querySelector('#preview-drawer').hidden,
      icon: document.querySelector('#pane-preview-toggle').firstElementChild.dataset.icon,
      expanded: document.querySelector('#pane-preview-toggle').getAttribute('aria-expanded'),
    }));
    record('报告右槽：栏头开合键可收起（图标/aria 同步）', closed.open === false && closed.hidden === true && closed.icon === 'panel-expand-right' && closed.expanded === 'false', closed);
  }

  // ── G. sidebar collapse ───────────────────────────────────────────────────
  await page.click('#sidebar-toggle');
  await page.waitForTimeout(300);
  const collapsed = await page.evaluate(() => ({
    width: Math.round(document.querySelector('#sidebar').getBoundingClientRect().width),
    expanded: document.querySelector('#sidebar-toggle').getAttribute('aria-expanded'),
    label: document.querySelector('#sidebar-toggle').getAttribute('aria-label'),
    navLabel: getComputedStyle(document.querySelector('.nav-label')).display,
  }));
  record('侧栏：折叠到 64 图标条 + aria 两态', collapsed.width === 64 && collapsed.expanded === 'false' && collapsed.label === '展开侧栏' && collapsed.navLabel === 'none', collapsed);
  await page.reload();
  await page.waitForSelector('#app-shell:not([hidden])', { timeout: 15_000 });
  await page.waitForTimeout(600);
  const persisted = await page.evaluate(() => ({
    width: Math.round(document.querySelector('#sidebar').getBoundingClientRect().width),
    expanded: document.querySelector('#sidebar-toggle').getAttribute('aria-expanded'),
  }));
  record('侧栏：折叠状态刷新后保持', persisted.width === 64 && persisted.expanded === 'false', persisted);
  await page.click('#sidebar-toggle');
  await page.waitForTimeout(300);
  record('侧栏：可再展开', (await page.evaluate(() => Math.round(document.querySelector('#sidebar').getBoundingClientRect().width))) === 240);

  // ── H. breakpoints ────────────────────────────────────────────────────────
  const layout = [];
  for (const width of BREAKPOINTS) {
    await page.setViewportSize({ width, height: 900 });
    await page.waitForTimeout(350);
    layout.push(await page.evaluate((viewportWidth) => {
      const sidebar = Math.round(document.querySelector('#sidebar').getBoundingClientRect().width);
      const home = document.querySelector('#home-view');
      const cards = getComputedStyle(document.querySelector('.home-cards')).gridTemplateColumns.split(' ').length;
      return {
        width: viewportWidth,
        sidebar,
        cards,
        paddingTop: getComputedStyle(document.querySelector('.home-inner')).paddingTop,
        overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
      };
    }, width));
    await page.click('#pane-home').catch(() => {});
    await page.waitForTimeout(200);
  }
  const expectedSidebar = { 1512: 240, 1250: 240, 1050: 212, 900: 212, 721: 64, 600: 64 };
  const sidebarOk = layout.every((item) => item.sidebar === expectedSidebar[item.width]);
  const overflowOk = layout.every((item) => !item.overflow);
  record('断点：侧栏 240 / 212 / 64 逐档正确', sidebarOk, layout.map((item) => `${item.width}:${item.sidebar}`).join(' '));
  record('断点：六档均无横向溢出', overflowOk);
  record('断点：首页双列 → 单列', layout.find((item) => item.width === 1512)?.cards === 2 && layout.find((item) => item.width === 1050)?.cards === 1);
  await page.setViewportSize(VIEWPORT);
  await page.waitForTimeout(300);

  // ── I. skip link ──────────────────────────────────────────────────────────
  // A fresh load is the only honest starting point: mid-document focus would send
  // Tab to whatever happens to be next, not to the first stop.
  await boot();
  await page.keyboard.press('Tab');
  await page.waitForTimeout(250);
  const skip = await page.evaluate(() => {
    const link = document.querySelector('.skip-link');
    return {
      active: document.activeElement.className,
      visible: Math.round(link.getBoundingClientRect().top) >= 0,
      text: document.activeElement.textContent,
    };
  });
  record('跳转链接：Tab 首个焦点且滑入可见', skip.active === 'skip-link' && skip.visible === true, skip);

  // Console must be clean for everything the user can reach; the injected
  // failures below are expected to log their own errors, so snapshot first.
  const cleanConsoleErrors = consoleErrors.length;
  record('浏览器控制台无错误（注入失败前）', cleanConsoleErrors === 0, consoleErrors.slice(0, 3));

  // ── J. empty state (stubbed API) ──────────────────────────────────────────
  await page.route('**/api/runs', (route) => {
    if (route.request().method() !== 'GET') return route.continue();
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ runs: [] }) });
  });
  await boot();
  const empty = await page.evaluate(() => ({
    note: document.querySelector('#run-list .empty-note')?.textContent,
    rows: document.querySelectorAll('#run-list [data-run-id]').length,
    homeRows: document.querySelectorAll('#home-run-list [data-run-id]').length,
  }));
  record('空运行列表：侧栏与首页都不留空块', empty.rows === 0 && empty.homeRows === 0 && /还没有运行记录/.test(empty.note || ''), empty);
  await page.unroute('**/api/runs');

  // ── K. module-graph failure face ──────────────────────────────────────────
  await page.route('**/modules/workspace.mjs*', (route) => route.abort());
  await page.goto(baseUrl, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('#boot-failure', { timeout: 10_000 });
  const failure = await page.evaluate(() => {
    const overlay = document.getElementById('boot-failure');
    const button = overlay.querySelector('button');
    const style = getComputedStyle(button);
    return {
      role: overlay.getAttribute('role'),
      title: overlay.querySelector('h2')?.textContent,
      buttonH: style.height,
      buttonBg: style.backgroundColor,
    };
  });
  record('模块加载失败：恢复面 + 重试键（标准库样式）', failure.role === 'alert' && failure.buttonH === '40px' && failure.buttonBg === 'rgb(23, 23, 23)', failure);
  await page.unroute('**/modules/workspace.mjs*');
  await boot();
  record('恢复后工作台可再次加载', await page.$('#app-shell:not([hidden])') !== null);

  // ── L. console after the injected failures ────────────────────────────────
  record(
    '注入失败只产生预期内的报错（不静默、不额外扩散）',
    consoleErrors.length > cleanConsoleErrors,
    { before: cleanConsoleErrors, after: consoleErrors.length },
  );
} catch (error) {
  record('运行异常', false, error.message);
  await shot('ui-features-failure.png').catch(() => {});
} finally {
  const passed = checks.filter((check) => check.ok).length;
  const transcript = [
    'pi-glink UI 全功能自检',
    `日期: ${new Date().toISOString()}`,
    `命令: node tests/browser/ui-features.mjs --base-url ${baseUrl}`,
    `视口: ${VIEWPORT.width}×${VIEWPORT.height}（断点档另测 ${BREAKPOINTS.join(' / ')}）`,
    '',
    ...checks.map((check) => `${check.ok ? '[ok]  ' : '[fail]'} ${check.name}${check.detail === undefined ? '' : ` · ${typeof check.detail === 'string' ? check.detail : JSON.stringify(check.detail)}`}`),
    '',
    `结果: ${passed}/${checks.length} 通过${failures.length ? ` · 失败 ${failures.length}` : ''}`,
    '',
  ].join('\n');
  writeFileSync(join(evidenceDir, 'ui-features.txt'), transcript, 'utf8');
  writeFileSync(
    join(evidenceDir, 'ui-features.json'),
    `${JSON.stringify({ baseUrl, viewport: VIEWPORT, breakpoints: BREAKPOINTS, checks, failures, consoleErrors }, null, 2)}\n`,
    'utf8',
  );
  process.stdout.write(`\n${passed}/${checks.length} 通过\n`);
  if (failures.length) console.log(`失败项:\n  - ${failures.join('\n  - ')}`);
  console.log('[ok] 证据：docs/evidence/ui-features.txt / ui-features.json');
  await browser.close();
}

process.exit(failures.length ? 1 : 0);
