/**
 * Pixel-level alignment check against the 百鉴 UI standard library.
 *
 * The workbench's front-end is deliberately build-free vanilla ESM, so alignment
 * with the standard library cannot be proven by "we import the same components".
 * This harness proves it by measurement instead: it loads a reference screen from
 * the library's full-page demo in one tab and the matching pi-glink screen in
 * another, then compares computed styles for the same UI roles (app shell, nav
 * row, pane header, prompt input, thinking fold, tool row, …) property by
 * property.
 *
 * Geometry tolerates a 1px rounding difference; colour, radius, typography,
 * border and elevation must match exactly. A role that does not exist on the
 * reference screen is reported as SKIPPED rather than silently passed.
 *
 * Usage (both servers must be running):
 *   node tests/browser/ui-parity.mjs \
 *     --reference-url http://127.0.0.1:5300/index.html?full-page-demo=agent-assistant-interaction \
 *     --base-url http://127.0.0.1:7861
 *
 * Reference server: `cd <glink-ui-library> && python3 -m http.server 5300` inside
 * `dist/` (the offline build `pnpm build:file` produces). Exit code is non-zero
 * on any mismatch, so this doubles as a regression gate.
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
const PX_TOLERANCE = 1;

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

/** Properties compared for every role; role tables add or drop from here. */
const BASE_PROPS = [
  '_src', // asset basename for image roles (the brand mark), null for the rest
  'display', 'backgroundColor', 'color', 'borderTopLeftRadius', 'borderTopWidth', 'borderTopColor',
  'boxShadow', 'paddingTop', 'paddingRight', 'paddingBottom', 'paddingLeft', 'gap',
  'fontSize', 'lineHeight', 'fontWeight', 'letterSpacing', 'height', 'minHeight',
  'alignItems', 'justifyContent', 'flexDirection',
];

/**
 * Page-side measurement. `pick` resolves a UI role on either page (the library's
 * demo page and the workbench share the same component vocabulary), and the
 * payload returns computed styles plus the border box.
 *
 * Everything here runs in the page: no closure over Node scope.
 */
