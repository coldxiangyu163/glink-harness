/**
 * DOM rendering for one run.
 *
 * Shape follows the standard library's AI execution surfaces:
 *
 *   ┌ 过程折叠（Thinking · steps）  正在检索 / 已完成 · N 个步骤 · 用时 …
 *   │   思考过程 ⌄            ← one compact row per thinking block
 *   │   检索  跨境资金监管 ⌄  ← one compact row per tool call (ToolChips)
 *   └   …
 *   研究助理 [待核验草稿]      ← the answer frame (规范 6.1)
 *   回答正文
 *   已生成 1 个文件 · report.md
 *   复制 · 下载 · 重新研究
 *
 * The process fold follows the library's contract: open while the run works,
 * folds itself when the run ends, and the reader's own choice wins from then on.
 * Rendering is incremental — each model item owns one node that is only touched
 * when its value changes; the final answer block is the one node that moves
 * between the fold and the answer frame.
 *
 * The view reads nothing but the model it is given: no fetch, no timers that
 * outlive `dispose`. Every word it prints comes from `present.mjs`, so engine
 * vocabulary (tool names, stage codes, host paths) never reaches the screen.
 * Class names keep the `glink-*` product prefix; the browser regressions
 * assert on them, so rename only together with those assertions.
 */

import {
  childCardState,
  childFacts,
  childLabel,
  childStatusText,
  childrenPanelTitle,
  describeChild,
  describeTool,
  displayPath,
  fetchedTitle,
  planEntries,
  planEntryLabel,
  planUpdateLabel,
  formatDuration,
  noticeText,
  stageText,
  toolStateLabel,
} from './present.mjs';
import { childrenPanel } from './run-reducer.mjs';
import { isTodoWrite } from './todo-panel.mjs';

let viewCounter = 0;

