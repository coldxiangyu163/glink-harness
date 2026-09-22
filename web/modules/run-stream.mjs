/**
 * Product event stream → run view model.
 *
 * This module is the front-end half of the event contract: the host emits the
 * transport-normalised events defined in `host/pi_glink/events.py`, and here
 * they become an immutable view model the renderer can draw. It never touches
 * the DOM and never parses terminal text, so it is directly importable by Node
 * tests (`tests/web/run-stream.test.mjs`).
 *
 * Two properties matter beyond "drawing":
 *
 * * **Reconnect safety** — frames carry a per-run `seq`. A frame at or below
 *   the model's watermark is a replay the model already applied and is ignored;
 *   a gap is recorded instead of faking continuity.
 * * **No hidden state** — every visible thing (thinking blocks, tool cards,
 *   text, stages, notices) is an item in `model.items`, derived only from
 *   events. Re-rendering from the model cannot disagree with the stream.
 */

export const RUNNING = 'running';
export const TERMINAL_STATUSES = new Set(['finished', 'failed', 'cancelled']);
/**
 * Terminal *frames*. A finished run starts at a terminal status, so consumers
 * must decide "stop listening" from the frame type — checking the model status
 * would end a history replay after its first frame.
 */
export const TERMINAL_EVENT_TYPES = new Set(['run_finished', 'run_failed', 'run_cancelled']);

const STAGE_LABELS = {
  agent_start: 'pi 开始处理',
  agent_end: '本轮 agent 结束',
  compaction_start: '上下文压缩开始',
  compaction_end: '上下文压缩结束',
  auto_retry_start: '自动重试',
  auto_retry_end: '重试结束',
};

export function createRunModel(run = {}) {
  return {
    runId: run.runId ?? null,
    status: run.status ?? RUNNING,
    prompt: run.prompt ?? '',
    createdAt: run.createdAt ?? null,
    finishedAt: run.finishedAt ?? null,
    durationSeconds: run.durationSeconds ?? null,
    sessionId: run.sessionId ?? null,
    model: null,
    thinkingLevel: null,
    hasReport: Boolean(run.hasReport),
    stopReason: run.stopReason ?? null,
    error: run.error ?? null,
    usage: run.usage ?? null,
    items: [],
    turn: 0,
    lastSeq: 0,
    gaps: 0,
    ignoredFrames: 0,
  };
}

/** Apply one event, returning a new model. Unknown types are ignored. */
export function applyEvent(model, event) {
  if (!event || typeof event.type !== 'string') return model;
  const seq = Number.isFinite(event.seq) ? event.seq : null;
  if (seq !== null) {
    if (seq <= model.lastSeq) {
      return { ...model, ignoredFrames: model.ignoredFrames + 1 };
    }
    if (seq > model.lastSeq + 1 && model.lastSeq !== 0) {
      model = { ...model, gaps: model.gaps + 1 };
    }
  }
  const next = { ...model, lastSeq: seq ?? model.lastSeq };
  const payload = event.payload ?? {};
  switch (event.type) {
    case 'run_started':
      return { ...next, prompt: payload.prompt ?? next.prompt };
    case 'session_ready':
      return {
        ...next,
        sessionId: payload.sessionId ?? next.sessionId,
        model: payload.model ?? next.model,
        thinkingLevel: payload.thinkingLevel ?? next.thinkingLevel,
      };
    case 'turn_started':
      return appendItem({ ...next, turn: next.turn + 1 }, {
        kind: 'turn',
        index: next.turn + 1,
      });
    case 'turn_finished':
      return closeCurrentBlocks(next);
    case 'thinking_delta':
      return appendDelta(next, 'thinking', payload.text);
    case 'assistant_delta':
      return appendDelta(next, 'text', payload.text);
    case 'assistant_message':
      return applyFinalMessage(next, payload);
    case 'tool_started':
      return startTool(next, payload);
    case 'tool_updated':
      return updateTool(next, payload);
    case 'tool_finished':
      return finishTool(next, payload);
    case 'run_stage':
      return appendItem(next, { kind: 'stage', stage: payload.stage, label: stageLabel(payload) });
    case 'kernel_status':
      return appendItem(next, {
        kind: 'notice',
        level: payload.level ?? 'info',
        message: payload.message ?? 'kernel status',
        detail: payload.detail ?? '',
      });
    case 'usage_reported':
      return { ...next, usage: payload.usage ?? next.usage };
    case 'run_finished':
      return finishRun(next, 'finished', payload);
    case 'run_failed':
      return finishRun(next, 'failed', payload);
    case 'run_cancelled':
      return finishRun(next, 'cancelled', payload);
    default:
      return next;
  }
}

export function applyEvents(model, events) {
  return events.reduce(applyEvent, model);
}

/** Subscribe to a run's SSE stream with reconnect-aware status reporting. */
export function createRunSubscription({
  runId,
  eventsUrl,
  eventSourceFactory = (url) => new EventSource(url),
  onEvent,
  onStatus,
  onError,
}) {
  let source = null;
  let closed = false;
  let status = 'idle';

  const report = (nextStatus) => {
    if (closed || status === nextStatus) return;
    status = nextStatus;
    onStatus?.(nextStatus);
  };

  const open = () => {
    report('connecting');
    source = eventSourceFactory(eventsUrl(runId));
    source.onopen = () => report('live');
    source.onerror = () => report('reconnecting');
    source.onmessage = (message) => {
      try {
        const event = JSON.parse(message.data);
        onEvent?.(event);
      } catch (error) {
        onError?.(error);
      }
    };
    // Named frames (`event: <type>`) do not reach onmessage; listen explicitly.
    for (const type of [
      'run_started', 'session_ready', 'turn_started', 'turn_finished',
      'thinking_delta', 'assistant_delta', 'assistant_message',
      'tool_started', 'tool_updated', 'tool_finished', 'run_stage',
      'kernel_status', 'usage_reported', 'run_finished', 'run_failed', 'run_cancelled',
    ]) {
      source.addEventListener(type, (message) => {
        try {
          onEvent?.(JSON.parse(message.data));
        } catch (error) {
          onError?.(error);
        }
      });
    }
  };

  open();

  return {
    get status() {
      return status;
    },
    close() {
      closed = true;
      source?.close();
      source = null;
      status = 'closed';
    },
  };
}