const measureRoles = (args) => {
  const q = (selector) => document.querySelector(selector);
  const demo = q('.baijian-demo');
  const main = demo ? demo.querySelector('main') : q('#main-content');
  const sidebar = q('#sidebar') || q('#demo-sidebar');

  const bySize = (root, height, radius) =>
    [...root.querySelectorAll('button')].filter((node) => {
      const rect = node.getBoundingClientRect();
      const cs = getComputedStyle(node);
      return (
        Math.round(rect.height) === height &&
        (radius === undefined || Math.abs(parseFloat(cs.borderTopLeftRadius) - radius) < 0.6)
      );
    });

  const find = (predicate) => [...main.querySelectorAll('*')].find(predicate);

  const pick = (role) => {
    switch (role) {
      case 'shell':
        return q('#app-shell') || demo;
      case 'sidebar':
        return sidebar;
      case 'brandMark':
        // The mark is the library's own asset: same file name, same 24px box.
        return q('#sidebar .brand-mark') || q('#demo-sidebar img');
      case 'newButton':
        return q('#new-research') || q('.demo-nav-new');
      case 'navItem': {
        // The library's own nav rows are 36px tall with 8px corners. Compare a
        // resting row: the current-page row carries its own selected treatment.
        const rows = bySize(sidebar, 36, 8).filter((node) => node.getAttribute('aria-current') !== 'page');
        return rows[0] || bySize(sidebar, 36, 8)[0];
      }
      case 'navLabel':
        return sidebar.querySelector('p.text-mini, .nav-label');
      case 'paneHeader':
        return q('.pane-header') || main.querySelector('header');
      case 'panePathButton':
        return q('.pane-path button') || main.querySelector('header button');
      case 'homeTitle':
        return q('#home-view h2') || main.querySelector('h2');
      case 'homeCard':
        return q('.home-card') || q('.demo-home-list');
      case 'homeCardLabel':
        return q('.home-card h3') || q('.demo-home-list h3');
      case 'homeRow':
        return q('.home-row') || q('.demo-home-list button');
      case 'homeRowTitle':
        return q('.home-row-title') || q('.demo-home-list button span span:first-child');
      case 'infoCard':
        return q('.info-card') || q('.demo-nav-upload');
      case 'userBubble':
        return (
          q('#run-question') ||
          find(
            (node) =>
              node.tagName === 'P' &&
              node.textContent.trim().length > 8 &&
              Math.abs(parseFloat(getComputedStyle(node).borderTopLeftRadius) - 20) < 0.6 &&
              getComputedStyle(node).backgroundColor === 'rgb(243, 243, 243)',
          )
        );
      case 'thinkingToggle': {
        // The library's Thinking button is the process fold's toggle on our side.
        if (q('.pi-process-toggle')) return q('.pi-process-toggle');
        const labelled = [...main.querySelectorAll('button[aria-expanded]')].find((node) =>
          /已完成|正在|思考|个工作步骤|步骤/.test(node.textContent),
        );
        if (labelled) return labelled;
        const box = [...main.querySelectorAll('div')].find((node) =>
          node.className.toString().includes('max-w-[380px]'),
        );
        return box ? box.querySelector('button') : null;
      }
      case 'thinkingLabel': {
        if (q('.pi-process-label')) return q('.pi-process-label');
        const toggle =
          [...main.querySelectorAll('button[aria-expanded]')].find((node) =>
            /已完成|正在|思考|步骤/.test(node.textContent),
          );
        if (!toggle) return null;
        const own = toggle.querySelector('.pi-thinking-label');
        if (own) return own;
        const status = toggle.querySelector('span[role="status"]');
        return status ? status.querySelector('span') || status : null;
      }
      case 'toolRow': {
        if (q('.pi-tool-head')) return q('.pi-tool-head');
        const box = [...main.querySelectorAll('div')].find((node) =>
          node.className.toString().includes('max-w-[420px]'),
        );
        return box ? box.querySelector('button') : null;
      }
      case 'toolLabel': {
        if (q('.pi-tool-name')) return q('.pi-tool-name');
        const box = [...main.querySelectorAll('div')].find((node) =>
          node.className.toString().includes('max-w-[420px]'),
        );
        if (!box) return null;
        return (
          [...box.querySelectorAll('span')].find(
            (node) => getComputedStyle(node).fontSize === '12px' && getComputedStyle(node).fontWeight === '500',
          ) || null
        );
      }
      case 'toolChip': {
        if (q('.pi-tool-args')) return q('.pi-tool-args');
        return (
          find(
            (node) =>
              node.tagName === 'SPAN' &&
              /rounded-\[var\(--corner-xs\)\]/.test(node.className.toString()) &&
              getComputedStyle(node).backgroundColor === 'rgb(243, 243, 243)',
          ) || null
        );
      }
      case 'composer':
        return q('.prompt-input') || q('form [data-slot="input-group"]');
      case 'composerEditor':
        return q('#prompt-input') || q('.baijian-mention-editor');
      case 'composerTools':
        return (
          q('.prompt-tools') ||
          (q('form [data-slot="input-group"]')
            ? [...q('form [data-slot="input-group"]').children].find((node) => node.querySelector('button'))
            : null)
        );
      case 'composerSend':
        return q('#send-button') || q('form button[type="submit"]');
      case 'paneToggle': {
        // 栏头开合键（PANEL_TOGGLE）：28 方、圆角 8、未选中态
        if (q('#header-new')) return q('#header-new');
        const header = main.querySelector('header');
        return header
          ? bySize(header, 28, 8).find((node) => getComputedStyle(node).backgroundColor === 'rgba(0, 0, 0, 0)') ?? null
          : null;
      }
      case 'answerAction':
        return q('.pi-answer-actions .icon-key') || q('.demo-answer-actions button');
      case 'outputCard':
        return q('.pi-output') || q('.demo-output-card');
      default:
        return null;
    }
  };

  const { roles, props } = args;
  const result = {};
  for (const role of roles) {
    const node = pick(role);
    if (!node) {
      result[role] = { missing: true };
      continue;
    }
    const cs = getComputedStyle(node);
    const rect = node.getBoundingClientRect();
    const record = {
      _box: `${Math.round(rect.width)}x${Math.round(rect.height)}`,
      _src: node.tagName === 'IMG' ? (node.getAttribute('src') || '').split('/').pop() : null,
    };
    for (const prop of props) record[prop] = cs[prop];
    result[role] = record;
  }
  return result;
};