function icon(name, size) {
  const node = document.createElement('span');
  node.className = 'icon';
  node.dataset.icon = name;
  node.setAttribute('aria-hidden', 'true');
  if (size) node.style.setProperty('--icon-size', `${size}px`);
  return node;
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

export function createRunView({
  root,
  renderMarkdown = (text) => text,
  onOpenReport = null,
  onRerun = null,
  onRetry = null,
  onCopy = null,
  reportHref = () => '#',
  reportDownloadName = () => 'report.md',
  childFileHref = () => '#',
  deliverableHref = () => '#',
}) {
  const uid = `run-view-${(viewCounter += 1)}`;

  // ── process fold ─────────────────────────────────────────────────────────
  const process = el('section', 'glink-process');
  process.setAttribute('aria-label', '研究过程');
  const toggle = el('button', 'glink-process-toggle trace-toggle');
  toggle.type = 'button';
  toggle.setAttribute('aria-controls', `${uid}-process`);
  const toggleIcon = icon('sparkles', 16);
  toggleIcon.classList.add('glink-process-icon');
  const toggleLabel = el('span', 'glink-process-label');
  toggleLabel.setAttribute('role', 'status');
  toggleLabel.setAttribute('aria-live', 'polite');
  const toggleChevron = icon('chevron-down', 14);
  toggleChevron.classList.add('glink-process-chevron');
  toggle.append(toggleIcon, toggleLabel, toggleChevron);
  const body = el('div', 'glink-process-body');
  body.id = `${uid}-process`;
  // body (0fr ↔ 1fr) › inner (overflow clip, no box) › track (timeline rule + padding):
  // any padding on the clipped child itself would survive a 0fr track.
  const inner = el('div', 'glink-process-inner');
  const track = el('div', 'glink-process-track');
  const list = el('div', 'glink-process-list');
  const pending = el('div', 'glink-pending');
  pending.append(el('span', 'glink-pending-dot'), el('span', 'glink-pending-label'));
  pending.hidden = true;
  track.append(list, pending);
  inner.append(track);
  body.append(inner);
  process.append(toggle, body);

  // ── answer frame ─────────────────────────────────────────────────────────
  const answer = el('div', 'glink-answer');
  const answerHead = el('header', 'glink-answer-head');
  answerHead.hidden = true;
  const headRow = el('div', 'glink-answer-title');
  headRow.append(icon('agent', 20), el('h3', '', '研究助理'));
  const draftBadge = el('span', 'badge', '待核验草稿');
  draftBadge.dataset.variant = 'warning';
  headRow.append(draftBadge);
  const disclaimer = el(
    'p',
    'glink-answer-note',
    'AI 生成，未经人工核验：可作为起草参考，不能直接对外发送或据此下判断。',
  );
  answerHead.append(headRow, disclaimer);
  const answerBody = el('div', 'glink-answer-body');

  const output = el('section', 'glink-output');
  output.setAttribute('aria-label', '生成的文件');
  output.hidden = true;
  const outputTitle = el('p', 'glink-output-title', '已生成 1 个文件');
  const outputRow = el('div', 'glink-output-row');
  const outputOpen = el('button', 'glink-output-open glink-report-button');
  outputOpen.type = 'button';
  outputOpen.setAttribute('aria-label', '在右侧打开 report.md');
  const fileTile = document.createElement('img');
  fileTile.className = 'glink-file-tile';
  fileTile.src = '/icons/markdown-32.png';
  fileTile.alt = '';
  fileTile.width = 32;
  fileTile.height = 32;
  const fileText = el('span', 'glink-output-text');
  fileText.append(el('span', 'glink-output-name', 'report.md'), el('span', 'glink-output-meta', 'Markdown 文档 · 待核验草稿'));
  outputOpen.append(fileTile, fileText);
  outputOpen.addEventListener('click', () => {
    if (latest?.hasReport && onOpenReport) onOpenReport(latest.runId);
  });
  const outputDownload = el('a', 'icon-key');
  outputDownload.setAttribute('aria-label', '下载 report.md');
  outputDownload.dataset.tip = '下载';
  outputDownload.append(icon('download', 16));
  outputRow.append(outputOpen, outputDownload);
  output.append(outputTitle, outputRow);

  const deliveries = el('section', 'glink-output glink-deliverables');
  deliveries.setAttribute('aria-label', '交付文件');
  deliveries.hidden = true;
  const deliveryTitle = el('p', 'glink-output-title');
  const deliveryList = el('div', 'glink-deliverables-list');
  deliveries.append(deliveryTitle, deliveryList);

  // ── children panel (ChildRun, §3.5) ────────────────────────────────────────
  // A run with dispatched subagents lists them in the detail area; a run
  // without any never renders an empty panel. The rows reuse the process-row
  // geometry (glink-step-row) and the output section's chrome — no new styles.
  const childrenSection = el('section', 'glink-output glink-children');
  childrenSection.setAttribute('aria-label', '子代理');
  childrenSection.hidden = true;
  const childrenTitle = el('p', 'glink-output-title');
  const childrenList = el('div', 'glink-children-list');
  childrenSection.append(childrenTitle, childrenList);

  const outcome = el('section', 'glink-outcome');
  outcome.hidden = true;

  const actions = el('div', 'glink-answer-actions');
  actions.setAttribute('role', 'group');
  actions.setAttribute('aria-label', '回答操作');
  actions.hidden = true;
  const copyButton = el('button', 'icon-key');
  copyButton.type = 'button';
  copyButton.setAttribute('aria-label', '复制回答');
  copyButton.dataset.tip = '复制回答';
  copyButton.append(icon('copy', 16));
  copyButton.addEventListener('click', () => onCopy?.(finalText()));
  const downloadButton = el('a', 'icon-key');
  downloadButton.setAttribute('aria-label', '下载报告');
  downloadButton.dataset.tip = '下载报告';
  downloadButton.append(icon('download', 16));
  const rerunButton = el('button', 'icon-key');
  rerunButton.type = 'button';
  rerunButton.setAttribute('aria-label', '用同一个问题重新研究');
  rerunButton.dataset.tip = '重新研究';
  rerunButton.append(icon('refresh', 16));
  rerunButton.addEventListener('click', () => onRerun?.(latest?.prompt ?? ''));
  actions.append(copyButton, downloadButton, rerunButton);

  answer.append(answerHead, answerBody, childrenSection, output, deliveries, outcome, actions);
  // Task plans belong to ProgressPanel, not a dock above the conversation or
  // a second card inside past turns. The conversation retains plan-update rows.
  root.replaceChildren(process, answer);

  // Answers often point at the report by its relative name ("见 report.md"); the
  // browser would navigate to a host path that does not exist. Open the slot instead.
  answer.addEventListener('click', (event) => {
    const link = event.target.closest?.('a[href]');
    if (!link || !answerBody.contains(link)) return;
    const href = link.getAttribute('href') ?? '';
    if (/^(?:\.\/)?report\.md$/i.test(href) && latest?.hasReport) {
      event.preventDefault();
      onOpenReport?.(latest.runId);
    }
  });

  /** @type {Array<{item: object, node: HTMLElement}>} */
  let rendered = [];
  let answerIndex = -1;
  let manualOpen = null;
  let connection = null;
  let outcomeKey = '';
  let frame = null;
  let latest = null;
  let disposed = false;

  toggle.addEventListener('click', () => {
    manualOpen = !isOpen();
    syncFold();
  });

  function isOpen() {
    return manualOpen ?? latest?.status === 'running';
  }

  function syncFold() {
    const open = isOpen();
    toggle.setAttribute('aria-expanded', String(open));
    process.dataset.open = String(open);
  }

  function schedule(model) {
    latest = model;
    if (frame !== null || disposed) return;
    frame = requestAnimationFrame(() => {
      frame = null;
      if (!disposed && latest) draw(latest);
    });
  }

  function draw(model) {
    root.dataset.status = model.status;
    drawItems(model);
    drawProcessHead(model);
    drawAnswer(model);
    drawChildren(model);
    syncFold();
  }

  // ── children panel ─────────────────────────────────────────────────────────

  function drawChildren(model) {
    const rows = childrenPanel(model);
    childrenSection.hidden = rows.length === 0;
    if (!rows.length) {
      if (childrenList.children?.length) childrenList.replaceChildren();
      return;
    }
    const title = childrenPanelTitle(rows.length);
    if (childrenTitle.textContent !== title) childrenTitle.textContent = title;
    childrenList.replaceChildren(
      ...rows.map((child) => {
        const row = el('div', 'glink-stage glink-step-row glink-child-row');
        row.dataset.status = child.status;
        row.append(
          icon('agent', 14),
          el('span', 'glink-stage-text', childLabel(child)),
          el('span', 'glink-tool-state', childStatusText(child.status)),
        );
        // R2 增强：耗时/工具步数（来自 _meta.json 白名单）与产物下载链接。
        const facts = childFacts(child);
        if (facts) row.append(el('span', 'glink-child-facts', facts));
        if (Array.isArray(child.artifacts) && child.artifacts.length && model.runId) {
          const links = el('span', 'glink-child-artifacts');
          for (const artifact of child.artifacts) {
            if (!artifact || typeof artifact.name !== 'string' || !artifact.name) continue;
            const link = el('a', 'glink-child-artifact', artifact.name);
            link.href = childFileHref(model.runId, child.n, artifact.name);
            link.target = '_blank';
            link.rel = 'noopener';
            links.append(link);
          }
          if (links.children.length) row.append(links);
        }
        return row;
      }),
    );
  }

  // ── process ──────────────────────────────────────────────────────────────

  function stepCount(model) {
    return model.items.filter(
      (item) => item.kind === 'tool' || item.kind === 'thinking' || item.kind === 'subagent',
    ).length;
  }

  function activity(model) {
    const last = [...model.items].reverse().find((item) => item.kind !== 'turn' && item.kind !== 'stage');
    if (!last) return { label: '正在准备研究', live: false };
    if (last.kind === 'tool' && last.status === 'running') {
      return { label: describeTool(last.name, last.args, last.presentation).active, live: true };
    }
    if (last.kind === 'subagent' && last.status === 'running') {
      return { label: describeChild(last).active, live: true };
    }
    if (last.kind === 'thinking' && !last.closed) return { label: '正在思考', live: true };
    if (last.kind === 'text' && !last.closed) return { label: '正在撰写回答', live: true };
    return { label: '正在处理', live: false };
  }

  function drawProcessHead(model) {
    const steps = stepCount(model);
    const running = model.status === 'running';
    process.dataset.status = model.status;
    process.hidden = !running && steps === 0;
    let label;
    if (running) {
      label = activity(model).label;
    } else {
      const parts = [{ finished: '已完成', cancelled: '已停止', failed: '未完成' }[model.status] ?? '已结束'];
      if (steps) parts.push(`${steps} 个步骤`);
      const duration = formatDuration(model.durationSeconds);
      if (duration && model.status === 'finished') parts.push(`用时 ${duration}`);
      label = parts.join(' · ');
    }
    if (toggleLabel.textContent !== label) toggleLabel.textContent = label;
    toggle.setAttribute('aria-label', `研究过程：${label}`);

    const act = running ? activity(model) : null;
    pending.hidden = !running || act.live;
    if (!pending.hidden) pending.querySelector('.glink-pending-label').textContent = act.label;
  }

  function finalAnswerIndex(items) {
    for (let index = items.length - 1; index >= 0; index -= 1) {
      const item = items[index];
      if (item.kind === 'tool' || item.kind === 'thinking' || item.kind === 'subagent') return -1;
      if (item.kind === 'text' && item.text.trim()) return index;
    }
    return -1;
  }

  function drawItems(model) {
    const items = model.items;
    // Drop nodes whose item disappeared (a replayed stream can rewrite the tail).
    if (rendered.length > items.length) {
      for (const { node } of rendered.slice(items.length)) node.remove();
      rendered = rendered.slice(0, items.length);
    }
    const nextAnswer = finalAnswerIndex(items);
    items.forEach((item, index) => {
      const entry = rendered[index];
      if (!entry) {
        const node = createNode(item);
        rendered[index] = { item, node };
        update(node, item);
        (index === nextAnswer ? answerBody : list).append(node);
        return;
      }
      if (entry.item !== item) {
        entry.item = item;
        update(entry.node, item);
      }
    });
    if (nextAnswer !== answerIndex) {
      // The final-answer role moved (a later step arrived, or a new last block
      // appeared): re-seat the nodes in stream order. Rare, so a full pass is fine.
      rendered.forEach(({ node }, index) => {
        if (index !== nextAnswer) list.append(node);
      });
      if (nextAnswer >= 0) answerBody.append(rendered[nextAnswer].node);
      answerIndex = nextAnswer;
    }
    rendered.forEach(({ node }, index) => {
      if (node.classList.contains('glink-text')) node.classList.toggle('glink-narration', index !== answerIndex);
    });
  }

  function createNode(item) {
    switch (item.kind) {
      case 'thinking': {
        const details = el('details', 'glink-thinking');
        const summary = el('summary', 'glink-step-row trace-toggle');
        summary.append(icon('sparkles', 14), el('span', 'glink-thinking-label'), chevron());
        details.append(summary, el('div', 'glink-thinking-body'));
        return details;
      }
      case 'tool': {
        if (isTodoWrite(item)) {
          const row = el('div', 'glink-stage glink-step-row glink-plan-update');
          row.append(icon('checklist', 14), el('span', 'glink-stage-text'));
          return row;
        }
        const card = el('details', 'glink-tool');
        const head = el('summary', 'glink-tool-head glink-step-row trace-toggle');
        const args = el('span', 'glink-tool-args');
        head.append(icon('wrench', 14), el('span', 'glink-tool-name'), args, el('span', 'glink-tool-state'), chevron('glink-tool-chevron'));
        head.addEventListener('click', (event) => {
          if (card.dataset.hasDetail !== 'true') {
            event.preventDefault();
            return;
          }
          // The reader's own choice outranks the streaming default from now on.
          card.dataset.touched = 'true';
        });
        const detail = el('div', 'glink-tool-detail');
        const wrap = el('div', 'glink-tool-detail-wrap');
        wrap.append(el('div', 'glink-tool-result glink-tool-structured'),
          el('pre', 'glink-tool-output scroll-area'), el('div', 'glink-tool-result'));
        detail.append(wrap);
        card.append(head, detail);
        return card;
      }
      case 'subagent': {
        // The dispatch card reuses the tool card's component and styles: one
        // compact row while the child runs, a folded result preview once it
        // settles. The child run's own step stream never enters the timeline
        // (§3.5: 子代理正文永不进事件日志).
        const card = el('details', 'glink-tool glink-subagent');
        const head = el('summary', 'glink-tool-head glink-step-row trace-toggle');
        const args = el('span', 'glink-tool-args');
        head.append(icon('agent', 14), el('span', 'glink-tool-name'), args, el('span', 'glink-tool-state'), chevron('glink-tool-chevron'));
        head.addEventListener('click', (event) => {
          if (card.dataset.hasDetail !== 'true') {
            event.preventDefault();
            return;
          }
          card.dataset.touched = 'true';
        });
        const detail = el('div', 'glink-tool-detail');
        const wrap = el('div', 'glink-tool-detail-wrap');
        wrap.append(el('div', 'glink-tool-result'));
        detail.append(wrap);
        card.append(head, detail);
        return card;
      }
      case 'text':
        return el('div', 'glink-text');
      case 'notice': {
        const notice = el('details', 'glink-notice');
        const summary = el('summary', 'glink-step-row trace-toggle');
        summary.append(icon('alert-circle', 14), el('span', 'glink-notice-text'), chevron());
        summary.addEventListener('click', (event) => {
          if (notice.dataset.hasDetail !== 'true') event.preventDefault();
        });
        notice.append(summary, el('div', 'glink-notice-detail'));
        return notice;
      }
      case 'stage': {
        const stage = el('div', 'glink-stage glink-step-row');
        stage.append(icon('refresh', 14), el('span', 'glink-stage-text'));
        return stage;
      }
      case 'turn':
      default: {
        // Turns are bookkeeping; the node exists only to keep index ↔ item aligned.
        const turn = el('div', 'glink-turn');
        turn.hidden = true;
        return turn;
      }
    }
  }

  function chevron(extra = '') {
    const node = icon('chevron-down', 12);
    node.classList.add('glink-row-chevron');
    if (extra) node.classList.add(extra);
    return node;
  }

  function update(node, item) {
    switch (item.kind) {
      case 'thinking': {
        node.dataset.status = item.closed ? 'done' : 'running';
        const label = node.querySelector('.glink-thinking-label');
        const next = item.closed ? '思考过程' : '正在思考';
        if (label.textContent !== next) label.textContent = next;
        const text = node.querySelector('.glink-thinking-body');
        if (text.textContent !== item.text) text.textContent = item.text;
        return;
      }
      case 'tool': {
        if (isTodoWrite(item)) {
          node.dataset.status = item.status;
          node.querySelector('.glink-stage-text').textContent = planUpdateLabel(item);
          return;
        }
        node.dataset.status = item.status;
        const described = describeTool(item.name, item.args, item.presentation);
        const iconNode = node.querySelector('.glink-tool-head .icon');
        if (iconNode.dataset.icon !== described.icon) iconNode.dataset.icon = described.icon;
        const nameNode = node.querySelector('.glink-tool-name');
        if (nameNode.textContent !== described.action) nameNode.textContent = described.action;
        const argsNode = node.querySelector('.glink-tool-args');
        const title = item.presentation === 'fetch_page' ? fetchedTitle(item.result) : '';
        const objectText = title ? `${title} · ${described.object}` : described.object;
        if (argsNode.textContent !== objectText) argsNode.textContent = objectText;
        argsNode.classList.toggle('is-mono', described.mono);
        const structured = node.querySelector('.glink-tool-structured');
        const queries = item.presentation === 'search_web' && Array.isArray(item.args?.queries)
          ? item.args.queries.filter((query) => typeof query === 'string' && query.trim()) : [];
        const plans = item.presentation === 'plan' ? planEntries(item.args) : [];
        const structuredText = queries.length ? queries.map((query) => `检索 · ${query}`).join('\n')
          : plans.map(planEntryLabel).join('\n');
        if (structured.textContent !== structuredText) structured.textContent = structuredText;
        structured.hidden = !structuredText;
        node.querySelector('.glink-tool-state').textContent = toolStateLabel(item);
        const outputNode = node.querySelector('.glink-tool-output');
        const outputText = displayPath(item.output);
        if (outputNode.textContent !== outputText) outputNode.textContent = outputText;
        outputNode.hidden = outputText === '';
        const resultNode = node.querySelector('.glink-tool-detail-wrap > .glink-tool-result:not(.glink-tool-structured)');
        const resultText = displayPath(item.result || '');
        resultNode.hidden = resultText === '';
        if (resultNode.textContent !== resultText) resultNode.textContent = resultText;
        // Live output is the point while a call runs; once it settles the row goes
        // back to a chip — unless the reader opened it themselves.
        const hasDetail = structuredText !== '' || outputText !== '' || resultText !== '';
        node.dataset.hasDetail = String(hasDetail);
        node.querySelector('.glink-tool-chevron').hidden = !hasDetail;
        if (!hasDetail) node.open = false;
        else if (node.dataset.touched !== 'true') node.open = item.status === 'running';
        return;
      }
      case 'subagent': {
        // Child statuses map onto the tool card's own status vocabulary, so
        // the running pulse, the error tint and the state words come from the
        // same styles a tool row uses.
        const cardStatus = {
          running: 'running',
          finished: 'done',
          failed: 'error',
          interrupted: 'interrupted',
          cancelled: 'cancelled',
        }[item.status] ?? 'running';
        node.dataset.status = cardStatus;
        const described = describeChild(item);
        const iconNode = node.querySelector('.glink-tool-head .icon');
        if (iconNode.dataset.icon !== described.icon) iconNode.dataset.icon = described.icon;
        const nameNode = node.querySelector('.glink-tool-name');
        if (nameNode.textContent !== described.action) nameNode.textContent = described.action;
        const argsNode = node.querySelector('.glink-tool-args');
        if (argsNode.textContent !== described.object) argsNode.textContent = described.object;
        // 进行中时状态位借给 R3a 进度行（3 步 · 最近读取 report.md）；收口后
        // 回到状态词。复用 glink-tool-state 同一槽位，不引入新类。
        const stateNode = node.querySelector('.glink-tool-state');
        const stateText = childCardState(item);
        if (stateNode.textContent !== stateText) stateNode.textContent = stateText;
        const resultNode = node.querySelector('.glink-tool-result');
        const resultText = displayPath(item.resultPreview || '');
        resultNode.hidden = resultText === '';
        if (resultNode.textContent !== resultText) resultNode.textContent = resultText;
        // A settled dispatch folds back to a chip with an optional preview;
        // the reader's own open/close wins from then on.
        const hasDetail = resultText !== '';
        node.dataset.hasDetail = String(hasDetail);
        node.querySelector('.glink-tool-chevron').hidden = !hasDetail;
        if (!hasDetail) node.open = false;
        else if (node.dataset.touched !== 'true') node.open = false;
        return;
      }
      case 'text': {
        if (item.closed) {
          if (node.dataset.mode !== 'markdown' || node.dataset.text !== item.text) {
            node.innerHTML = renderMarkdown(item.text);
            node.dataset.mode = 'markdown';
            node.dataset.text = item.text;
          }
        } else if (node.dataset.mode !== 'stream' || node.textContent !== item.text) {
          node.textContent = item.text;
          node.dataset.mode = 'stream';
          delete node.dataset.text;
        }
        return;
      }
      case 'notice': {
        node.dataset.level = item.level;
        const text = noticeText(item);
        const label = node.querySelector('.glink-notice-text');
        if (label.textContent !== text) label.textContent = text;
        const detail = displayPath(item.detail || '');
        node.querySelector('.glink-notice-detail').textContent = detail;
        node.dataset.hasDetail = String(detail !== '');
        node.querySelector('.glink-row-chevron').hidden = !detail;
        return;
      }
      case 'stage': {
        const text = stageText(item);
        node.hidden = text === null;
        const label = node.querySelector('.glink-stage-text');
        if (text !== null && label.textContent !== text) label.textContent = text;
        return;
      }
      default:
    }
  }

  // ── answer frame ─────────────────────────────────────────────────────────

  function finalText() {
    if (answerIndex < 0 || !latest) return '';
    return latest.items[answerIndex]?.text ?? '';
  }

  function drawAnswer(model) {
    const terminal = model.status !== 'running';
    const hasAnswer = answerIndex >= 0;
    const unavailable = connection === 'unavailable';
    answerHead.hidden = !(terminal && (hasAnswer || model.hasReport)) || unavailable;

    output.hidden = !model.hasReport;
    const files = Array.isArray(model.deliverables) ? model.deliverables : [];
    deliveries.hidden = files.length === 0;
    if (files.length) {
      deliveryTitle.textContent = `交付文件 · ${files.length}`;
      deliveryList.replaceChildren();
      files.forEach((file, index) => {
        const row = el('div', 'glink-output-row');
        const name = String(file.path || '').split('/').pop() || '文件';
        const available = file.available === true && file.status === 'ok';
        const href = deliverableHref(model.runId, index);
        const open = el('a', 'glink-output-name');
        open.textContent = name;
        open.setAttribute('aria-label', `打开 ${name}`);
        if (available) { open.href = href; open.target = '_blank'; open.rel = 'noopener'; }
        else { open.removeAttribute('href'); open.setAttribute('aria-disabled', 'true'); }
        const text = el('span', 'glink-output-text');
        text.append(open);
        const status = file.status === 'rejected' ? `未能保存：${file.reason || '安全校验未通过'}`
          : available ? '已保存' : '整理中';
        text.append(el('span', 'glink-output-meta', [file.description, status].filter(Boolean).join(' · ')));
        const tile = document.createElement('img');
        tile.className = 'glink-file-tile'; tile.src = '/icons/markdown-32.png'; tile.alt = '';
        tile.width = 32; tile.height = 32;
        row.append(tile, text);
        if (available) {
          const download = el('a', 'icon-key');
          download.href = href;
          download.setAttribute('download', name);
          download.setAttribute('aria-label', `下载 ${name}`);
          download.append(icon('download', 16));
          row.append(download);
        }
        deliveryList.append(row);
      });
    }
    if (model.hasReport) {
      const href = reportHref(model.runId);
      outputDownload.href = href;
      outputDownload.setAttribute('download', reportDownloadName(model));
      downloadButton.href = href;
      downloadButton.setAttribute('download', reportDownloadName(model));
    }

    actions.hidden = !(model.status === 'finished' && (hasAnswer || model.hasReport)) || unavailable;
    copyButton.hidden = !hasAnswer;
    downloadButton.hidden = !model.hasReport;

    drawOutcome(model);
  }

  /* Connection & outcome states (标准库 ConnectionState)：每一态都凑齐四件 ——
   * 图标 + 标题 + 说明 + 可执行的下一步。停止是中性（不是错误，不用 ×），
   * 只有真正的失败用 destructive，网络慢用 warning。纯函数、可单测。 */
  function outcomeSpec(model) {
    return outcomeState(model, connection);
  }

  function drawOutcome(model) {
    const spec = outcomeSpec(model);
    const key = spec?.key ?? '';
    if (key === outcomeKey) return;
    outcomeKey = key;
    outcome.replaceChildren();
    outcome.hidden = !spec;
    if (!spec) return;
    outcome.dataset.tone = spec.tone;
    outcome.setAttribute('role', spec.live);
    outcome.setAttribute('aria-live', spec.live === 'alert' ? 'assertive' : 'polite');
    const tile = el('span', 'glink-outcome-icon');
    tile.setAttribute('aria-hidden', 'true');
    tile.append(icon(spec.icon, 16));
    const text = el('div', 'glink-outcome-text');
    const heading = el('h3', 'glink-outcome-title', spec.title);
    heading.id = `${uid}-outcome-title`;
    outcome.setAttribute('aria-labelledby', heading.id);
    text.append(heading, el('p', 'glink-outcome-desc', spec.text));
    if (spec.actions.length || spec.detail) {
      const row = el('div', 'glink-outcome-actions');
      for (const [label, action] of spec.actions) {
        const button = el('button', 'btn outline sm');
        button.type = 'button';
        if (action !== 'report') button.append(icon('refresh', 16));
        button.append(document.createTextNode(label));
        button.addEventListener('click', () => {
          if (action === 'rerun') onRerun?.(latest?.prompt ?? '');
          if (action === 'retry') onRetry?.(latest?.runId);
          if (action === 'report') onOpenReport?.(latest?.runId);
        });
        row.append(button);
      }
      let detailBlock = null;
      if (spec.detail) {
        const more = el('button', 'btn ghost sm glink-outcome-more');
        more.type = 'button';
        more.setAttribute('aria-expanded', 'false');
        more.setAttribute('aria-controls', `${uid}-outcome-detail`);
        more.append(document.createTextNode('查看详情'), icon('chevron-down', 16));
        detailBlock = el('pre', 'glink-outcome-detail', spec.detail);
        detailBlock.id = `${uid}-outcome-detail`;
        detailBlock.hidden = true;
        more.addEventListener('click', () => {
          const open = more.getAttribute('aria-expanded') !== 'true';
          more.setAttribute('aria-expanded', String(open));
          detailBlock.hidden = !open;
        });
        row.append(more);
      }
      text.append(row);
      if (detailBlock) text.append(detailBlock);
    }
    outcome.append(tile, text);
  }

  return {
    render: schedule,
    /** live | reconnecting | unavailable | null — drives the connection card. */
    setConnection(state) {
      connection = state;
      if (latest) schedule(latest);
    },
    dispose() {
      disposed = true;
      if (frame !== null) cancelAnimationFrame(frame);
      frame = null;
      latest = null;
      rendered = [];
      root.replaceChildren();
      delete root.dataset.status;
    },
  };
}

/* Connection & outcome states (标准库 ConnectionState)：每一态都凑齐四件 ——
 * 图标 + 标题 + 说明 + 可执行的下一步。停止是中性（不是错误，不用 ×），
 * 只有真正的失败用 destructive，网络慢用 warning。纯函数，便于单测。 */
export function outcomeState(model, connection) {
  if (connection === 'unavailable') {
    return {
      key: 'unavailable', tone: 'failed', icon: 'alert-circle', live: 'alert',
      title: '过程记录暂时读不到',
      text: model.hasReport
        ? '这项研究的过程记录没能加载，生成的报告不受影响，可以直接打开。'
        : '这项研究的过程记录没能加载。可以重试一次。',
      actions: [['重试', 'retry'], ...(model.hasReport ? [['打开报告', 'report']] : [])],
    };
  }
  if (connection === 'reconnecting' && model.status === 'running') {
    return {
      key: 'slow', tone: 'slow', icon: 'alert-circle', live: 'status',
      title: '连接中断，正在重连',
      text: '研究仍在后台进行，重新连上后会补齐这段时间的过程。',
      actions: [],
    };
  }
  if (model.status === 'cancelled') {
    return {
      key: 'stopped', tone: 'stopped', icon: 'dashed-circle', live: 'status',
      title: '已停止研究',
      text: '这不是错误。已完成的步骤和内容都保留在上面，可以用同一个问题重新开始。',
      actions: [['重新开始', 'rerun']],
    };
  }
  if (model.status === 'failed' && model.parentRunId && !model.sessionId) {
    // A follow-up that never got a session: the agent refused to reattach to
    // the parent's conversation (the host fails closed, never a fresh session).
    // Rerunning cannot fix that — the action starts the question as a new
    // research without the earlier context, and the text says so.
    return {
      key: `resume-failed:${model.error ?? ''}`, tone: 'failed', icon: 'alert-circle', live: 'alert',
      title: '追问没能接上之前的研究',
      text: '之前的研究和结果不受影响。重新开始会把这个问题作为一项新研究，不带之前的上下文。',
      actions: [['作为新研究开始', 'rerun']],
      detail: model.error ? displayPath(model.error) : '',
    };
  }
  if (model.status === 'failed') {
    return {
      key: `failed:${model.error ?? ''}`, tone: 'failed', icon: 'alert-circle', live: 'alert',
      title: '这次研究没有完成',
      text: '服务没有返回完整结果。已经生成的部分保留在上面，重试不会覆盖它。',
      actions: [['重试', 'rerun']],
      detail: model.error ? displayPath(model.error) : '',
    };
  }
  if (model.status === 'finished' && model.stopReason === 'length') {
    // The run finished, but the model was cut off at its output limit: the
    // delivery is incomplete, and saying only "已完成" would claim more than
    // the facts support.
    return {
      key: 'incomplete', tone: 'slow', icon: 'alert-circle', live: 'status',
      title: '交付不完整',
      text: '模型在输出长度上限处被截断，上面的回答和报告可能不完整。可以继续追问补齐，或重新开始。',
      actions: [['重新开始', 'rerun']],
    };
  }
  return null;
}
