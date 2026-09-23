/**
 * 进度与上下文 — the 340px floating panel of the standard library's
 * conversation page (规范 S-01：进度与上下文是受槽位互斥管着的临时浮层).
 *
 * It answers "what did the AI just do" without growing the conversation:
 *
 *   进度      one timeline row per step (tool call / thinking block)
 *   上下文    输出（this run's report）· 来源（what it searched and read on the web）
 *   运行信息  status, time, usage, model
 *
 * Run metadata lives here and nowhere else — the conversation column carries
 * the answer, not bookkeeping. Every word comes from `present.mjs`; nothing here
 * names the engine or the execution surface.
 */

import {
  describeTool,
  formatDuration,
  formatTokens,
  modelLabel,
  statusLabel,
} from './present.mjs';

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function icon(name, size) {
  const node = el('span', 'icon');
  node.dataset.icon = name;
  node.setAttribute('aria-hidden', 'true');
  if (size) node.style.setProperty('--icon-size', `${size}px`);
  return node;
}

const WEB_SEARCH_RE = /^(web_?search|search_web|websearch)$/i;
const WEB_READ_RE = /fetch|browse|webfetch|scrape/i;

/** Queries and pages the run touched on the web, in first-seen order. */
export function collectSources(items) {
  const queries = [];
  const pages = [];
  const seen = new Set();
  for (const item of items) {
    if (item.kind !== 'tool') continue;
    const args = item.args && typeof item.args === 'object' ? item.args : {};
    if (WEB_SEARCH_RE.test(item.name)) {
      const values = [args.query, ...(Array.isArray(args.queries) ? args.queries : [args.queries])];
      for (const value of values) {
        if (typeof value !== 'string' || !value.trim() || seen.has(`q:${value}`)) continue;
        seen.add(`q:${value}`);
        queries.push(value.trim());
      }
    } else if (WEB_READ_RE.test(item.name)) {
      const values = Array.isArray(args.urls) ? args.urls : [args.url];
      for (const value of values) {
        if (typeof value !== 'string' || seen.has(`u:${value}`)) continue;
        try {
          const url = new URL(value);
          if (url.protocol !== 'http:' && url.protocol !== 'https:') continue;
          seen.add(`u:${value}`);
          pages.push({ href: url.href, host: url.hostname, path: url.pathname === '/' ? '' : url.pathname });
        } catch {
          /* not a URL — nothing to link to */
        }
      }
    }
  }
  return { queries, pages };
}

