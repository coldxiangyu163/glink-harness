/** Workbench task acceptance: real fake-execution host -> projection/store/SSE
 * -> workspace. No model, cloud, existing service or hand-built final UI state.
 * Reference: the standard library's actual ProgressPanel + scene CSS via its
 * installed Vite, not the standalone TaskRows showcase.
 * node tests/browser/todo-panel.mjs --reference /path/to/glink-ui-library
 */
import assert from 'node:assert/strict';
import { mkdirSync, mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { createServer as createPortProbe } from 'node:net';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { chromium } from 'playwright';
import { startHost } from './support/host.mjs';

const arg = (name) => process.argv[process.argv.indexOf(name) + 1];
if (!process.argv.includes('--reference')) throw new Error('--reference is required; parity cannot silently skip');
const referenceRoot = resolve(arg('--reference'));
const out = process.argv.includes('--out') ? resolve(arg('--out')) : mkdtempSync(join(tmpdir(), 'glink-workbench-todo-'));
mkdirSync(out, { recursive: true });
const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const { createServer } = await import(pathToFileURL(join(referenceRoot, 'node_modules/vite/dist/node/index.js')));
const results = [];
let host, reference, browser;
function check(name, condition, detail) {
  results.push({ name, ok: Boolean(condition), detail });
  assert.ok(condition, `${name}: ${JSON.stringify(detail)}`);
}
async function loopbackPort() {
  const probe = createPortProbe();
  return new Promise((resolve, reject) => {
    probe.once('error', reject);
    probe.listen(0, '127.0.0.1', () => {
      const port = probe.address().port;
      probe.close(error => error ? reject(error) : resolve(port));
    });
  });
}
function measure(selector) {
  const panel = document.querySelector(selector);
  const list = panel.querySelector('ol.pi-todo-list') ?? panel.querySelector('ul');
  const rows = [...list.children];
  const style = (node, fields) => Object.fromEntries(fields.map(field => [field, getComputedStyle(node)[field]]));
  const styles = style(panel, ['paddingTop', 'paddingRight', 'paddingBottom', 'paddingLeft', 'borderRadius', 'borderWidth', 'backgroundColor', 'boxShadow']);
  styles.boxShadow = styles.boxShadow.split(/,(?![^()]*\))/).map(s => s.trim()).filter(s => !s.startsWith('rgba(0, 0, 0, 0)')).join(', ');
  const row = (i) => {
    const li = rows[i], body = li.querySelector('div'), mark = body.querySelector('span');
    const label = li.querySelector('.progress-step-text') ?? li.querySelector('.text-body');
    return { outer: style(li, ['paddingBottom']), body: style(body, ['gap', 'paddingBottom']),
      text: style(label, ['fontSize', 'lineHeight', 'fontWeight', 'color', 'textDecorationLine']),
      mark: style(mark, ['width', 'height', 'backgroundColor', 'borderStyle']),
      line: { left: getComputedStyle(li, '::before').left, top: getComputedStyle(li, '::before').top,
        width: getComputedStyle(li, '::before').width, background: getComputedStyle(li, '::before').backgroundColor } };
  };
  // The source has one current step; research can have two. Compare equivalent
  // done/running/pending roles, without fabricating a sequential research plan.
  return { panel: styles, done: row(0), running: row(1), pending: row(3),
    close: style(panel.querySelector('button[aria-label="关闭进度与上下文"]'), ['width', 'height', 'borderRadius', 'color']) };
}
async function settled(page) {
  await page.waitForFunction(() => [...document.getAnimations()]
    .filter(a => a.effect?.getTiming().iterations !== Infinity).every(a => a.playState === 'finished'));
}
async function start(page, prompt) {
  await page.fill('#prompt-input', prompt);
  await page.press('#prompt-input', 'Enter');
  await page.waitForSelector('#run-view[data-status="running"], #run-view[data-status="finished"]');
}
async function stop(page) {
  await page.click('#stop-run');
  await page.waitForSelector('#run-view[data-status="cancelled"]');
}
async function openProgress(page) {
  await page.click('#progress-toggle');
  await page.waitForFunction(() => document.querySelector('#progress-layer').dataset.open === 'true');
  await settled(page);
}
try {
  host = await startHost({ repoRoot, mode: 'local', stateDir: join(out, 'state'), logFile: join(out, 'fixture.log') });
  reference = await createServer({
    root: referenceRoot, configFile: join(referenceRoot, 'vite.config.mjs'), cacheDir: join(out, 'vite-cache'),
    server: { host: '127.0.0.1', port: await loopbackPort(), strictPort: true },
    plugins: [{
      name: 'workbench-progress-test-entry',
      resolveId(id) { if (id === '/task-parity.mjs') return '\0task-parity'; },
      load(id) {
        if (id !== '\0task-parity') return;
        return `import React from 'react'; import {createRoot} from 'react-dom/client';
import {ProgressPanel} from '/src/pages/agent-assistant-demo/conversation-workspace.tsx';
import '/src/styles/globals.css';
import '/src/pages/agent-assistant-demo/demo-font.css';
import '/src/pages/agent-assistant-demo/demo-figma.css';
createRoot(document.getElementById('root')).render(React.createElement(ProgressPanel,{editor:true,onClose:()=>{},stage:{done:1,running:1},outputCount:0}));`;
      },
      configureServer(server) {
        server.middlewares.use(async (req, res, next) => {
          if (req.url !== '/task-parity') return next();
          res.setHeader('content-type', 'text/html; charset=utf-8');
          res.end(await server.transformIndexHtml('/task-parity', '<!doctype html><html lang="zh-CN"><div id="root" class="baijian-demo" style="position:relative;height:760px"></div><script type="module" src="/task-parity.mjs"></script></html>'));
        });
      },
    }],
  });
  await reference.listen();
  browser = await chromium.launch();
  const context = await browser.newContext({ viewport: { width: 1254, height: 814 } });
  const page = await context.newPage(), ref = await context.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(String(error)));
  await page.goto(host.baseUrl);
  await page.waitForSelector('#app-shell:not([hidden])');
  await start(page, '工作台任务展示验收 [scenario:todo]');
  await page.waitForFunction(() => document.querySelectorAll('.pi-plan-update').length > 0);
  const closed = await page.evaluate(() => ({
    tasks: document.querySelectorAll('#chat-view .pi-todo-list, #chat-view .pi-todo-panel').length,
    open: document.querySelector('#progress-layer').dataset.open,
    inaccessible: document.querySelector('#progress-layer').inert && document.querySelector('#progress-layer').getAttribute('aria-hidden') === 'true',
    questionY: document.querySelector('#run-question').getBoundingClientRect().y,
    headerBottom: document.querySelector('.pane-header').getBoundingClientRect().bottom,
  }));
  check('默认对话不出现常驻任务卡，问题紧接栏头', closed.tasks === 0 && closed.open === 'false' && closed.inaccessible
    && closed.questionY - closed.headerBottom <= 24, closed);
  await page.screenshot({ path: join(out, 'conversation.png') });
  await openProgress(page);
  await page.waitForSelector('.pi-todo-item');
  const head = page.locator('.progress-section-steps .progress-section-head');
  await head.focus(); await page.keyboard.press('Enter');
  await page.waitForFunction(() => /2 项进行中/.test(document.querySelector('.progress-section-meta').textContent));
  check('新整表到达仍保持用户折叠', await head.getAttribute('aria-expanded') === 'false'
    && await page.locator('.progress-steps-body').isHidden());
  await page.keyboard.press('Space');
  await page.waitForSelector('.pi-todo-list:not([hidden])');
  check('键盘焦点使用可见的中性2px环', await head.evaluate(node => {
    const style = getComputedStyle(node); return style.outlineWidth === '2px' && style.outlineStyle === 'solid';
  }));
  await page.screenshot({ path: join(out, 'keyboard-focus.png'), animations: 'disabled' });
  await page.click('#run-question');
  check('任务整表唯一地出现在进度浮层，而非对话中', await page.locator('#progress-panel .pi-todo-item').count() === 4
    && await page.locator('#chat-view .pi-todo-item').count() === 0);
  await ref.goto(`http://127.0.0.1:${reference.httpServer.address().port}/task-parity`);
  await ref.waitForSelector('.demo-progress-panel li'); await settled(ref);
  const actual = await page.evaluate(measure, '#progress-panel');
  const expected = await ref.evaluate(measure, '.demo-progress-panel');
  check('对齐实际工作台：浮层/时间轴三态/行距/字号/关闭键逐值一致', JSON.stringify(actual) === JSON.stringify(expected), { actual, expected });
  const arcs = await Promise.all([page.evaluate(() => [...document.querySelector('.pi-todo-item [role="progressbar"]').querySelectorAll('circle')].map(c => [c.getAttribute('stroke'), c.getAttribute('stroke-dasharray')])),
    ref.evaluate(() => [...document.querySelector('[role="progressbar"]').querySelectorAll('circle')].map(c => [c.getAttribute('stroke'), c.getAttribute('stroke-dasharray')]))]);
  check('相同的22px黑色运行环，不伪造百分比', JSON.stringify(arcs[0]) === JSON.stringify(arcs[1])
    && await page.locator('.pi-todo-item [role="progressbar"]').first().getAttribute('aria-valuenow') === null, arcs);
  await page.screenshot({ path: join(out, 'progress.png'), animations: 'disabled' });
  await ref.screenshot({ path: join(out, 'reference.png'), animations: 'disabled' });

  for (const [width, height] of [[1512,900], [1254,814], [1250,814], [1249,814], [1050,814], [1049,814], [721,814], [720,814], [600,814], [599,814], [390,700], [320,640], [1254,640]]) {
    await page.setViewportSize({ width, height }); await settled(page);
    const boxes = await page.evaluate(() => {
      const rect = selector => { const r = document.querySelector(selector).getBoundingClientRect(); return { x: r.x, y: r.y, width: r.width, height: r.height, right: r.right, bottom: r.bottom }; };
      const panel = document.querySelector('#progress-panel'), meta = document.querySelector('.progress-section-meta');
      return { panel: rect('#progress-panel'), pane: rect('#conversation-pane'), composer: rect('#composer-host'),
        head: rect('.progress-section-steps .progress-section-head'), close: rect('.progress-close'),
        scrollWidth: panel.scrollWidth, clientWidth: panel.clientWidth,
        metaFits: meta.getBoundingClientRect().right <= document.querySelector('.progress-section-steps .progress-section-head').getBoundingClientRect().right,
        listGap: document.querySelector('.pi-todo-list').getBoundingClientRect().top - document.querySelector('.progress-section-steps .progress-section-head').getBoundingClientRect().bottom };
    });
    check(`${width}×${height}: 右侧浮层、不给对话加卡、不挡输入器或关闭键`,
      Math.abs(boxes.panel.width - (width >= 1250 ? 340 : width >= 721 ? 308 : boxes.pane.width - 16)) <= 1
      && boxes.panel.right <= boxes.pane.right && boxes.panel.x >= boxes.pane.x
      && boxes.panel.bottom <= boxes.composer.y && boxes.head.right <= boxes.close.x + 1
      && boxes.scrollWidth <= boxes.clientWidth && boxes.metaFits && Math.abs(boxes.listGap - 16) <= 1, boxes);
  }
  await page.screenshot({ path: join(out, 'short-desktop.png'), animations: 'disabled' });
  await page.setViewportSize({ width: 390, height: 700 }); await settled(page);
  await page.screenshot({ path: join(out, 'mobile.png'), animations: 'disabled' });
  await page.click('.progress-close');
  check('关闭×关闭浮层并归还焦点到栏头入口', await page.locator('#progress-layer').getAttribute('data-open') === 'false'
    && await page.locator('#progress-layer').getAttribute('aria-hidden') === 'true'
    && await page.evaluate(() => document.querySelector('#progress-layer').inert && document.activeElement.id === 'progress-toggle'));
  await page.setViewportSize({ width: 1254, height: 814 });
  await openProgress(page); await page.keyboard.press('Escape');
  check('Esc关闭和焦点归还保持原合同', await page.locator('#progress-layer').getAttribute('data-open') === 'false'
    && await page.evaluate(() => document.activeElement.id === 'progress-toggle'));
  await openProgress(page);
  await stop(page);
  await page.waitForFunction(() => document.querySelector('.progress-card').dataset.status === 'cancelled');
  check('停止不冒充计划完成，也不残留运行环', await page.locator('.pi-todo-item[data-status="stopped"]').count() === 2
    && await page.locator('.pi-todo-item [role="progressbar"]').count() === 0
    && !/进行中/.test(await page.locator('.progress-section-steps .progress-section-meta').innerText()));
  await page.waitForSelector('#pane-preview-toggle:not([hidden])');
  await page.click('#pane-preview-toggle');
  await page.waitForSelector('#preview-drawer:not([hidden])');
  check('报告打开让进度退场，不增加第三列', await page.locator('#progress-layer').getAttribute('data-open') === 'false');
  await openProgress(page);
  check('进度打开让报告退场，任务计划仍在', await page.locator('#preview-drawer').isHidden()
    && await page.locator('.pi-todo-item').count() === 4);
  await page.locator('.segmented-item').nth(1).focus(); await page.keyboard.press('ArrowLeft');
  check('上下文页签键盘合同保持', await page.locator('#progress-outputs').isVisible());
  await page.screenshot({ path: join(out, 'stopped.png'), animations: 'disabled' });
  const firstId = await page.locator('#run-list [aria-current="page"]').getAttribute('data-run-id');
  await page.reload(); await page.waitForSelector('#app-shell:not([hidden])');
  await page.click(`#run-list [data-run-id="${firstId}"]`);
  await page.waitForSelector('#run-view[data-status="cancelled"]');
  await openProgress(page);
  check('历史回放恢复该轮最新整表', await page.locator('.pi-todo-item').count() === 4);
  await start(page, '追加一个无计划的追问 [scenario:tools]');
  await page.waitForSelector('#run-view[data-status="finished"]');
  await openProgress(page);
  check('追问无任务源不继承父计划，历史对话也不堆任务卡', await page.locator('.pi-todo-list').isHidden()
    && await page.locator('#earlier-turns .pi-todo-item, #chat-view .pi-todo-panel').count() === 0
    && await page.locator('#earlier-turns .pi-plan-update').count() > 0);
  check('无计划时仍显示实际执行步骤，保留失败文字', await page.locator('.progress-step[data-status="failed"]').count() > 0);
  await page.click('#new-research');
  await start(page, '长任务计划 [scenario:todolong]'); await openProgress(page);
  await page.waitForFunction(() => document.querySelectorAll('.pi-todo-item').length === 20);
  const long = await page.evaluate(() => {
    const panel = document.querySelector('#progress-panel'), list = document.querySelector('.pi-todo-list');
    return { scrollable: panel.scrollHeight > panel.clientHeight, listOverflow: getComputedStyle(list).overflowY,
      textHeight: list.querySelector('.progress-step-text').clientHeight, lineClamp: getComputedStyle(list.querySelector('.progress-step-text')).webkitLineClamp };
  });
  check('长任务全文换行，仅浮层滚动、不再双层滚动', long.scrollable && long.listOverflow === 'visible'
    && long.textHeight > 40 && long.lineClamp === 'none', long);
  await page.emulateMedia({ reducedMotion: 'reduce' });
  check('减弱动效停止无限旋转', await page.evaluate(() => {
    const style = getComputedStyle(document.querySelector('.progress-arc'));
    return style.animationIterationCount === '1' && parseFloat(style.animationDuration) < .001;
  }));
  await page.screenshot({ path: join(out, 'long.png'), animations: 'disabled' });
  await stop(page);
  await page.click('#new-research'); await start(page, '清空任务计划 [scenario:todoempty]'); await openProgress(page);
  await page.waitForFunction(() => document.querySelector('.pi-todo-list').children.length === 4);
  await page.waitForFunction(() => document.querySelector('.pi-todo-list').children.length === 0);
  check('最新空表清除旧计划，不把计划调用重复列为任务', await page.locator('.pi-todo-list').isHidden()
    && !/更新计划/.test(await page.locator('.progress-steps:not(.pi-todo-list)').innerText()));
  await stop(page);
  check('生产前端无脚本错误', errors.length === 0, errors);
} finally {
  await browser?.close(); await reference?.close();
  const exit = await host?.stop();
  writeFileSync(join(out, 'results.json'), JSON.stringify({ results, host: host && { pid: host.pid, exit } }, null, 2));
  console.log(`工作台任务验收: ${results.filter(r => r.ok).length}/${results.length}; 证据: ${out}`);
}
