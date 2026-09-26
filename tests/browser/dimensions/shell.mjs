/**
 * Shell + composer dimension: workbench chrome, sidebar, breakpoints,
 * keyboard focus, and the composer's interaction contract.
 */

import { waitFor } from '../support/harness.mjs';
import { waitShell } from '../support/flows.mjs';

const BREAKPOINTS = [1512, 1250, 1050, 1049, 900, 721, 720, 600];
const EXPECTED_SIDEBAR = { 1512: 240, 1250: 240, 1050: 240, 1049: 212, 900: 212, 721: 212, 720: 64, 600: 64 };

export async function run(ctx) {
  const { suite, page } = ctx;
  suite.dimension('shell');
  await waitShell(page);

  // ── geometry ──────────────────────────────────────────────────────────────
  const shell = await page.evaluate(() => {
    const box = (selector) => {
      const node = document.querySelector(selector);
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
  suite.record(
    '外壳几何：8px 沟槽 + 240 导航 + 52 栏头，品牌取标准库资产',
    shell.sidebar.w === 240 && shell.header.h === 52 && shell.gap === '8px' &&
      shell.padding === '8px' && String(shell.brand).endsWith('logo-icon.svg') &&
      shell.title === '研究工作台',
    shell,
  );
  suite.record('字体：页面作用域与标准库 Demo 同族（Noto Sans SC 优先）', /^"?Noto Sans SC"?/.test(shell.font), shell.font);
  suite.record('服务正常时侧栏页脚不出现状态牌', shell.alertHidden === true, shell.alertHidden);

  // ── home / empty state (this runs before any run exists) ─────────────────
  const home = await page.evaluate(() => ({
    title: document.querySelector('#home-view .home-title')?.textContent,
    cards: document.querySelectorAll('.home-card').length,
    path: document.querySelector('#pane-title')?.textContent,
    current: document.querySelectorAll('#run-list [aria-current]').length,
    headerActions: [...document.querySelectorAll('#pane-actions button')].filter((node) => !node.hidden).length,
    emptyStates: [...document.querySelectorAll('.empty-state')].map((node) => ({
      where: node.parentElement?.id,
      title: node.querySelector('.empty-state-title')?.textContent,
      role: node.getAttribute('role'),
      variant: node.dataset.variant,
    })),
    rows: document.querySelectorAll('#run-list [data-run-id]').length,
  }));
  suite.record(
    '首页：标题 + 双卡 + 无选中、栏头右簇为空',
    home.title === '今天要研究什么？' && home.cards === 2 && home.path === '新研究' &&
      home.current === 0 && home.headerActions === 0,
    home,
  );
  suite.record(
    '空态：侧栏 + 两张首页卡都画 EmptyState（role=status / no-data）',
    home.rows === 0 && home.emptyStates.length === 3 &&
      home.emptyStates.every((item) => item.role === 'status' && item.variant === 'no-data') &&
      home.emptyStates[0].title === '还没有研究记录' &&
      home.emptyStates[1].title === '还没有研究' &&
      home.emptyStates[2].title === '还没有报告',
    home.emptyStates,
  );

  // ── sidebar collapse / expand / persistence ──────────────────────────────
  await page.click('#sidebar-toggle');
  await page.waitForTimeout(350);
  const collapsed = await page.evaluate(() => {
    const item = document.querySelector('#run-list .nav-item') ?? document.querySelector('#new-research');
    return {
      width: Math.round(document.querySelector('#sidebar').getBoundingClientRect().width),
      expanded: document.querySelector('#sidebar-toggle').getAttribute('aria-expanded'),
      label: document.querySelector('#sidebar-toggle').getAttribute('aria-label'),
      newBorder: getComputedStyle(document.querySelector('#new-research')).borderTopColor,
      itemW: Math.round(item.getBoundingClientRect().width),
    };
  });
  suite.record(
    '侧栏收起：64 图标条 + aria 两态 + 「新建」去描边',
    collapsed.width === 64 && collapsed.expanded === 'false' &&
      collapsed.label === '展开侧栏' && collapsed.newBorder === 'rgba(0, 0, 0, 0)',
    collapsed,
  );
  await page.hover('#new-research');
  await page.waitForTimeout(500);
  const tip = await page.evaluate(() => {
    const bubble = document.querySelector('.tooltip');
    if (!bubble || bubble.hidden) return null;
    const style = getComputedStyle(bubble);
    return { text: bubble.textContent, radius: style.borderTopLeftRadius, size: style.fontSize, bg: style.backgroundColor };
  });
  suite.record(
    '侧栏收起态悬停：Tooltip 圆角 8 / 12px / 近黑',
    Boolean(tip?.text) && tip.radius === '8px' && tip.size === '12px' && tip.bg === 'rgb(23, 23, 23)',
    tip,
  );
  await page.mouse.move(700, 450);
  await page.reload({ waitUntil: 'domcontentloaded' });
  await waitShell(page);
  const persisted = await page.evaluate(() => ({
    width: Math.round(document.querySelector('#sidebar').getBoundingClientRect().width),
    expanded: document.querySelector('#sidebar-toggle').getAttribute('aria-expanded'),
  }));
  suite.record('侧栏折叠状态刷新后保持', persisted.width === 64 && persisted.expanded === 'false', persisted);
  await page.click('#sidebar-toggle');
  await page.waitForTimeout(350);
  const expanded = await page.evaluate(() => Math.round(document.querySelector('#sidebar').getBoundingClientRect().width));
  suite.record('侧栏可再展开（回到 240）', expanded === 240, expanded);

  // ── breakpoints ───────────────────────────────────────────────────────────
  const measured = [];
  for (const width of BREAKPOINTS) {
    await page.setViewportSize({ width, height: 900 });
    await page.waitForTimeout(320);
    measured.push(
      await page.evaluate((viewportWidth) => {
        const rect = (selector) => Math.round(document.querySelector(selector).getBoundingClientRect().width);
        const lines = document.querySelector('.account-lines');
        const accountArea = document.querySelector('#account-area');
        const cards = document.querySelector('.home-cards');
        return {
          width: viewportWidth,
          sidebar: rect('#sidebar'),
          accountShown: accountArea ? !accountArea.hidden : false,
          accountLines: lines && accountArea && !accountArea.hidden ? getComputedStyle(lines).display : null,
          avatar: rect('#account-avatar'),
          cards: cards ? getComputedStyle(cards).gridTemplateColumns.split(' ').length : null,
          overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
        };
      }, width),
    );
  }
  suite.record(
    '断点：侧栏 240/212/64 与标准库同为 width<N 边界',
    measured.every((item) => item.sidebar === EXPECTED_SIDEBAR[item.width]),
    measured.map((item) => `${item.width}:${item.sidebar}`).join(' '),
  );
  suite.record('断点：各档均无横向溢出', measured.every((item) => !item.overflow), measured.filter((item) => item.overflow));
  suite.record(
    '断点：首页双列 → 单列（1050 边界）',
    measured.find((item) => item.width === 1050)?.cards === 2 &&
      measured.find((item) => item.width === 1049)?.cards === 1,
    measured.map((item) => `${item.width}:${item.cards}`).join(' '),
  );
  if (measured.some((item) => item.accountShown)) {
    suite.record(
      '断点：账号区 <721 只留 28px 头像',
      measured.filter((item) => item.width >= 721).every((item) => item.accountLines === 'flex') &&
        measured.filter((item) => item.width < 721).every((item) => item.accountLines === 'none' && item.avatar === 28),
      measured.map((item) => `${item.width}:${item.accountLines}/${item.avatar}`).join(' '),
    );
  } else {
    suite.skip('断点：账号区 <721 只留 28px 头像', 'local 模式没有账号区');
  }
  await page.setViewportSize({ width: 1512, height: 900 });
  await page.waitForTimeout(300);

  // ── keyboard: skip link + visible focus rings ─────────────────────────────
  // Reload so keyboard traversal starts from the document, not from whatever
  // received the last click (Chromium keeps the sequential-focus start point).
  await page.reload({ waitUntil: 'domcontentloaded' });
  await waitShell(page);
  await page.keyboard.press('Tab');
  await page.waitForTimeout(250);
  const skip = await page.evaluate(() => ({
    active: document.activeElement?.id,
    visible: Math.round(document.querySelector('#skip-link').getBoundingClientRect().top) >= 0,
  }));
  suite.record('跳转链接：Tab 首个焦点且滑入可见', skip.active === 'skip-link' && skip.visible === true, skip);
  const rings = [];
  for (let index = 0; index < 6; index += 1) {
    await page.keyboard.press('Tab');
    await page.waitForTimeout(180);
    rings.push(
      await page.evaluate(() => {
        const node = document.activeElement;
        const style = getComputedStyle(node);
        // The composer input claims its own focus ring (data-focus-ring-managed):
        // its container shows the active state as a 1px near-black border
        // instead of an outline (ui-alignment §3 输入器).
        const managed = node.hasAttribute?.('data-focus-ring-managed') ?? false;
        const composer = document.querySelector('.prompt-input');
        const composerStyle = composer ? getComputedStyle(composer) : null;
        return {
          id: node.id || node.className || node.tagName,
          managed: Boolean(managed),
          outline: `${style.outlineStyle} ${style.outlineWidth} ${style.outlineColor}`,
          composerFocusWithin: composer ? composer.matches(':focus-within') : false,
          composerBorder: composerStyle?.borderTopColor ?? null,
        };
      }),
    );
  }
  const ringSubjects = rings.filter((ring) => !/^(BODY|HTML)$/i.test(String(ring.id ?? '')));
  const outlineSubjects = ringSubjects.filter((ring) => !ring.managed);
  const managedSubjects = ringSubjects.filter((ring) => ring.managed);
  suite.record(
    '键盘焦点可见：普通控件画 2px --ring',
    outlineSubjects.length >= 4 &&
      outlineSubjects.every((ring) => ring.outline === 'solid 2px rgb(104, 104, 104)'),
    outlineSubjects,
  );
  suite.record(
    '键盘焦点可见：受管输入由容器给出激活态（1px 近黑描边 + focus-within）',
    managedSubjects.length === 1 &&
      managedSubjects.every((ring) => ring.composerFocusWithin && ring.composerBorder !== 'rgb(225, 225, 225)'),
    managedSubjects,
  );

  // ── composer interaction contract ─────────────────────────────────────────
  suite.dimension('composer');
  const empty = await page.evaluate(() => ({
    sendDisabled: document.querySelector('#send-button')?.disabled,
    countHidden: document.querySelector('#prompt-count')?.hidden,
  }));
  suite.record('输入器：空输入发送键禁用、不显示计数', empty.sendDisabled === true && empty.countHidden === true, empty);

  await page.fill('#prompt-input', '浏览器验收 · 普通长度');
  await page.waitForTimeout(200);
  const typed = await page.evaluate(() => ({
    composerH: Math.round(document.querySelector('.prompt-input').getBoundingClientRect().height),
    sendDisabled: document.querySelector('#send-button')?.disabled,
    countHidden: document.querySelector('#prompt-count')?.hidden,
  }));
  suite.record('输入器：普通长度 120 高、发送键启用、计数隐藏', typed.composerH === 120 && !typed.sendDisabled && typed.countHidden === true, typed);

  await page.fill('#prompt-input', Array.from({ length: 14 }, (_, index) => `第 ${index + 1} 行`).join('\n'));
  await page.waitForTimeout(250);
  const grown = await page.evaluate(() => ({
    inputH: Math.round(document.querySelector('#prompt-input').getBoundingClientRect().height),
    overflow: getComputedStyle(document.querySelector('#prompt-input')).overflowY,
  }));
  suite.record('输入器：多行自增到 240 上限并滚动', grown.inputH === 240 && grown.overflow === 'auto', grown);

  await page.fill('#prompt-input', 'x'.repeat(30_010));
  await page.waitForTimeout(200);
  const nearLimit = await page.evaluate(() => ({
    hidden: document.querySelector('#prompt-count')?.hidden,
    text: document.querySelector('#prompt-count')?.textContent,
  }));
  suite.record('输入器：接近上限才显示计数', nearLimit.hidden === false && nearLimit.text === '30010 / 32000', nearLimit);

  await page.fill('#prompt-input', '');
  await page.waitForTimeout(250);
  const cleared = await page.evaluate(() => ({
    sendDisabled: document.querySelector('#send-button')?.disabled,
    inputH: Math.round(document.querySelector('#prompt-input').getBoundingClientRect().height),
  }));
  suite.record('输入器：清空后回落并禁用发送', cleared.sendDisabled === true && cleared.inputH === 68, cleared);

  await page.focus('#prompt-input');
  await page.waitForTimeout(150);
  const focused = await page.evaluate(() => ({
    border: getComputedStyle(document.querySelector('.prompt-input')).borderTopColor,
    managed: document.querySelector('#prompt-input').hasAttribute('data-focus-ring-managed'),
  }));
  suite.record('输入器：激活态 1px 近黑描边 + 内层认领焦点', focused.border === 'rgb(23, 23, 23)' && focused.managed, focused);

  await page.fill('#prompt-input', '组字保护');
  const composing = await page.evaluate(async () => {
    const input = document.querySelector('#prompt-input');
    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', isComposing: true, bubbles: true, cancelable: true }));
    await new Promise((resolve) => setTimeout(resolve, 400));
    return { value: input.value, view: document.querySelector('#app-shell').dataset.view };
  });
  suite.record('输入器：输入法组字中的回车不提交', composing.value === '组字保护' && composing.view === 'home', composing);

  await page.fill('#prompt-input', '换行');
  await page.keyboard.press('Shift+Enter');
  await page.waitForTimeout(150);
  const shiftEnter = await page.evaluate(() => ({
    value: document.querySelector('#prompt-input').value,
    view: document.querySelector('#app-shell').dataset.view,
  }));
  suite.record('输入器：Shift+Enter 换行不提交', shiftEnter.value === '换行\n' && shiftEnter.view === 'home', shiftEnter);
  await page.fill('#prompt-input', '');
  await page.waitForTimeout(150);
}