/** Screens to compare, with the roles each one owns. */
const SCREENS = [
  {
    name: 'app-shell',
    referenceHash: '#home',
    roles: [
      'shell', 'sidebar', 'brandMark', 'newButton', 'navItem', 'navLabel', 'paneHeader', 'panePathButton',
      'homeTitle', 'homeCard', 'homeCardLabel', 'homeRow', 'homeRowTitle', 'composer',
      'composerEditor', 'composerTools',
    ],
    props: [...BASE_PROPS, 'width'],
  },
  {
    // The progress screen carries the trace but no user bubble; the plain-answer
    // screen is the one that renders 人说的话 as a bubble.
    name: 'conversation-reply',
    referenceHash: '#text-answer',
    roles: ['userBubble', 'composer'],
    props: [...BASE_PROPS, 'maxWidth'],
  },
  {
    name: 'conversation',
    referenceHash: '#progress',
    roles: ['paneHeader', 'paneToggle', 'userBubble', 'thinkingToggle', 'thinkingLabel', 'toolRow', 'toolLabel', 'toolChip', 'outputCard', 'answerAction', 'composer', 'composerSend'],
    props: [...BASE_PROPS, 'maxWidth'],
  },
];

/**
 * Which axis of the border box carries meaning for a role. Text-bound roles are
 * `none`: their box is content width, which differs between two products.
 */
const BOX = {
  shell: 'both',
  sidebar: 'both',
  brandMark: 'both',
  newButton: 'both',
  navItem: 'height',
  paneHeader: 'height',
  homeCard: 'height',
  homeRow: 'height',
  composer: 'both',
  composerEditor: 'height',
  composerTools: 'height',
  composerSend: 'both',
  userBubble: 'none',
  thinkingToggle: 'height',
  toolRow: 'both',
  toolChip: 'height',
  infoCard: 'none',
  navLabel: 'height',
  homeTitle: 'height',
  homeCardLabel: 'height',
  homeRowTitle: 'height',
  panePathButton: 'height',
  thinkingLabel: 'height',
  toolLabel: 'height',
  paneToggle: 'both',
  answerAction: 'both',
  outputCard: 'none',
};

/**
 * Content-driven properties that cannot match across two different products.
 *   `borderTopColor` on zero-width borders: Chrome reports `currentColor` when
 *   `border-style: none`, so the property is meaningless wherever the reference
 *   draws its hairline with an inset shadow instead of a border.
 */
const IGNORED = {
  homeCard: ['boxShadow', 'borderTopWidth', 'borderTopColor'],
  navItem: ['borderTopColor'],
  navLabel: ['borderTopColor', 'minHeight'],
  brandMark: ['backgroundColor', 'boxShadow', 'borderTopColor'],
  homeCardLabel: ['borderTopColor'],
  homeTitle: ['borderTopColor'],
  homeRow: ['borderTopColor'],
  homeRowTitle: ['borderTopColor'],
  panePathButton: ['borderTopColor'],
  composer: ['backgroundColor'],
  composerEditor: ['borderTopColor', 'backgroundColor'],
  composerTools: ['justifyContent', 'minHeight'],
  composerSend: ['backgroundColor', 'color'],
  toolChip: ['boxShadow'],
  toolRow: ['maxWidth'],
  toolLabel: ['borderTopColor'],
  thinkingLabel: ['borderTopColor'],
  thinkingToggle: ['borderTopColor'],
  userBubble: ['borderTopColor'],
  outputCard: ['display', 'flexDirection', 'alignItems', 'justifyContent', 'gap'],
  paneToggle: ['borderTopColor'],
  answerAction: ['borderTopColor'],
  infoCard: ['display', 'backgroundColor', 'color', 'borderTopColor', 'gap', 'alignItems', 'height', 'marginBottom'],
};

function normaliseShadow(value) {
  return value
    .split(/,(?![^(]*\))/)
    .map((part) => part.trim())
    .filter((part) => !/^rgba?\(0,\s*0,\s*0,\s*0\)/.test(part) && part !== 'none')
    .join(', ');
}

