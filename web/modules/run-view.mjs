/**
 * DOM rendering for one run.
 *
 * Rendering is incremental: each item in the run model owns a node, and only
 * items whose value changed since the last frame are touched. Streaming text
 * is shown as plain text while it grows and re-rendered as Markdown once the
 * block closes, so a long answer never re-parses on every delta.
 *
 * The view reads nothing but the model it is given: no fetch, no timers that
 * outlive `dispose`, no knowledge of pi.
 *
 * Shape follows the standard library's AI execution surfaces: thinking folds
 * (`Thinking`) keep a timeline rule and a steps vocabulary, tool calls render
 * as compact rows (`ToolChips`) — one row per call, the target on a chip, the
 * detail one level down. Class names stay `pi-*` because the browser
 * regression (tests/browser/run-smoke.mjs) asserts on them.
 */

const TOOL_ARG_KEYS = ['command', 'path', 'file_path', 'pattern', 'query', 'url'];

/** Tool name → (icon, action label). Mirrors 百鉴 ToolChips `ICONS` + 动作名. */
const TOOL_KINDS = [
  [/bash|shell|terminal|exec/i, 'terminal', '执行'],
  [/read|view|open_file|cat/i, 'file-text', '读取'],
  [/write|edit|create|apply|patch|multi_edit/i, 'new-chat', '修改'],
  [/search|grep|find|glob/i, 'search', '检索'],
  [/fetch|http|browse|webfetch|web_search/i, 'globe', '检索'],
  [/todos?|todo_write|plan|update_plan/i, 'checklist', '计划'],
];

