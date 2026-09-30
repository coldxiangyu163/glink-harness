/**
 * 进度与上下文 — the 340px floating panel of the standard library's
 * conversation page (规范 S-01：进度与上下文是受槽位互斥管着的临时浮层).
 *
 * It answers "what did the AI just do" without growing the conversation:
 *
 *   进度      latest whole task plan, otherwise actual tool/thinking steps
 *   上下文    输出（this run's report）· 来源（what it searched and read on the web）
 *   运行信息  status, time, usage, model
 *
 * Run metadata lives here and nowhere else — the conversation column carries
 * the answer, not bookkeeping. Every word comes from `present.mjs`; nothing here
 * names the engine or the execution surface.
 */

import {
  describeTool,
  describeChild,
  stageText,
  fetchedTitle,
  formatDuration,
  formatTokens,
  modelLabel,
  statusLabel,
  planProgressLabel,
} from './present.mjs';
import { createProgressMarker, createTodoTimeline, isTodoWrite, latestTodoEntries } from './todo-panel.mjs';

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
  // The search result already names its sources. Match on origin + pathname:
  // fetch URLs often omit the search provider's tracking query/fragment.
  const titles = new Map();
  for (const item of items) {
    if (item.kind !== 'tool' || !(item.presentation === 'search_web' ||
        (!item.presentation && WEB_SEARCH_RE.test(item.name)))) continue;
    for (const match of String(item.result ?? '').matchAll(/^- \[([^\]\n]{3,140})\]\((https?:\/\/[^\s)]+)\)/gm)) {
      try {
        const url = new URL(match[2]);
        titles.set(`${url.origin}${url.pathname}`, match[1].trim());
      } catch { /* malformed link is not a source */ }
    }
  }
  for (const item of items) {
    if (item.kind !== 'tool') continue;
    const args = item.args && typeof item.args === 'object' ? item.args : {};
    if (item.presentation === 'search_web' || (!item.presentation && WEB_SEARCH_RE.test(item.name))) {
      const values = [args.query, ...(Array.isArray(args.queries) ? args.queries : [args.queries])];
      for (const value of values) {
        if (typeof value !== 'string' || !value.trim() || seen.has(`q:${value}`)) continue;
        seen.add(`q:${value}`);
        queries.push(value.trim());
      }
    } else if (item.presentation === 'fetch_page' || (!item.presentation && WEB_READ_RE.test(item.name))) {
      const values = Array.isArray(args.urls) ? args.urls : [args.url];
      for (const value of values) {
        if (typeof value !== 'string' || seen.has(`u:${value}`)) continue;
        try {
          const url = new URL(value);
          if (url.protocol !== 'http:' && url.protocol !== 'https:') continue;
          seen.add(`u:${value}`);
          const page = { href: url.href, host: url.hostname, path: url.pathname === '/' ? '' : url.pathname };
          const fetched = /^Fetched https?:\/\/[^\n]+ \(HTTP 2\d\d\)/.test(String(item.result ?? ''));
          const title = fetched ? fetchedTitle(item.result) || titles.get(`${url.origin}${url.pathname}`) : '';
          pages.push(title ? { ...page, title } : page);
        } catch {
          /* not a URL — nothing to link to */
        }
      }
    }
  }
  return { queries, pages };
}

