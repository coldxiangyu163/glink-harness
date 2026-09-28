/**
 * Research records: the sidebar list and the home page's research/report lists.
 *
 * View only — it draws REST run records (`api.listRuns`) and reports clicks to
 * the owner (`onOpen(runId, options)`); navigation and state stay in
 * `workspace.mjs`. A follow-up chain is shown once, under its root's prompt;
 * a report row opens the chain's latest turn with that report in the slot.
 *
 * The list is a page, not the whole history: when the host says there is more
 * (`hasMore`), the sidebar ends with a «显示更多» entry that asks the owner for
 * the next page. The merge itself lives in `run-list-state.mjs`.
 */

import { formatDuration, relativeTime, researchTitle, statusLabel } from './present.mjs';
import { groupRunChains } from './run-list-state.mjs';

/* Only the two states that need the reader get a word in the sidebar. */
const NAV_META = { running: '进行中', failed: '未完成' };

function emptyState({ title, description, size = 'sm' }) {
  const wrap = document.createElement('div');
  wrap.className = 'empty-state';
  wrap.dataset.size = size;
  wrap.dataset.variant = 'no-data';
  wrap.setAttribute('role', 'status');
  const image = document.createElement('img');
  image.src = '/empty-state/no-data.svg';
  image.alt = '';
  const heading = document.createElement('p');
  heading.className = 'empty-state-title';
  heading.textContent = title;
  wrap.append(image, heading);
  if (description) {
    const text = document.createElement('p');
    text.className = 'empty-state-desc';
    text.textContent = description;
    wrap.append(text);
  }
  return wrap;
}

export function renderRunLists(
  { runList, homeRunList, homeReportList },
  runs,
  { currentRunId = null, onOpen, hasMore = false, loadingMore = false, onLoadMore = null } = {},
) {
  runList.replaceChildren();
  homeRunList.replaceChildren();
  homeReportList.replaceChildren();
  if (!runs.length) {
    runList.append(emptyState({ title: '还没有研究记录' }));
    homeRunList.append(emptyState({ title: '还没有研究', description: '在上方描述问题，开始第一项研究' }));
    homeReportList.append(emptyState({ title: '还没有报告', description: '研究完成后生成的报告会出现在这里' }));
    return;
  }
  const { rows, reports } = groupRunChains(runs);
  for (const { run, title } of rows) {
    runList.append(navRow(run, title, currentRunId, onOpen));
    homeRunList.append(homeRow(run, title, onOpen));
  }
  if (hasMore) runList.append(loadMoreRow({ onLoadMore, loading: loadingMore }));
  if (!reports.length) {
    homeReportList.append(emptyState({ title: '还没有报告', description: '研究完成后生成的报告会出现在这里' }));
  }
  for (const { run, latestRunId } of reports) homeReportList.append(reportRow(run, latestRunId, onOpen));
}

function iconNode(name) {
  const node = document.createElement('span');
  node.className = 'icon';
  node.dataset.icon = name;
  node.setAttribute('aria-hidden', 'true');
  return node;
}

function span(className, text) {
  const node = document.createElement('span');
  node.className = className;
  node.textContent = text;
  return node;
}

function loadMoreRow({ onLoadMore, loading }) {
  const button = document.createElement('button');
  button.type = 'button';
  button.id = 'run-list-more';
  // The same row shape as a research entry: the collapsed sidebar already
  // turns `.nav-title` into an accessible name and centers the icon.
  button.className = 'nav-item nav-more';
  button.append(iconNode('chevron-down'), span('nav-title', loading ? '正在加载…' : '显示更多'));
  if (loading) button.disabled = true;
  button.addEventListener('click', () => {
    if (loading || typeof onLoadMore !== 'function') return;
    onLoadMore();
  });
  return button;
}

function navRow(run, title, currentRunId, onOpen) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'nav-item';
  button.dataset.runId = run.runId;
  button.dataset.status = run.status;
  button.dataset.tip = researchTitle(title, 40);
  button.dataset.tipSide = 'right';
  button.dataset.tipWhen = 'collapsed';
  if (currentRunId === run.runId) button.setAttribute('aria-current', 'page');
  button.append(iconNode('chat'), span('nav-title', researchTitle(title)));
  if (NAV_META[run.status]) button.append(span('nav-meta', NAV_META[run.status]));
  button.addEventListener('click', () => onOpen(run.runId));
  return button;
}

function homeRow(run, title, onOpen) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'home-row';
  button.dataset.runId = run.runId;
  button.dataset.status = run.status;
  const body = span('home-row-body', '');
  const duration = formatDuration(run.durationSeconds);
  body.append(
    span('home-row-title', researchTitle(title, 40)),
    span('home-row-meta', duration ? `${statusLabel(run.status)} · ${duration}` : statusLabel(run.status)),
  );
  button.append(iconNode('history'), body, span('home-row-time', relativeTime(run.createdAt)));
  button.addEventListener('click', () => onOpen(run.runId));
  return button;
}

function reportRow(run, conversationRunId, onOpen) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'home-row';
  button.dataset.reportRunId = run.runId;
  const tile = document.createElement('img');
  tile.className = 'home-row-tile';
  tile.src = '/icons/markdown-32.png';
  tile.alt = '';
  const body = span('home-row-body', '');
  body.append(span('home-row-title', researchTitle(run.prompt, 40)), span('home-row-meta', 'report.md · 待核验草稿'));
  button.append(tile, body, span('home-row-time', relativeTime(run.finishedAt || run.createdAt)));
  button.addEventListener('click', () => onOpen(conversationRunId, { withReport: run.runId }));
  return button;
}