export function createRunView({ root, renderMarkdown = (text) => text, onOpenReport = null }) {
  const header = document.createElement('div');
  header.className = 'pi-run-head';
  const timeline = document.createElement('div');
  timeline.className = 'pi-run-timeline';
  root.replaceChildren(header, timeline);

  /** @type {Array<{item: object, node: HTMLElement}>} */
  let rendered = [];
  let frame = null;
  let latest = null;
  let disposed = false;

  const reportLink = document.createElement('button');
  reportLink.type = 'button';
  reportLink.className = 'btn outline sm pi-report-button';
  reportLink.textContent = '查看报告';
  reportLink.hidden = true;
  reportLink.addEventListener('click', () => {
    if (latest?.hasReport && onOpenReport) onOpenReport(latest.runId);
  });

  const statusLine = document.createElement('p');
  statusLine.className = 'pi-run-status';
  const statusIcon = document.createElement('span');
  statusIcon.className = 'icon';
  statusIcon.dataset.icon = 'time';
  statusIcon.setAttribute('aria-hidden', 'true');
  const statusText = document.createElement('span');
  statusLine.append(statusIcon, statusText);
  const metaLine = document.createElement('p');
  metaLine.className = 'pi-run-meta';
  header.append(statusLine, metaLine, reportLink);

  function schedule(model) {
    latest = model;
    if (frame !== null || disposed) return;
    frame = requestAnimationFrame(() => {
      frame = null;
      if (!disposed && latest) draw(latest);
    });
  }

  function draw(model) {
    drawHeader(model);
    drawItems(model);
  }

  function drawHeader(model) {
    const statuses = {
      running: ['运行中', 'time'],
      finished: ['已完成', 'check-circle'],
      cancelled: ['已取消', 'close'],
      failed: ['已失败', 'alert-circle'],
    };
    const [label, icon] = statuses[model.status] ?? [model.status, 'time'];
    statusLine.dataset.status = model.status;
    statusIcon.dataset.icon = icon;
    statusText.textContent = `${label}${model.durationSeconds ? ` · ${model.durationSeconds}s` : ''}`;

    const bits = [];
    if (model.model) bits.push(model.model);
    if (model.sessionId) bits.push(`session ${model.sessionId.slice(0, 8)}`);
    if (model.usage?.totalTokens) bits.push(`${model.usage.totalTokens} tokens`);
    if (model.gaps) bits.push(`事件缺口 ${model.gaps}`);
    if (model.error) bits.push(model.error);
    metaLine.textContent = bits.join(' · ');
    metaLine.hidden = bits.length === 0;
    reportLink.hidden = !model.hasReport;
  }

  function drawItems(model) {
    const items = model.items;
    // Drop nodes whose item disappeared (a replayed stream can rewrite the tail).
    if (rendered.length > items.length) {
      for (const { node } of rendered.slice(items.length)) node.remove();
      rendered = rendered.slice(0, items.length);
    }
    items.forEach((item, index) => {
      const entry = rendered[index];
      if (!entry) {
        const node = createNode(item);
        timeline.append(node);
        rendered[index] = { item, node };
        update(node, item);
        return;
      }
      if (entry.item !== item) {
        entry.item = item;
        update(entry.node, item);
      }
    });
  }

  function createNode(item) {
    switch (item.kind) {
      case 'thinking': {
        const details = document.createElement('details');
        details.className = 'pi-thinking';
        const summary = document.createElement('summary');
        summary.className = 'trace-toggle';
        const icon = document.createElement('span');
        icon.className = 'icon';
        icon.dataset.icon = 'sparkles';
        icon.setAttribute('aria-hidden', 'true');
        const label = document.createElement('span');
        label.className = 'pi-thinking-label';
        const chevron = document.createElement('span');
        chevron.className = 'icon';
        chevron.dataset.icon = 'chevron-down';
        chevron.setAttribute('aria-hidden', 'true');
        summary.append(icon, label, chevron);
        const body = document.createElement('div');
        body.className = 'pi-thinking-body';
        details.append(summary, body);
        return details;
      }
      case 'tool': {
        // One row per call, the target on a chip, the detail one level down
        // (百鉴 ToolChips). `<details>` gives keyboard/AT semantics for free.
        const card = document.createElement('details');
        card.className = 'pi-tool';
        const head = document.createElement('summary');
        head.className = 'pi-tool-head trace-toggle';
        const icon = document.createElement('span');
        icon.className = 'icon';
        icon.dataset.icon = 'wrench';
        icon.setAttribute('aria-hidden', 'true');
        const title = document.createElement('span');
        title.className = 'pi-tool-name';
        const args = document.createElement('span');
        args.className = 'pi-tool-args';
        const state = document.createElement('span');
        state.className = 'pi-tool-state';
        const chevron = document.createElement('span');
        chevron.className = 'icon pi-tool-chevron';
        chevron.dataset.icon = 'chevron-down';
        chevron.setAttribute('aria-hidden', 'true');
        head.append(icon, title, args, state, chevron);
        head.addEventListener('click', () => {
          // The user's own choice outranks the streaming default from now on.
          card.dataset.touched = 'true';
        });
        const detail = document.createElement('div');
        detail.className = 'pi-tool-detail';
        const wrap = document.createElement('div');
        wrap.className = 'pi-tool-detail-wrap';
        const output = document.createElement('pre');
        output.className = 'pi-tool-output scroll-area';
        const result = document.createElement('div');
        result.className = 'pi-tool-result';
        wrap.append(output, result);
        detail.append(wrap);
        card.append(head, detail);
        return card;
      }
      case 'text': {
        const block = document.createElement('div');
        block.className = 'pi-text';
        return block;
      }
      case 'turn': {
        const turn = document.createElement('div');
        turn.className = 'pi-turn';
        return turn;
      }
      case 'notice': {
        const notice = document.createElement('div');
        notice.className = 'pi-notice';
        const icon = document.createElement('span');
        icon.className = 'icon';
        icon.dataset.icon = 'alert-circle';
        icon.setAttribute('aria-hidden', 'true');
        icon.style.setProperty('--icon-size', '14px');
        const text = document.createElement('span');
        notice.append(icon, text);
        return notice;
      }
      case 'stage':
      default: {
        const stage = document.createElement('div');
        stage.className = 'pi-stage';
        return stage;
      }
    }
  }

  function update(node, item) {
    switch (item.kind) {
      case 'thinking': {
        const label = node.querySelector('.pi-thinking-label');
        const body = node.querySelector('.pi-thinking-body');
        const chars = item.text.length;
        node.dataset.status = item.closed ? 'done' : 'running';
        const next = item.closed ? `思考 · ${chars} 字` : `思考中 · ${chars} 字`;
        if (label.textContent !== next) label.textContent = next;
        if (body.textContent !== item.text) body.textContent = item.text;
        return;
      }
      case 'tool': {
        node.dataset.status = item.status;
        const [icon, action] = toolKind(item.name);
        const iconNode = node.querySelector('.pi-tool-head .icon');
        if (iconNode.dataset.icon !== icon) iconNode.dataset.icon = icon;
        const nameNode = node.querySelector('.pi-tool-name');
        const nextName = `${action}${action === item.name ? '' : ` · ${item.name}`}`;
        if (nameNode.textContent !== nextName) nameNode.textContent = nextName;
        node.querySelector('.pi-tool-state').textContent = toolStateLabel(item);
        const argsText = summarizeArgs(item.args);
        const argsNode = node.querySelector('.pi-tool-args');
        if (argsNode.textContent !== argsText) argsNode.textContent = argsText;
        const outputNode = node.querySelector('.pi-tool-output');
        if (outputNode.textContent !== item.output) outputNode.textContent = item.output;
        outputNode.hidden = item.output === '';
        const resultNode = node.querySelector('.pi-tool-result');
        const resultText = item.result || '';
        resultNode.hidden = resultText === '';
        if (resultNode.textContent !== resultText) resultNode.textContent = resultText;
        // Live output is the point while a call runs; once it settles the row goes
        // back to a chip — unless the reader opened it themselves.
        const hasDetail = item.output !== '' || resultText !== '';
        node.querySelector('.pi-tool-chevron').hidden = !hasDetail;
        if (!hasDetail) node.open = false;
        else if (node.dataset.touched !== 'true') node.open = item.status === 'running';
        return;
      }
      case 'text': {
        if (item.closed) {
          const html = renderMarkdown(item.text);
          if (node.dataset.mode !== 'markdown' || node.dataset.text !== item.text) {
            node.innerHTML = html;
            node.dataset.mode = 'markdown';
            node.dataset.text = item.text;
          }
        } else {
          if (node.dataset.mode !== 'stream' || node.textContent !== item.text) {
            node.textContent = item.text;
            node.dataset.mode = 'stream';
            delete node.dataset.text;
          }
        }
        return;
      }
      case 'turn': {
        const label = `第 ${item.index} 轮`;
        if (node.textContent !== label) node.textContent = label;
        return;
      }
      case 'notice': {
        node.dataset.level = item.level;
        const text = node.querySelector('span:last-child');
        const label = item.detail ? `${item.message} · ${item.detail}` : item.message;
        if (text.textContent !== label) text.textContent = label;
        return;
      }
      case 'stage':
      default: {
        const label = item.label ?? item.stage ?? '';
        if (node.textContent !== label) node.textContent = label;
      }
    }
  }

  return {
    render: schedule,
    dispose() {
      disposed = true;
      if (frame !== null) cancelAnimationFrame(frame);
      frame = null;
      latest = null;
      rendered = [];
      root.replaceChildren();
    },
  };
}