export function createProgressPanel({ root, onOpenReport = null, onClose = null }) {
  const card = el('div', 'progress-card');

  // 进度：有整表计划时用任务时间轴，无源时保留实际执行步骤。
  const stepsSection = el('section', 'progress-section progress-section-steps');
  const stepsBody = el('div', 'progress-steps-body');
  stepsBody.id = 'progress-steps-body';
  const stepsMeta = el('span', 'progress-section-meta');
  const stepsHead = sectionHead('进度', stepsBody, stepsMeta);
  stepsMeta.setAttribute('role', 'status');
  stepsMeta.setAttribute('aria-live', 'polite');
  const todo = createTodoTimeline();
  const steps = el('ol', 'progress-steps');
  steps.id = 'progress-steps-list';
  const stepsEmpty = el('p', 'progress-empty', '还没有步骤。开始研究后，每一步都会出现在这里。');
  stepsBody.append(todo.node, steps, stepsEmpty);
  stepsSection.append(stepsHead, stepsBody);

  // 上下文
  const contextSection = el('section', 'progress-section');
  const context = el('div', 'progress-context');
  context.id = 'progress-context';
  const contextHead = sectionHead('上下文', context);
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
  root.classList.toggle('has-close', Boolean(onClose));
  if (onClose) {
    const close = el('button', 'progress-close');
    close.type = 'button';
    close.setAttribute('aria-label', '关闭进度与上下文');
    close.dataset.tip = '关闭进度与上下文';
    close.append(icon('close', 16));
    close.addEventListener('click', onClose);
    root.append(close);
  }

  let latest = null;
  let modelName = '';

  function sectionHead(title, content, meta = el('span', 'progress-section-meta')) {
    const head = el('button', 'progress-section-head');
    head.type = 'button';
    head.setAttribute('aria-expanded', 'true');
    head.setAttribute('aria-label', `收起${title}`);
    head.setAttribute('aria-controls', content.id);
    const chevron = icon('chevron-down', 12);
    chevron.classList.add('progress-chevron');
    head.append(el('span', 'progress-section-title', title), meta, chevron);
    head.addEventListener('click', () => {
      const open = head.getAttribute('aria-expanded') !== 'true';
      head.setAttribute('aria-expanded', String(open));
      head.setAttribute('aria-label', `${open ? '收起' : '展开'}${title}`);
      content.hidden = !open;
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
    const running = model.status === 'running';
    card.dataset.status = model.status;
    const entries = latestTodoEntries(model.items);
    todo.render(entries, running);
    const rows = entries.length ? [] : model.items.filter((item) =>
      (item.kind === 'tool' && !isTodoWrite(item)) || item.kind === 'thinking' ||
      item.kind === 'subagent' || (item.kind === 'stage' && item.stage === 'plan'));
    stepsMeta.textContent = entries.length ? planProgressLabel(entries, running)
      : rows.length ? running ? `第 ${rows.length} 步` : `共 ${rows.length} 步` : '';
    steps.hidden = entries.length > 0 || rows.length === 0;
    stepsEmpty.hidden = entries.length > 0 || rows.length > 0;
    steps.replaceChildren(
      ...rows.map((item) => {
        const li = el('li', 'progress-step');
        let status = 'done';
        if (item.kind === 'tool' || item.kind === 'subagent') {
          if (item.status === 'running') status = 'running';
          else if (item.status === 'error' || item.isError) status = 'failed';
          else if (item.status === 'interrupted' || item.status === 'cancelled') status = 'stopped';
        } else if (item.kind === 'thinking' && !item.closed) {
          status = 'running';
        }
        li.dataset.status = status;
        const srStatus = { done: '已完成', failed: '失败', running: '进行中', stopped: '未完成' }[status];
        let label;
        if (item.kind === 'thinking') {
          label = status === 'running' ? '正在思考' : '思考';
        } else if (item.kind === 'subagent') {
          const described = describeChild(item);
          label = `${described.action} · ${described.object}`;
        } else if (item.kind === 'stage') {
          label = stageText(item);
        } else {
          const described = describeTool(item.name, item.args, item.presentation);
          label = described.object ? `${described.action} · ${described.object}` : described.action;
        }
        const text = el('span', 'progress-step-text', label);
        const row = el('div', 'progress-step-row');
        row.append(createProgressMarker(status, label), text, el('span', 'sr-only', `，${srStatus}`));
        li.append(row);
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
        link.append(icon('globe', 14), el('span', 'progress-source-text',
          page.title ? `${page.title} · ${page.host}${page.path}` : `${page.host}${page.path}`));
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