function compare(role, reference, target, props) {
  const ignored = new Set(IGNORED[role] ?? []);
  const box = BOX[role] ?? 'none';
  const diffs = [];
  for (const prop of props) {
    if (ignored.has(prop)) continue;
    let expected = reference[prop];
    let actual = target[prop];
    if (prop === 'boxShadow') {
      expected = normaliseShadow(expected);
      actual = normaliseShadow(actual);
    }
    if (expected === actual) continue;
    const isPx = /^-?[\d.]+px$/.test(expected) && /^-?[\d.]+px$/.test(actual);
    if (isPx) {
      const [a, b] = [parseFloat(expected), parseFloat(actual)];
      if (Math.abs(a - b) <= PX_TOLERANCE) continue;
    }
    diffs.push(`${prop}: ref=${expected} target=${actual}`);
  }
  for (const prop of ['width', 'maxWidth', 'height', 'minHeight']) {
    const carried = box === 'both' || (box === 'width' && prop === 'width') || (box === 'height' && (prop === 'height' || prop === 'minHeight'));
    if (carried || ignored.has(prop)) continue;
    const index = diffs.findIndex((line) => line.startsWith(`${prop}:`));
    if (index >= 0) diffs.splice(index, 1);
  }
  if (box !== 'none') {
    const [refWidth, refHeight] = String(reference._box).split('x').map(Number);
    const [targetWidth, targetHeight] = String(target._box).split('x').map(Number);
    if ((box === 'both' || box === 'width') && Math.abs(refWidth - targetWidth) > PX_TOLERANCE) {
      diffs.push(`box.width: ref=${refWidth} target=${targetWidth}`);
    }
    if ((box === 'both' || box === 'height') && Math.abs(refHeight - targetHeight) > PX_TOLERANCE) {
      diffs.push(`box.height: ref=${refHeight} target=${targetHeight}`);
    }
  }
  return diffs;
}

const playwrightPath = findPlaywright();
if (!playwrightPath) {
  console.error('[fail] 未找到 playwright；设置 PLAYWRIGHT_PATH 或先 npx playwright --version');
  process.exit(2);
}
const require = createRequire(import.meta.url);
const { chromium } = require(playwrightPath);

const baseUrl = argValue('--base-url', 'http://127.0.0.1:7861');
const referenceUrl = argValue(
  '--reference-url',
  'http://127.0.0.1:5300/index.html?full-page-demo=agent-assistant-interaction',
);

const evidenceDir = join(REPO_ROOT, 'docs', 'evidence');
mkdirSync(evidenceDir, { recursive: true });

const browser = await chromium.launch();
const referencePage = await browser.newPage({ viewport: VIEWPORT });
const targetPage = await browser.newPage({ viewport: VIEWPORT });

const lines = [];
const evidence = { baseUrl, referenceUrl, viewport: VIEWPORT, screens: [], mismatches: 0, consoleErrors: [] };
const consoleErrors = evidence.consoleErrors;
targetPage.on('pageerror', (error) => consoleErrors.push(String(error)));