//: Human labels for every tool state the model can hold. A tool that never
//: saw its own terminal frame follows the run's outcome instead of claiming
//: success: failed runs leave it `interrupted`, cancelled runs `cancelled`.
const TOOL_STATE_LABELS = {
  running: '执行中',
  done: '完成',
  error: '失败',
  cancelled: '已取消',
  interrupted: '已中断',
};

function toolStateLabel(item) {
  if (item.status === 'error' || item.isError) return TOOL_STATE_LABELS.error;
  return TOOL_STATE_LABELS[item.status] ?? TOOL_STATE_LABELS.done;
}

/** Map a pi tool name onto the standard library's (icon, action) vocabulary. */
function toolKind(name) {
  const text = String(name ?? '');
  for (const [pattern, icon, action] of TOOL_KINDS) {
    if (pattern.test(text)) return [icon, action];
  }
  return ['wrench', text || '工具'];
}

export function summarizeArgs(args) {
  if (!args || typeof args !== 'object') return '';
  const keys = Object.keys(args);
  if (!keys.length) return '';
  const ordered = [...TOOL_ARG_KEYS.filter((key) => key in args), ...keys.filter((key) => !TOOL_ARG_KEYS.includes(key))];
  const parts = [];
  for (const key of ordered.slice(0, 3)) {
    const value = args[key];
    const text = typeof value === 'string' ? value : JSON.stringify(value);
    if (text === undefined) continue;
    parts.push(`${key}: ${text.length > 90 ? `${text.slice(0, 90)}…` : text}`);
  }
  return parts.join('  ·  ');
}