export function createProgressPanel({ root, onOpenReport = null }) {
  const card = el('div', 'progress-card');

  // 进度
  const stepsSection = el('section', 'progress-section');
  const stepsHead = sectionHead('进度', 'progress-steps-list');
  const stepsMeta = stepsHead.querySelector('.progress-section-meta');
  const steps = el('ol', 'progress-steps');
  steps.id = 'progress-steps-list';
  const stepsEmpty = el('p', 'progress-empty', '还没有步骤。开始研究后，每一步都会出现在这里。');
  stepsSection.append(stepsHead, steps, stepsEmpty);

  // 上下文
  const contextSection = el('section', 'progress-section');
  const contextHead = sectionHead('上下文', 'progress-context');
  const context = el('div', 'progress-context');
  context.id = 'progress-context';
  const tabs = el('div', 'segmented');
  tabs.setAttribute('role', 'tablist');
  tabs.setAttribute('aria-label', '上下文类型');
  const outputTab = tab('输出', 'progress-outputs', true);
  const sourceTab = tab('来源', 'progress-sources', false);
  tabs.append(outputTab, sourceTab);
  const outputs = el('div', 'progress-tabpanel');
  outputs.id = 'progress-outputs';
  outputs.setAttribute('role', 'tabpanel');
  const sources = el('div', 'progress-tabpanel');
  sources.id = 'progress-sources';
  sources.setAttribute('role', 'tabpanel');
  sources.hidden = true;
  context.append(tabs, outputs, sources);
  contextSection.append(contextHead, context);

  // 运行信息
  const facts = el('dl', 'progress-facts');

  card.append(stepsSection, contextSection, facts);
  root.replaceChildren(card);

  let latest = null;
  let modelName = '';

  function sectionHead(title, controls) {
    const head = el('button', 'progress-section-head');
    head.type = 'button';
    head.setAttribute('aria-expanded', 'true');
    head.setAttribute('aria-controls', controls);
    const chevron = icon('chevron-down', 12);
    chevron.classList.add('progress-chevron');
    const meta = el('span', 'progress-section-meta');
    head.append(el('span', 'progress-section-title', title), meta, chevron);
    head.addEventListener('click', () => {
      const open = head.getAttribute('aria-expanded') !== 'true';
      head.setAttribute('aria-expanded', String(open));
      head.parentElement.dataset.collapsed = String(!open);
    });
    return head;
  }

  function tab(label, controls, selected) {
    const button = el('button', 'segmented-item', label);
    button.type = 'button';
    button.setAttribute('role', 'tab');
    button.setAttribute('aria-controls', controls);
    button.setAttribute('aria-selected', String(selected));
    button.tabIndex = selected ? 0 : -1;
    button.addEventListener('click', () => select(button));
    button.addEventListener('keydown', (event) => {
      if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
      event.preventDefault();
      const other = button === outputTab ? sourceTab : outputTab;
      select(other);
      other.focus();
    });
    return button;
  }

  function select(button) {
    for (const item of [outputTab, sourceTab]) {
      const on = item === button;
      item.setAttribute('aria-selected', String(on));
      item.tabIndex = on ? 0 : -1;
      document.getElementById(item.getAttribute('aria-controls')).hidden = !on;
    }
  }

  function drawSteps(model) {
    const rows = model.items.filter((item) => item.kind === 'tool' || item.kind === 'thinking');
    const running = model.status === 'running';
    stepsMeta.textContent = rows.length
      ? running ? `第 ${rows.length} 步` : `共 ${rows.length} 步`
      : '';
    stepsEmpty.hidden = rows.length > 0;
    steps.replaceChildren(
      ...rows.map((item) => {
        const li = el('li', 'progress-step');
        let status = 'done';
        if (item.kind === 'tool') {
          if (item.status === 'running') status = 'running';
          else if (item.status === 'error' || item.isError) status = 'failed';
          else if (item.status === 'interrupted' || item.status === 'cancelled') status = 'stopped';
        } else if (!item.closed) {
          status = 'running';
        }
        li.dataset.status = status;
        const marker = el('span', 'progress-marker');
        if (status === 'done') marker.append(icon('check', 12));
        if (status === 'failed') marker.append(icon('close', 12));
        const srStatus = { done: '已完成', failed: '失败', running: '进行中', stopped: '未完成' }[status];
        let label;
        if (item.kind === 'thinking') {
          label = status === 'running' ? '正在思考' : '思考';
        } else {
          const described = describeTool(item.name, item.args);
          label = described.object ? `${described.action} · ${described.object}` : described.action;
        }
        const text = el('span', 'progress-step-text', label);
        li.append(marker, text, el('span', 'sr-only', `，${srStatus}`));
        return li;
      }),
    );
  }

  function fileRow({ name, meta, onOpen }) {
    const row = el('button', 'progress-file');
    row.type = 'button';
    row.setAttribute('aria-label', `在右侧打开 ${name}`);
    const tile = document.createElement('img');
    tile.src = '/icons/markdown-32.png';
    tile.alt = '';
    tile.width = 32;
    tile.height = 32;
    const text = el('span', 'progress-file-text');
    text.append(el('span', 'progress-file-name', name), el('span', 'progress-file-meta', meta));
    row.append(tile, text);
    row.addEventListener('click', onOpen);
    return row;
  }

  function drawOutputs(model) {
    const count = model.hasReport ? 1 : 0;
    const head = el('p', 'progress-tab-meta', `${count} 个文件`);
    const nodes = [head];
    if (model.hasReport) {
      nodes.push(fileRow({
        name: 'report.md',
        meta: 'Markdown 文档 · 待核验草稿',
        onOpen: () => onOpenReport?.(model.runId),
      }));
    } else {
      nodes.push(el('p', 'progress-empty', model.status === 'running'
        ? '这项研究还没有生成文件，生成后会出现在这里。'
        : '这项研究没有生成文件。'));
    }
    outputs.replaceChildren(...nodes);
  }

  function drawSources(model) {
    const { queries, pages } = collectSources(model.items);
    const parts = [];
    if (queries.length) parts.push(`${queries.length} 次检索`);
    if (pages.length) parts.push(`${pages.length} 个网页`);
    const nodes = [el('p', 'progress-tab-meta', parts.join(' · ') || '没有网络来源')];
    if (!queries.length && !pages.length) {
      nodes.push(el('p', 'progress-empty', '这项研究没有检索网络或读取网页。'));
    }
    if (queries.length) {
      const list = el('ul', 'progress-source-list');
      for (const query of queries) {
        const li = el('li', 'progress-source');
        li.append(icon('search', 14), el('span', 'progress-source-text', query));
        list.append(li);
      }
      nodes.push(list);
    }
    if (pages.length) {
      const list = el('ul', 'progress-source-list');
      for (const page of pages) {
        const li = el('li', 'progress-source');
        const link = el('a', 'progress-source-link');
        link.href = page.href;
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
        link.append(icon('globe', 14), el('span', 'progress-source-text', `${page.host}${page.path}`));
        li.append(link);
        list.append(li);
      }
      nodes.push(list);
    }
    sources.replaceChildren(...nodes);
  }

  function drawFacts(model) {
    const rows = [['状态', statusLabel(model.status)]];
    const duration = formatDuration(model.durationSeconds);
    if (duration) rows.push(['用时', duration]);
    const tokens = formatTokens(model.usage?.totalTokens);
    if (tokens) rows.push(['用量', `${tokens} token`]);
    const name = modelLabel(model.model || modelName);
    if (name) rows.push(['模型', name]);
    facts.replaceChildren(
      ...rows.flatMap(([term, value]) => [el('dt', '', term), el('dd', '', value)]),
    );
  }

  return {
    render(model, options = {}) {
      latest = model;
      if (options.modelName !== undefined) modelName = options.modelName;
      drawSteps(model);
      drawOutputs(model);
      drawSources(model);
      drawFacts(model);
    },
    get model() {
      return latest;
    },
  };
}
