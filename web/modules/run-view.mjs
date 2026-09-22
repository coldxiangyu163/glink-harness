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
 */

const TOOL_ARG_KEYS = ['command', 'path', 'file_path', 'pattern', 'query', 'url'];

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
  reportLink.className = 'button quiet pi-report-button';
  reportLink.textContent = '查看报告';
  reportLink.hidden = true;
  reportLink.addEventListener('click', () => {
    if (latest?.hasReport && onOpenReport) onOpenReport(latest.runId);
  });

  const statusLine = document.createElement('p');
  statusLine.className = 'pi-run-status';
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
    const label =
      model.status === 'running'
        ? '运行中'
        : model.status === 'finished'
          ? '已完成'
          : model.status === 'cancelled'
            ? '已取消'
            : '已失败';
    statusLine.dataset.status = model.status;
    statusLine.textContent = `${label}${model.durationSeconds ? ` · ${model.durationSeconds}s` : ''}`;

    const bits = [];
    if (model.model) bits.push(model.model);
    if (model.sessionId) bits.push(`session ${model.sessionId.slice(0, 8)}`);
    if (model.usage?.totalTokens) bits.push(`${model.usage.totalTokens} tokens`);
    if (model.gaps) bits.push(`事件缺口 ${model.gaps}`);
    if (model.error) bits.push(model.error);
    metaLine.textContent = bits.join(' · ');
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
        const body = document.createElement('div');
        body.className = 'pi-thinking-body';
        details.append(summary, body);
        return details;
      }
      case 'tool': {
        const card = document.createElement('div');
        card.className = 'pi-tool';
        const head = document.createElement('div');
        head.className = 'pi-tool-head';
        const title = document.createElement('span');
        title.className = 'pi-tool-name';
        const state = document.createElement('span');
        state.className = 'pi-tool-state';
        head.append(title, state);
        const args = document.createElement('div');
        args.className = 'pi-tool-args';
        const output = document.createElement('pre');
        output.className = 'pi-tool-output';
        const result = document.createElement('div');
        result.className = 'pi-tool-result';
        card.append(head, args, output, result);
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
        const summary = node.querySelector('summary');
        const body = node.querySelector('.pi-thinking-body');
        const chars = item.text.length;
        summary.textContent = item.closed
          ? `思考 · ${chars} 字`
          : `思考中 · ${chars} 字`;
        if (body.textContent !== item.text) body.textContent = item.text;
        return;
      }
      case 'tool': {
        node.dataset.status = item.status;
        node.querySelector('.pi-tool-name').textContent = item.name;
        node.querySelector('.pi-tool-state').textContent =
          item.status === 'running' ? '执行中' : item.isError ? '失败' : '完成';
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
        const label = item.detail ? `${item.message} · ${item.detail}` : item.message;
        if (node.textContent !== label) node.textContent = label;
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