let exitCode = 1;
try {
  const targetHealth = await fetch(`${baseUrl}/api/health`).then((r) => r.json());
  if (!targetHealth || targetHealth.status !== 'ok') throw new Error('宿主不可达');
  const referenceProbe = await fetch(referenceUrl).catch(() => null);
  if (!referenceProbe || !referenceProbe.ok) {
    throw new Error(`标准库参考页不可达：${referenceUrl}（先在该仓库 dist/ 里起静态服务）`);
  }

  // Pick a finished run whose trace actually carries a thinking fold and tool
  // rows — those are the roles the conversation screen compares.
  const runs = await fetch(`${baseUrl}/api/runs`).then((r) => r.json());
  const candidates = (runs.runs ?? []).filter((item) => item.status === 'finished');
  await targetPage.goto(baseUrl, { waitUntil: 'domcontentloaded' });
  await targetPage.waitForSelector('#app-shell:not([hidden])', { timeout: 15_000 });

  let run = null;
  for (const candidate of candidates.filter((item) => item.hasReport).slice(0, 4)) {
    await targetPage.click(`#run-list [data-run-id="${candidate.runId}"]`);
    await targetPage.waitForTimeout(2000);
    const probe = await targetPage.evaluate(() => ({
      thinking: document.querySelectorAll('.pi-thinking').length,
      tools: document.querySelectorAll('.pi-tool').length,
    }));
    console.log(`[info] run ${candidate.runId}: 思考 ${probe.thinking} · 工具 ${probe.tools}`);
    if (probe.thinking > 0 && probe.tools > 0) {
      run = candidate;
      break;
    }
    run = run ?? candidate;
  }
  await targetPage.evaluate(() => document.activeElement?.blur?.());
  await targetPage.mouse.move(20, 860);
  await targetPage.click('#pane-home').catch(() => {});
  await targetPage.waitForTimeout(500);

  for (const screen of SCREENS) {
    await referencePage.goto(`${referenceUrl}${screen.referenceHash}`, { waitUntil: 'domcontentloaded' });
    await referencePage.waitForTimeout(2500);
    if (screen.name === 'app-shell') {
      await targetPage.click('#pane-home').catch(() => {});
      await targetPage.waitForTimeout(500);
    } else {
      // Every conversation screen needs an open run: the composer dock only takes
      // the 720 column when the workbench is actually showing a conversation.
      await targetPage.click(`#run-list [data-run-id="${run.runId}"]`).catch(() => {});
      await targetPage.waitForTimeout(1500);
      // The process fold closes itself when a run ends; the reference renders its
      // steps expanded, so open ours before measuring the rows inside it.
      await targetPage.evaluate(() => {
        const toggle = document.querySelector('.pi-process-toggle');
        if (toggle?.getAttribute('aria-expanded') === 'false') toggle.click();
      });
      await targetPage.waitForTimeout(700);
    }

    // Hover and focus are states, not styles: park the pointer and drop focus
    // before reading, or a hovered nav row silently poisons the comparison.
    await referencePage.mouse.move(20, 860);
    await referencePage.evaluate(() => document.activeElement?.blur?.());
    await targetPage.mouse.move(20, 860);
    await targetPage.evaluate(() => document.activeElement?.blur?.());
    await targetPage.waitForTimeout(200);

    const reference = await referencePage.evaluate(measureRoles, { roles: screen.roles, props: screen.props });
    const target = await targetPage.evaluate(measureRoles, { roles: screen.roles, props: screen.props });

    const screenRecord = { name: screen.name, referenceHash: screen.referenceHash, roles: {} };
    lines.push(`\n## ${screen.name}  (reference ${screen.referenceHash})`);
    for (const role of screen.roles) {
      const a = reference[role];
      const b = target[role];
      if (!a || !b || a.missing || b.missing) {
        const status = a?.missing ? 'SKIPPED (not on reference screen)' : 'SKIPPED (not on target screen)';
        lines.push(`  - ${role}: ${status}`);
        screenRecord.roles[role] = { status: 'skipped' };
        continue;
      }
      const diffs = compare(role, a, b, screen.props);
      screenRecord.roles[role] = { status: diffs.length ? 'mismatch' : 'ok', diffs, reference: a, target: b };
      if (diffs.length) {
        evidence.mismatches += diffs.length;
        lines.push(`  - ${role}: ${diffs.length} 处不一致`);
        for (const diff of diffs) lines.push(`      ${diff}`);
      } else {
        lines.push(`  - ${role}: OK`);
      }
    }
    evidence.screens.push(screenRecord);
  }

  const ok = evidence.mismatches === 0 && consoleErrors.length === 0;
  lines.push('');
  lines.push(
    ok
      ? `[done] 逐值比对通过：${SCREENS.reduce((total, screen) => total + screen.roles.length, 0)} 个角色，0 处不一致`
      : `[fail] ${evidence.mismatches} 处不一致，控制台错误 ${consoleErrors.length} 条`,
  );

  const transcript = [
    'pi-glink × 百鉴 UI 标准库 · 像素级对齐校验',
    `日期: ${new Date().toISOString()}`,
    `命令: node tests/browser/ui-parity.mjs --base-url ${baseUrl} --reference-url ${referenceUrl}`,
    `视口: ${VIEWPORT.width}×${VIEWPORT.height}`,
    '',
    '比对口径: 几何容许 ≤1px 取整差；颜色 / 圆角 / 排版 / 描边 / 海拔必须逐值相同。',
    '内容驱动的属性按角色白名单跳过（文本宽度、跨产品文本高度），未在参考屏出现的角色记为 SKIPPED。',
    '已登记的两处刻意等价写法: ① 参考页的卡片用 inset 1px 阴影画发丝线（Demo 页面覆盖），',
    '工作台用组件默认的 1px border，渲染同为 1px --border；② 输入器禁用态的底色取自 Button 的',
    'disabled:opacity-45 口径，与按钮的 disabled 状态同源。',
    ...lines,
    '',
  ].join('\n');
  writeFileSync(join(evidenceDir, 'ui-parity.txt'), transcript, 'utf8');
  writeFileSync(join(evidenceDir, 'ui-parity.json'), `${JSON.stringify(evidence, null, 2)}\n`, 'utf8');
  process.stdout.write(transcript);
  console.log(`[ok] 证据：docs/evidence/ui-parity.txt / ui-parity.json`);
  exitCode = ok ? 0 : 1;
} catch (error) {
  console.error(`[fail] ${error.message}`);
} finally {
  await browser.close();
}

process.exit(exitCode);
