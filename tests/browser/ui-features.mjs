/**
 * Full functional sweep of the workbench UI.
 *
 * The run smoke (`run-smoke.mjs`) proves one real run end-to-end; this sweep
 * proves the rest of the surface against the 百鉴 standard library's
 * interaction contract: shell and home, product copy (no engine / execution
 * surface / session leaks), composer (Enter / Shift+Enter / IME guard /
 * counter / auto-grow), stop-in-place, neutral stopped state, history replay
 * with the process fold, the draft answer frame, 进度与上下文, the report slot
 * (two columns, draggable line, collapse keys, unique names, focus return),
 * sidebar rail with tooltips, breakpoints (width < N like the library), visible
 * keyboard focus, EmptyState, the unreadable-history state and the
 * module-graph failure face.
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
const BREAKPOINTS = [1512, 1250, 1050, 1049, 900, 721, 720, 600];
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
      header: box('.pane-header'),
      gap: getComputedStyle(document.querySelector('#app-shell')).gap,
      padding: getComputedStyle(document.querySelector('#app-shell')).padding,
      brand: document.querySelector('.brand-mark')?.getAttribute('src'),
      alertHidden: document.querySelector('#sidebar-foot')?.hidden,
      font: getComputedStyle(document.body).fontFamily,
      title: document.title,
    };
  });
  record('外壳几何（8px 沟槽 / 240 导航 / 52 栏头）', shell.sidebar.w === 240 && shell.header.h === 52 && shell.gap === '8px' && shell.padding === '8px', shell);
  record('品牌标识取自标准库资产', String(shell.brand).endsWith('logo-icon.svg'), shell.brand);
  record('服务正常时侧栏页脚不出现状态牌', shell.alertHidden === true, shell.alertHidden);
  record('页面作用域字体与标准库 Demo 同族（Noto Sans SC 优先）', /^"?Noto Sans SC"?/.test(shell.font), shell.font);

  // ── B. home ───────────────────────────────────────────────────────────────
  const home = await page.evaluate(() => ({
    title: document.querySelector('#home-view .home-title')?.textContent,
    cards: document.querySelectorAll('.home-card').length,
    rows: document.querySelectorAll('#home-run-list [data-run-id]').length,
    reports: document.querySelectorAll('#home-report-list [data-report-run-id]').length,
    path: document.querySelector('#pane-title')?.textContent,
    pathColor: getComputedStyle(document.querySelector('#pane-title')).color,
    current: document.querySelectorAll('#run-list [aria-current]').length,
    headerActions: [...document.querySelectorAll('#pane-actions button')].filter((node) => !node.hidden).length,
  }));
  record('首页（标题 / 两卡 / 无选中 / 无右簇）', home.title === '今天要研究什么？' && home.cards === 2 && home.path === '新研究' && home.current === 0 && home.headerActions === 0, home);
  record('栏头对象段走 --foreground（PH-03）', home.pathColor === 'rgb(23, 23, 23)', home.pathColor);

  // 暴露面：界面自己的文字（不含用户写的研究问题）里不出现内核名、执行面、会话 id 这类运维事实
  const leaks = await page.evaluate(() => {
    const clone = document.body.cloneNode(true);
    for (const sel of ['#run-list', '#home-run-list', '#home-report-list']) clone.querySelector(sel)?.replaceChildren();
    const holder = document.createElement('div');
    holder.style.cssText = 'position:absolute;left:-99999px;top:0;width:1512px';
    holder.append(clone);
    document.body.append(holder);
    const text = clone.innerText;
    holder.remove();
    return ['pi-glink', '执行面', 'inproc', '内核', '宿主', 'session ', 'openai/'].filter((word) => text.includes(word))
      .concat(/(^|[^a-z])pi([^a-z-]|$)/i.test(text) ? ['pi'] : []);
  });
  record('首页可见文字不暴露内核 / 执行面 / 会话信息', leaks.length === 0, leaks);

  // ── C. composer ───────────────────────────────────────────────────────────
  await page.fill('#prompt-input', '功能自检 · 字符计数');
  await page.waitForTimeout(150);
  const typed = await page.evaluate(() => ({
    countHidden: document.querySelector('#prompt-count')?.hidden,
    sendDisabled: document.querySelector('#send-button')?.disabled,
    composerH: Math.round(document.querySelector('.prompt-input').getBoundingClientRect().height),
  }));
  record('输入器：普通长度不显示计数、发送键启用、120 高', typed.countHidden === true && !typed.sendDisabled && typed.composerH === 120, typed);

  await page.fill('#prompt-input', Array.from({ length: 14 }, (_, index) => `第 ${index + 1} 行`).join('\n'));
  await page.waitForTimeout(250);
  const grown = await page.evaluate(() => ({
    inputH: Math.round(document.querySelector('#prompt-input').getBoundingClientRect().height),
    overflow: getComputedStyle(document.querySelector('#prompt-input')).overflowY,
  }));
  record('输入器：多行自增到 240 上限', grown.inputH === 240 && grown.overflow === 'auto', grown);

  await page.fill('#prompt-input', 'x'.repeat(30010));
  await page.waitForTimeout(150);
  const nearLimit = await page.evaluate(() => ({ hidden: document.querySelector('#prompt-count')?.hidden, text: document.querySelector('#prompt-count')?.textContent }));
  record('输入器：接近上限才显示计数', nearLimit.hidden === false && nearLimit.text === '30010 / 32000', nearLimit);

  await page.fill('#prompt-input', '');
  await page.waitForTimeout(200);
  const cleared = await page.evaluate(() => ({
    sendDisabled: document.querySelector('#send-button')?.disabled,
    inputH: Math.round(document.querySelector('#prompt-input').getBoundingClientRect().height),
  }));
  record('输入器：清空后回落并禁用发送', cleared.sendDisabled === true && cleared.inputH === 68, cleared);

  await page.focus('#prompt-input');
  const focused = await page.evaluate(() => ({
    border: getComputedStyle(document.querySelector('.prompt-input')).borderTopColor,
    managed: document.querySelector('#prompt-input').hasAttribute('data-focus-ring-managed'),
  }));
  record('输入器：激活态 1px 近黑描边 + 内层认领焦点（UC-03/04）', focused.border === 'rgb(23, 23, 23)' && focused.managed, focused);

  // 输入法组字期间的回车不是提交
  await page.fill('#prompt-input', '组字保护');
  const composing = await page.evaluate(async () => {
    const input = document.querySelector('#prompt-input');
    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', isComposing: true, bubbles: true, cancelable: true }));
    await new Promise((resolve) => setTimeout(resolve, 400));
    return { value: input.value, view: document.querySelector('#app-shell').dataset.view };
  });
  record('输入器：输入法组字中的回车不提交', composing.value === '组字保护' && composing.view === 'home', composing);

  await page.fill('#prompt-input', '换行');
  await page.keyboard.press('Shift+Enter');
  const shiftEnter = await page.evaluate(() => ({ value: document.querySelector('#prompt-input').value, view: document.querySelector('#app-shell').dataset.view }));
  record('输入器：Shift+Enter 换行不提交', shiftEnter.value === '换行\n' && shiftEnter.view === 'home', shiftEnter);

  // ── D. Enter starts a run; stop sits where send was ───────────────────────
  await page.fill('#prompt-input', '先列出三条本地工作台的观察，再逐个展开成一段话，最后写入 report.md。用中文。');
  await page.waitForTimeout(150);
  await page.press('#prompt-input', 'Enter');
  await page.waitForSelector('#run-view[data-status="running"]', { timeout: 30_000 });
  await page.waitForTimeout(6000);
  const running = await page.evaluate(() => ({
    status: document.querySelector('#run-view')?.dataset.status,
    stopVisible: !document.querySelector('#stop-run')?.hidden,
    sendHidden: document.querySelector('#send-button')?.hidden,
    readOnly: document.querySelector('#prompt-input')?.readOnly,
    placeholder: document.querySelector('#prompt-input')?.placeholder,
    foldOpen: document.querySelector('.pi-process-toggle')?.getAttribute('aria-expanded'),
    label: document.querySelector('.pi-process-label')?.textContent,
    path: document.querySelector('#pane-title')?.textContent,
    marked: document.querySelectorAll('#run-list [aria-current="page"]').length,
    dock: getComputedStyle(document.querySelector('#conversation-pane')).getPropertyValue('--composer-dock').trim(),
  }));
  record('运行中：停止键原位替换发送键 + 输入只读', running.status === 'running' && running.stopVisible && running.sendHidden && running.readOnly === true, running);
  record('运行中：过程折叠展开并写具体动作', running.foldOpen === 'true' && /^正在/.test(running.label || ''), running.label);
  record('运行中：侧栏标记当前研究（且只标记一个）', running.marked === 1, running.marked);
  record('运行中：输入器实测高度写回留白变量', /^\d+px$/.test(running.dock), running.dock);
  await shot('ui-features-running.png');

  await page.click('#stop-run');
  await page.waitForSelector('#run-view[data-status="cancelled"]', { timeout: 60_000 });
  await page.waitForTimeout(800);
  const stopped = await page.evaluate(() => {
    const outcome = document.querySelector('.pi-outcome');
    return {
      status: document.querySelector('#run-view')?.dataset.status,
      stopHidden: document.querySelector('#stop-run')?.hidden,
      outcome: outcome && !outcome.hidden ? outcome.querySelector('.pi-outcome-title')?.textContent : null,
      tone: outcome?.dataset.tone,
      icon: outcome?.querySelector('.pi-outcome-icon .icon')?.dataset.icon,
      toast: document.querySelector('#toast-text')?.textContent,
    };
  });
  record('停止：终态中性（虚线圆、不是 ×）+ 可重新开始', stopped.status === 'cancelled' && stopped.stopHidden === true && stopped.outcome === '已停止研究' && stopped.tone === 'stopped' && stopped.icon === 'dashed-circle', stopped);

  // ── E. history replay + trace interactions ────────────────────────────────
  const catalogue = await fetch(`${baseUrl}/api/runs`).then((response) => response.json());
  const replayCandidates = (catalogue.runs ?? []).filter((run) => run.status === 'finished' && run.hasReport).slice(0, 8);
  let replayRun = null;
  for (const candidate of replayCandidates) {
    await page.click(`#run-list [data-run-id="${candidate.runId}"]`);
    // A finished REST record paints immediately, then its SSE history replay
    // fills the answer and terminal report fact. The child panel deliberately
    // reuses `.pi-output` chrome and precedes the file section in DOM order, so
    // the old generic selector read the hidden child panel even while the
    // archived screenshot showed the report card. Wait on the semantic file
    // section and sample that same reader-visible contract.
    await page.waitForSelector('#run-view[data-status="finished"] [aria-label="生成的文件"]:not([hidden])', {
      timeout: TERMINAL_TIMEOUT,
    }).catch(() => {});
    const probe = await page.evaluate(() => ({
      thinking: document.querySelectorAll('.pi-thinking').length,
      tools: document.querySelectorAll('.pi-tool').length,
      answer: document.querySelectorAll('.pi-answer-body .pi-text').length,
      status: document.querySelector('#run-view')?.dataset.status,
      folded: document.querySelector('.pi-process-toggle')?.getAttribute('aria-expanded') === 'false',
      draft: !document.querySelector('.pi-answer-head')?.hidden && document.querySelector('.pi-answer-head .badge')?.textContent,
      output: !document.querySelector('[aria-label="生成的文件"]')?.hidden,
      actions: [...document.querySelectorAll('.pi-answer-actions > *')].filter((node) => !node.hidden).map((node) => node.getAttribute('aria-label')),
      unavailable: /读不到/.test(document.querySelector('.pi-outcome')?.textContent ?? ''),
      distance: (() => {
        const scroll = document.querySelector('#conversation-scroll');
        return Math.round(scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight);
      })(),
    }));
    const score = probe.thinking + probe.tools + probe.answer;
    if (!replayRun || score > replayRun.score) replayRun = { id: candidate.runId, score, ...probe };
    if (probe.thinking > 0 && probe.tools > 0 && probe.answer > 0) break;
  }
  record('历史重放：时间线完整、贴底、过程已收起', replayRun?.thinking > 0 && replayRun?.tools > 0 && replayRun?.distance === 0 && replayRun?.status === 'finished' && replayRun?.folded && !replayRun?.unavailable, replayRun);
  record('回答外壳：待核验草稿 + 文件卡 + 动作条（规范 6.1）', replayRun?.draft === '待核验草稿' && replayRun?.output === true && replayRun?.actions?.includes('复制回答'), { draft: replayRun?.draft, actions: replayRun?.actions });

  const replayLeaks = await page.evaluate(() => {
    // Interface copy only: the research title and prompt are the user's own words.
    const chrome = [...document.querySelectorAll('#pane-home, #pane-actions, .pi-process-label, .pi-step-row, .pi-answer-head, .pi-output, .pi-answer-actions, .composer, #sidebar-foot, .nav-label')]
      .map((node) => `${node.innerText} ${node.getAttribute('aria-label') ?? ''}`).join('\n');
    return ['web_search', 'fetch_content', '执行面', 'session ', 'tokens', '第 1 轮', '本轮 agent'].filter((word) => chrome.includes(word));
  });
  record('对话界面不暴露工具内部名 / 会话 / 轮次记账', replayLeaks.length === 0, replayLeaks);

  await page.click('.pi-process-toggle');
  await page.waitForTimeout(700);
  const fold = await page.evaluate(async () => {
    const details = document.querySelector('.pi-thinking');
    details.querySelector('summary').click();
    await new Promise((resolve) => setTimeout(resolve, 400));
    const after = { open: details.open, chars: details.querySelector('.pi-thinking-body')?.textContent.length };
    details.querySelector('summary').click();
    await new Promise((resolve) => setTimeout(resolve, 300));
    return { process: document.querySelector('.pi-process-toggle').getAttribute('aria-expanded'), after, closedAgain: details.open };
  });
  record('过程折叠可再展开；思考行可展开看正文再收起', fold.process === 'true' && fold.after.open === true && fold.after.chars > 0 && fold.closedAgain === false, fold);

  const tool = await page.evaluate(async () => {
    const row = [...document.querySelectorAll('.pi-tool')].find((node) => node.dataset.hasDetail === 'true');
    if (!row) return null;
    const head = row.querySelector('.pi-tool-head');
    const label = row.querySelector('.pi-tool-name')?.textContent;
    head.click();
    await new Promise((resolve) => setTimeout(resolve, 500));
    const openedHeight = Math.round(row.getBoundingClientRect().height);
    const open = row.open;
    const detail = row.querySelector('.pi-tool-output')?.textContent + row.querySelector('.pi-tool-result')?.textContent;
    head.click();
    await new Promise((resolve) => setTimeout(resolve, 500));
    return { label, open, openedHeight, closedHeight: Math.round(row.getBoundingClientRect().height), closedAgain: row.open, hostPath: /\/workspaces\/[0-9a-f]{6,}/.test(detail) };
  });
  record('工具行：写动作不写工具名；可展开看细节、不露宿主路径', Boolean(tool) && !/web_search|bash|fetch|write|read/.test(tool.label) && tool.open && tool.openedHeight > tool.closedHeight && tool.closedAgain === false && !tool.hostPath, tool);
  await shot('ui-features-trace.png');

  // ── F. progress & context panel ───────────────────────────────────────────
  await page.click('#progress-toggle');
  await page.waitForTimeout(600);
  const panel = await page.evaluate(() => ({
    open: document.querySelector('#progress-layer')?.dataset.open,
    expanded: document.querySelector('#progress-toggle')?.getAttribute('aria-expanded'),
    label: document.querySelector('#progress-toggle')?.getAttribute('aria-label'),
    on: document.querySelector('#progress-toggle')?.classList.contains('is-on'),
    steps: document.querySelectorAll('.progress-step').length,
    facts: document.querySelector('.progress-facts')?.innerText,
    width: Math.round(document.querySelector('#progress-panel').getBoundingClientRect().width),
    inset: getComputedStyle(document.querySelector('#conversation-scroll')).paddingRight,
  }));
  record('进度与上下文：340 浮层 / 开关两态 / 步骤与运行信息', panel.open === 'true' && panel.expanded === 'true' && panel.label === '收起进度与上下文' && panel.on && panel.steps > 0 && /状态/.test(panel.facts) && panel.width === 340 && panel.inset === '356px', panel);
  await page.click('.segmented-item:nth-child(2)');
  const sourcesTab = await page.evaluate(() => ({ selected: document.querySelector('.segmented-item:nth-child(2)').getAttribute('aria-selected'), shown: !document.querySelector('#progress-sources').hidden }));
  record('进度与上下文：来源页签可切换', sourcesTab.selected === 'true' && sourcesTab.shown, sourcesTab);
  await page.keyboard.press('Escape');
  await page.waitForTimeout(400);
  const escaped = await page.evaluate(() => ({ open: document.querySelector('#progress-layer')?.dataset.open, focus: document.activeElement?.id }));
  record('进度与上下文：Esc 收起并把焦点还给开关', escaped.open === 'false' && escaped.focus === 'progress-toggle', escaped);

  // ── G. report slot ────────────────────────────────────────────────────────
  await page.click('.pi-report-button');
  await page.waitForSelector('#preview-article h1', { timeout: 15_000 });
  await page.waitForTimeout(600);
  const report = await page.evaluate(() => {
    const surface = document.querySelector('.surface');
    const pane = document.querySelector('.pane');
    const body = document.querySelector('#preview-body');
    const dockRect = document.querySelector('.composer-dock').getBoundingClientRect();
    const paneRect = pane.getBoundingClientRect();
    const drawerRect = document.querySelector('#preview-drawer').getBoundingClientRect();
    const visibleNames = [...document.querySelectorAll('button, a[href]')]
      .filter((node) => node.offsetParent !== null && node.getAttribute('aria-label'))
      .map((node) => node.getAttribute('aria-label'));
    return {
      open: document.querySelector('#app-shell').classList.contains('preview-open'),
      columns: getComputedStyle(surface).gridTemplateColumns.split(' ').length,
      paneW: Math.round(paneRect.width),
      drawerW: Math.round(drawerRect.width),
      title: document.querySelector('#preview-title')?.textContent,
      titleColor: getComputedStyle(document.querySelector('#preview-title')).color,
      badge: document.querySelector('.preview-name .badge')?.textContent,
      // The guard is about the grid row bug (a slot that cannot scroll), not about
      // this report's length: pad a short report so the container is actually tested.
      bodyScrollable: (() => {
        if (body.scrollHeight > body.clientHeight + 1) return true;
        const spacer = document.createElement('div');
        spacer.style.height = '3000px';
        document.querySelector('#preview-article').append(spacer);
        const scrollable = body.scrollHeight > body.clientHeight + 1;
        body.scrollTop = 400;
        const moved = body.scrollTop > 0;
        body.scrollTop = 0;
        spacer.remove();
        return scrollable && moved;
      })(),
      dockInsidePane: dockRect.right <= paneRect.right + 1,
      toggleHidden: document.querySelector('#pane-preview-toggle').hidden,
      collapseVisible: !document.querySelector('#conversation-collapse').hidden,
      closeIcon: document.querySelector('#preview-close .icon')?.dataset.icon,
      duplicateNames: visibleNames.filter((name, index) => visibleNames.indexOf(name) !== index),
      overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
    };
  });
  record('报告右槽：两栏一线、默认对半、无横向溢出', report.open && report.columns === 3 && Math.abs(report.paneW - report.drawerW) <= 2 && !report.overflow, report);
  record('报告右槽：名字档写文件名（近黑）+ 待核验草稿', report.title === 'report.md' && report.titleColor === 'rgb(23, 23, 23)' && report.badge === '待核验草稿', report);
  record('报告右槽：开合键双箭头、左栏选择键让位、同屏无重名（UC-12 / S-04）', report.toggleHidden && report.collapseVisible && report.closeIcon === 'chevrons-left' && report.duplicateNames.length === 0, report);
  record('报告右槽：正文能滚动、输入器只占左栏', report.bodyScrollable && report.dockInsidePane, report);

  const handle = await page.$('#split-handle');
  const box = await handle.boundingBox();
  await page.mouse.move(box.x + 0.5, box.y + 200);
  await page.mouse.down();
  await page.mouse.move(box.x + 120, box.y + 200, { steps: 6 });
  await page.mouse.up();
  await page.waitForTimeout(200);
  const dragged = await page.evaluate(() => ({ paneW: Math.round(document.querySelector('.pane').getBoundingClientRect().width), stored: localStorage.getItem('pi-glink-report-split') }));
  record('分割线：可拖改宽并记住', Math.abs(dragged.paneW - (report.paneW + 120)) <= 4 && Number(dragged.stored) === dragged.paneW, dragged);
  const moved = await handle.boundingBox();
  await page.mouse.move(moved.x + 0.5, moved.y + 200);
  await page.mouse.down();
  await page.mouse.move(300, moved.y + 200, { steps: 6 });
  await page.mouse.up();
  await page.waitForTimeout(200);
  const clamped = await page.evaluate(() => ({
    paneW: Math.round(document.querySelector('.pane').getBoundingClientRect().width),
    drawerW: Math.round(document.querySelector('#preview-drawer').getBoundingClientRect().width),
  }));
  record('分割线：对话列不窄于 560、报告不窄于 360', clamped.paneW === 560 && clamped.drawerW >= 360, clamped);
  await page.dblclick('#split-handle');
  await page.waitForTimeout(200);
  const reset = await page.evaluate(() => ({ paneW: Math.round(document.querySelector('.pane').getBoundingClientRect().width), stored: localStorage.getItem('pi-glink-report-split') }));
  record('分割线：双击复位', Math.abs(reset.paneW - report.paneW) <= 2 && reset.stored === null, reset);

  await page.click('#conversation-collapse');
  await page.waitForTimeout(300);
  const collapsedConversation = await page.evaluate(() => ({
    paneHidden: getComputedStyle(document.querySelector('.pane')).display === 'none',
    expandVisible: !document.querySelector('#conversation-expand').hidden,
    drawerW: Math.round(document.querySelector('#preview-drawer').getBoundingClientRect().width),
    focus: document.activeElement?.id,
  }));
  record('对话栏可收起让报告占满，展开键落在报告栏头最左', collapsedConversation.paneHidden && collapsedConversation.expandVisible && collapsedConversation.drawerW > 1200 && collapsedConversation.focus === 'conversation-expand', collapsedConversation);
  await page.click('#conversation-expand');
  await page.waitForTimeout(300);

  // 主区放不下 560 + 1 + 360 时，报告改覆盖浮层（按主区实宽判，不按视口）
  await page.setViewportSize({ width: 1000, height: 900 });
  await page.waitForTimeout(500);
  const overlay = await page.evaluate(() => {
    const surface = document.querySelector('.surface').getBoundingClientRect();
    const drawer = document.querySelector('#preview-drawer').getBoundingClientRect();
    return {
      overlay: document.querySelector('#app-shell').classList.contains('slot-overlay'),
      surfaceW: Math.round(surface.width),
      drawerInside: drawer.left >= surface.left - 1 && drawer.right <= surface.right + 1,
      handleHidden: getComputedStyle(document.querySelector('#split-handle')).display === 'none',
      overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
    };
  });
  record('窄主区：报告改覆盖浮层、不溢出、竖线隐藏', overlay.overlay && overlay.drawerInside && overlay.handleHidden && !overlay.overflow, overlay);
  await page.setViewportSize(VIEWPORT);
  await page.waitForTimeout(500);

  const downloadPromise = page.waitForEvent('download', { timeout: 20_000 });
  await page.click('#preview-download');
  const download = await downloadPromise;
  await download.saveAs('/tmp/pi-glink-ui-features-report.md');
  const stat = await import('node:fs/promises').then((fs) => fs.stat('/tmp/pi-glink-ui-features-report.md'));
  record('报告下载：落盘成功、文件名可读', stat.size > 0 && /^研究报告/.test(download.suggestedFilename()), { file: download.suggestedFilename(), bytes: stat.size });

  await shot('ui-features-report.png');
  await page.click('#preview-close');
  await page.waitForTimeout(400);
  const closed = await page.evaluate(() => ({
    open: document.querySelector('#app-shell').classList.contains('preview-open'),
    toggleVisible: !document.querySelector('#pane-preview-toggle').hidden,
    expanded: document.querySelector('#pane-preview-toggle').getAttribute('aria-expanded'),
    focus: document.activeElement?.id,
  }));
  record('报告右槽：收起后开关回到左栏栏头并接住焦点', closed.open === false && closed.toggleVisible && closed.expanded === 'false' && closed.focus === 'pane-preview-toggle', closed);

  // ── H. sidebar collapse ───────────────────────────────────────────────────
  await page.click('#sidebar-toggle');
  await page.waitForTimeout(300);
  const collapsed = await page.evaluate(() => {
    const item = document.querySelector('#run-list .nav-item');
    const title = item.querySelector('.nav-title');
    return {
      width: Math.round(document.querySelector('#sidebar').getBoundingClientRect().width),
      expanded: document.querySelector('#sidebar-toggle').getAttribute('aria-expanded'),
      label: document.querySelector('#sidebar-toggle').getAttribute('aria-label'),
      itemW: Math.round(item.getBoundingClientRect().width),
      titleClipped: getComputedStyle(title).position === 'absolute' && title.getBoundingClientRect().width <= 1,
      newBorder: getComputedStyle(document.querySelector('#new-research')).borderTopColor,
      overflowingText: [...document.querySelectorAll('#run-list .nav-item')].some((node) => node.scrollWidth > node.clientWidth + 1),
    };
  });
  record('侧栏：折叠到 64 图标条 + aria 两态 + 标签降为 sr-only', collapsed.width === 64 && collapsed.expanded === 'false' && collapsed.label === '展开侧栏' && collapsed.itemW === 36 && collapsed.titleClipped && !collapsed.overflowingText, collapsed);
  record('侧栏：收起态「新建」去掉描边', collapsed.newBorder === 'rgba(0, 0, 0, 0)', collapsed.newBorder);
  await page.hover('#run-list .nav-item');
  await page.waitForTimeout(500);
  const tip = await page.evaluate(() => {
    const bubble = document.querySelector('.tooltip');
    const cs = bubble ? getComputedStyle(bubble) : null;
    return bubble && !bubble.hidden ? { text: bubble.textContent, radius: cs.borderTopLeftRadius, size: cs.fontSize, bg: cs.backgroundColor } : null;
  });
  record('侧栏：收起态悬停给出名字（Tooltip 圆角 8 / 12px / 近黑）', Boolean(tip?.text) && tip.radius === '8px' && tip.size === '12px' && tip.bg === 'rgb(23, 23, 23)', tip);
  await page.mouse.move(700, 450);
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

  // ── I. breakpoints（标准库 max-[N] = width < N）────────────────────────────
  const layout = [];
  for (const width of BREAKPOINTS) {
    await page.setViewportSize({ width, height: 900 });
    await page.waitForTimeout(350);
    layout.push(await page.evaluate((viewportWidth) => ({
      width: viewportWidth,
      sidebar: Math.round(document.querySelector('#sidebar').getBoundingClientRect().width),
      cards: getComputedStyle(document.querySelector('.home-cards')).gridTemplateColumns.split(' ').length,
      overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
    }), width));
  }
  const expectedSidebar = { 1512: 240, 1250: 240, 1050: 240, 1049: 212, 900: 212, 721: 212, 720: 64, 600: 64 };
  record('断点：侧栏 240 / 212 / 64，边界与标准库同为 width < N', layout.every((item) => item.sidebar === expectedSidebar[item.width]), layout.map((item) => `${item.width}:${item.sidebar}`).join(' '));
  record('断点：各档均无横向溢出', layout.every((item) => !item.overflow));
  record('断点：首页双列 → 单列', layout.find((item) => item.width === 1512)?.cards === 2 && layout.find((item) => item.width === 1049)?.cards === 1);
  await page.setViewportSize(VIEWPORT);
  await page.waitForTimeout(300);

  // ── J. keyboard focus & skip link ─────────────────────────────────────────
  await boot();
  await page.keyboard.press('Tab');
  await page.waitForTimeout(250);
  const skip = await page.evaluate(() => ({
    active: document.activeElement.className,
    visible: Math.round(document.querySelector('.skip-link').getBoundingClientRect().top) >= 0,
  }));
  record('跳转链接：Tab 首个焦点且滑入可见', skip.active === 'skip-link' && skip.visible === true, skip);
  const rings = [];
  for (let index = 0; index < 4; index += 1) {
    await page.keyboard.press('Tab');
    rings.push(await page.evaluate(() => {
      const node = document.activeElement;
      const cs = getComputedStyle(node);
      return { id: node.id || node.className, outline: `${cs.outlineStyle} ${cs.outlineWidth} ${cs.outlineColor}` };
    }));
  }
  record('键盘焦点可见：侧栏按钮与导航行都画 2px --ring（不再透明）', rings.every((ring) => ring.outline === 'solid 2px rgb(104, 104, 104)'), rings);

  const cleanConsoleErrors = consoleErrors.length;
  record('浏览器控制台无错误（注入失败前）', cleanConsoleErrors === 0, consoleErrors.slice(0, 3));

  // ── K. empty state (stubbed API) ──────────────────────────────────────────
  await page.route('**/api/runs', (route) => {
    if (route.request().method() !== 'GET') return route.continue();
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ runs: [] }) });
  });
  await boot();
  const empty = await page.evaluate(() => {
    const node = document.querySelector('#run-list .empty-state');
    return {
      role: node?.getAttribute('role'),
      variant: node?.dataset.variant,
      image: node?.querySelector('img')?.getAttribute('src'),
      title: node?.querySelector('.empty-state-title')?.textContent,
      homeEmpty: document.querySelectorAll('#home-run-list .empty-state, #home-report-list .empty-state').length,
      rows: document.querySelectorAll('#run-list [data-run-id]').length,
    };
  });
  record('空列表：EmptyState（插画 + 标题 + role=status，UC-05）', empty.rows === 0 && empty.role === 'status' && empty.variant === 'no-data' && /no-data\.svg$/.test(empty.image || '') && empty.title === '还没有研究记录' && empty.homeEmpty === 2, empty);
  await page.unroute('**/api/runs');

  // ── L. unreadable history (stubbed 404 stream) ───────────────────────────
  if (replayRun?.id) {
    await page.route(`**/api/runs/${replayRun.id}/events`, (route) => route.fulfill({ status: 404, contentType: 'application/json', body: '{"error":"unknown_run"}' }));
    await boot();
    await page.click(`#run-list [data-run-id="${replayRun.id}"]`);
    // The product deliberately retries a refused EventSource after
    // 1.5s + 4s + 10s before declaring it unavailable. The former fixed 2s
    // sample only observed the reconnecting phase. Wait for the bounded retry
    // contract to settle, while keeping a timeout so a real regression records
    // the assertion below instead of wedging the suite.
    await page.waitForFunction(() => {
      const outcome = document.querySelector('#run-view .pi-outcome');
      return outcome && !outcome.hidden &&
        outcome.querySelector('.pi-outcome-title')?.textContent === '过程记录暂时读不到';
    }, { timeout: 20_000 }).catch(() => {});
    const unreadable = await page.evaluate(() => {
      const outcome = document.querySelector('.pi-outcome');
      return {
        shown: outcome && !outcome.hidden,
        title: outcome?.querySelector('.pi-outcome-title')?.textContent,
        actions: [...(outcome?.querySelectorAll('.pi-outcome-actions button') ?? [])].map((node) => node.textContent.trim()),
      };
    });
    record('过程记录读不到：给出说明与下一步（重试 / 打开报告），不再无限「重连中」', unreadable.shown && unreadable.title === '过程记录暂时读不到' && unreadable.actions.includes('重试'), unreadable);
    await page.unroute(`**/api/runs/${replayRun.id}/events`);
  }

  // ── M. module-graph failure face ──────────────────────────────────────────
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