/** Terminal-state facts worth surfacing, derived from the model alone. */
export function runSummary(model) {
  const thinking = model.items.filter((item) => item.kind === 'thinking');
  const tools = model.items.filter((item) => item.kind === 'tool');
  return {
    status: model.status,
    isTerminal: TERMINAL_STATUSES.has(model.status),
    turnCount: model.items.filter((item) => item.kind === 'turn').length,
    thinkingBlocks: thinking.length,
    thinkingChars: thinking.reduce((total, item) => total + item.text.length, 0),
    toolCount: tools.length,
    failedTools: tools.filter((item) => item.isError).length,
    gaps: model.gaps,
    ignoredFrames: model.ignoredFrames,
  };
}

// -- mutation helpers (all immutable at the model level) ---------------------

function appendItem(model, item) {
  return { ...model, items: [...model.items, item] };
}

function replaceLast(model, item) {
  const items = model.items.slice(0, -1);
  items.push(item);
  return { ...model, items };
}

function lastItem(model) {
  return model.items.length ? model.items[model.items.length - 1] : null;
}

function appendDelta(model, kind, text) {
  if (typeof text !== 'string' || text === '') return model;
  const last = lastItem(model);
  if (last && last.kind === kind && last.turn === model.turn && !last.closed) {
    return replaceLast(model, { ...last, text: last.text + text });
  }
  return appendItem(model, { kind, turn: model.turn, text, closed: false });
}

function closeCurrentBlocks(model) {
  // A turn can leave more than one block open (thinking followed by text), so
  // closing only the tail would strand an earlier block in "思考中" forever.
  let changed = false;
  const items = model.items.map((item) => {
    if ((item.kind === 'text' || item.kind === 'thinking') && !item.closed) {
      changed = true;
      return { ...item, closed: true };
    }
    return item;
  });
  return changed ? { ...model, items } : model;
}

function applyFinalMessage(model, payload) {
  const text = typeof payload.text === 'string' ? payload.text : '';
  const last = lastItem(model);
  if (text === '') return model;
  if (last && last.kind === 'text' && !last.closed) {
    // The streamed text and pi's authoritative message agree in practice; use
    // pi's copy so a dropped delta cannot leave a truncated answer on screen.
    return replaceLast(model, { ...last, text, closed: true });
  }
  return appendItem(model, { kind: 'text', turn: model.turn, text, closed: true });
}

function startTool(model, payload) {
  const id = payload.toolCallId ?? `tool-${model.items.length}`;
  return appendItem(model, {
    kind: 'tool',
    toolCallId: id,
    name: payload.toolName || 'tool',
    args: payload.args ?? {},
    output: '',
    result: '',
    isError: false,
    status: 'running',
  });
}

function findToolIndex(model, toolCallId) {
  for (let index = model.items.length - 1; index >= 0; index -= 1) {
    const item = model.items[index];
    if (item.kind === 'tool' && item.toolCallId === toolCallId) return index;
  }
  return -1;
}

function updateTool(model, payload) {
  const index = findToolIndex(model, payload.toolCallId);
  if (index < 0) return model;
  const item = model.items[index];
  const items = model.items.slice();
  items[index] = { ...item, name: item.name || payload.toolName, output: item.output + (payload.output ?? '') };
  return { ...model, items };
}

function finishTool(model, payload) {
  const index = findToolIndex(model, payload.toolCallId);
  if (index < 0) return model;
  const item = model.items[index];
  const items = model.items.slice();
  items[index] = {
    ...item,
    name: item.name || payload.toolName,
    result: payload.result ?? '',
    isError: Boolean(payload.isError),
    status: Boolean(payload.isError) ? 'error' : 'done',
  };
  return { ...model, items };
}

function finishRun(model, status, payload) {
  const withStage = appendItem(model, {
    kind: 'stage',
    stage: status,
    label: status === 'finished' ? '本次运行完成' : status === 'cancelled' ? '本次运行已取消' : '本次运行失败',
  });
  return {
    ...withStage,
    status,
    finishedAt: Date.now() / 1000,
    durationSeconds: payload.durationSeconds ?? withStage.durationSeconds,
    hasReport: Boolean(payload.reportPath) || withStage.hasReport,
    sessionId: payload.sessionId ?? withStage.sessionId,
    stopReason: payload.stopReason ?? withStage.stopReason,
    error: payload.error ?? withStage.error,
    usage: payload.usage ?? withStage.usage,
    items: withStage.items.map((item) =>
      item.kind === 'tool' && item.status === 'running' ? { ...item, status: 'done' } : item,
    ),
  };
}

function stageLabel(payload) {
  const base = STAGE_LABELS[payload.stage] ?? payload.stage ?? '阶段';
  if (payload.stage === 'auto_retry_start' && payload.attempt) {
    return `${base}（第 ${payload.attempt} 次）`;
  }
  if (payload.stage === 'compaction_start' && payload.reason) {
    return `${base}（${payload.reason}）`;
  }
  return base;
}
