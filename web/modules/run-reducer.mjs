/**
 * Product event stream → run state: the pure reducer.
 *
 * `reduce(state, event)` is the front-end half of the event contract. The host
 * validates every frame against `host/pi_glink/event_schema.py` before it is
 * committed; the vocabulary and payload versions here come from the module
 * generated from that schema (`event-schema.gen.mjs`). This module never
 * touches the DOM, the network or the clock, so Node tests drive it with event
 * fixtures (`tests/web/run-reducer.test.mjs`, `tests/fixtures/events/`).
 *
 * Semantics this reducer owns:
 *
 * * **Reconnect safety** — frames carry a per-run `seq`. A frame at or below
 *   the watermark is a replay already applied and is ignored
 *   (`ignoredFrames`); a gap is recorded (`gaps`) instead of faking continuity.
 * * **Versioned frames** — a frame whose `v` differs from the schema version of
 *   its type (including a frame without `v`, i.e. an event log written before
 *   the schema existed, and an unknown type) is not applied and is counted in
 *   `rejectedFrames`; its `seq` still advances the watermark. Old logs are not
 *   migrated (docs/architecture.md §3.6「事件 schema 与版本」).
 * * **Cumulative snapshots** — `tool_updated.output` and `subagent_updated`
 *   carry the value accumulated so far, not a delta: they replace.
 * * **Deltas** — `assistant_delta` / `thinking_delta` append to the open block.
 * * **Authoritative terminal frames** — `assistant_message` replaces the
 *   streamed text (unless it is `truncated` and shorter), `tool_finished`
 *   drops a preview equal to its result, and the run terminal frame settles
 *   running tools and children (its `children` summary is the source of truth).
 * * **No hidden state** — every visible thing is an item in `state.items`,
 *   derived only from events.
 */

import { EVENT_VERSIONS, TERMINAL_EVENT_TYPES } from './event-schema.gen.mjs';

export { EVENT_VERSIONS, TERMINAL_EVENT_TYPES };

export const RUNNING = 'running';
export const TERMINAL_STATUSES = new Set(['finished', 'failed', 'cancelled']);

//: Stage codes → readable labels. The view decides which stages the reader
//: actually sees (present.mjs `stageText`); lifecycle bookkeeping stays hidden.
const STAGE_LABELS = {
  agent_start: '开始处理',
  agent_end: '本轮处理结束',
  compaction_start: '上下文压缩开始',
  compaction_end: '上下文压缩结束',
  auto_retry_start: '自动重试',
  auto_retry_end: '重试结束',
};

/** Initial state of one run, seeded from its REST record. */
export function createRunState(run = {}) {
  return {
    runId: run.runId ?? null,
    /** The run this one follows up (null for a first turn). */
    parentRunId: run.parentRunId ?? null,
    status: run.status ?? RUNNING,
    prompt: run.prompt ?? '',
    createdAt: run.createdAt ?? null,
    finishedAt: run.finishedAt ?? null,
    durationSeconds: run.durationSeconds ?? null,
    sessionId: run.sessionId ?? null,
    model: null,
    thinkingLevel: null,
    hasReport: Boolean(run.hasReport),
    deliverables: Array.isArray(run.deliverables) ? run.deliverables : [],
    stopReason: run.stopReason ?? null,
    error: run.error ?? null,
    usage: run.usage ?? null,
    /**
     * ChildRun collection (docs/architecture.md §3.5), keyed by `n`: the typed
     * subagent events maintain it live, and the terminal frame's `children`
     * summary is the replay-time source of truth that settles it.
     */
    children: [],
    items: [],
    turn: 0,
    lastSeq: 0,
    gaps: 0,
    ignoredFrames: 0,
    rejectedFrames: 0,
  };
}

/** True when the frame's `v` is the schema version of its type. */
export function isCurrentVersion(event) {
  const expected = EVENT_VERSIONS[event.type];
  return expected !== undefined && event.v === expected;
}

/** Apply one event, returning a new state (the input is never mutated). */
export function reduce(state, event) {
  if (!event || typeof event.type !== 'string') return state;
  const seq = Number.isFinite(event.seq) ? event.seq : null;
  if (seq !== null) {
    if (seq <= state.lastSeq) {
      return { ...state, ignoredFrames: state.ignoredFrames + 1 };
    }
    if (seq > state.lastSeq + 1 && state.lastSeq !== 0) {
      state = { ...state, gaps: state.gaps + 1 };
    }
  }
  const next = { ...state, lastSeq: seq ?? state.lastSeq };
  if (!isCurrentVersion(event)) {
    return { ...next, rejectedFrames: next.rejectedFrames + 1 };
  }
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
    case 'deliverables_declared': {
      const known = new Set(next.deliverables.map((file) => file.path));
      const files = Array.isArray(payload.files) ? payload.files.filter((file) =>
        file && typeof file.path === 'string' && !known.has(file.path) && known.add(file.path)) : [];
      return { ...next, deliverables: [...next.deliverables, ...files.map((file) =>
        ({ ...file, status: 'declared', available: false }))].slice(0, 8) };
    }
    case 'tool_started':
      return startTool(next, payload);
    case 'tool_updated':
      return updateTool(next, payload);
    case 'tool_finished':
      return finishTool(next, payload);
    case 'subagent_started':
      return startChild(next, payload);
    case 'subagent_updated':
      return updateChildProgress(next, payload);
    case 'subagent_finished':
      return finishChildren(next, payload);
    case 'run_stage':
      return appendItem(next, {
        kind: 'stage',
        stage: payload.stage,
        attempt: payload.attempt ?? null,
        entries: payload.entries ?? [],
        label: stageLabel(payload),
      });
    case 'kernel_status':
      return appendItem(next, {
        kind: 'notice',
        level: payload.level ?? 'info',
        message: payload.message ?? 'notice',
        detail: payload.detail ?? '',
      });
    case 'usage_reported':
      return { ...next, usage: payload.usage ?? next.usage };
    case 'run_finished':
      return finishRun(next, 'finished', payload, event.ts);
    case 'run_failed':
      return finishRun(next, 'failed', payload, event.ts);
    case 'run_cancelled':
      return finishRun(next, 'cancelled', payload, event.ts);
    default:
      return next;
  }
}

export function reduceAll(state, events) {
  return events.reduce(reduce, state);
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
    rejectedFrames: model.rejectedFrames,
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
  const truncated = payload.truncated === true;
  const last = lastItem(model);
  if (text === '') return model;
  if (last && last.kind === 'text' && !last.closed) {
    // The streamed text and pi's authoritative message agree in practice; use
    // pi's copy so a dropped delta cannot leave a truncated answer on screen.
    // The exception is a frame the host itself clipped (`truncated: true`): it
    // carries less than the stream already delivered, so the longer streamed
    // body stays and the item is only closed.
    const keepStreamed = truncated && last.text.length > text.length;
    return replaceLast(model, {
      ...last,
      text: keepStreamed ? last.text : text,
      closed: true,
      truncated,
    });
  }
  return appendItem(model, { kind: 'text', turn: model.turn, text, closed: true, truncated });
}

function startTool(model, payload) {
  const id = payload.toolCallId ?? `tool-${model.items.length}`;
  return appendItem(model, {
    kind: 'tool',
    toolCallId: id,
    name: payload.toolName || 'tool',
    args: payload.args ?? {},
    ...(typeof payload.presentation === 'string' ? { presentation: payload.presentation } : {}),
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
  // pi streams the accumulated output so far, not a delta (docs/rpc.md:
  // "the accumulated output so far (not just the delta)"), so we replace.
  const output = typeof payload.output === 'string' ? payload.output : item.output;
  items[index] = { ...item, name: item.name || payload.toolName, output };
  return { ...model, items };
}

function finishTool(model, payload) {
  const index = findToolIndex(model, payload.toolCallId);
  if (index < 0) return model;
  const item = model.items[index];
  const items = model.items.slice();
  const result = payload.result ?? '';
  items[index] = {
    ...item,
    name: item.name || payload.toolName,
    result,
    // pi's terminal frame repeats the accumulated output for streamed tools
    // (fixture ebf75f590601: both are the same 495 characters), so keeping the
    // preview would render the same text twice. The terminal result wins
    // whenever it carries exactly what the stream already showed.
    output: result !== '' && result === item.output ? '' : item.output,
    isError: Boolean(payload.isError),
    status: Boolean(payload.isError) ? 'error' : 'done',
  };
  return { ...model, items };
}

//: A tool still running when the run ends never saw its own terminal frame;
//: its state must follow the run, not default to "done".
const INTERRUPTED_TOOL_STATUS = {
  failed: 'interrupted',
  cancelled: 'cancelled',
};

// -- ChildRun (§3.5): the typed lifecycle replaces generic tool cards ---------
//
// The event stream is only the push side: `model.children` mirrors the
// authoritative child.json documents, and each dispatch also owns one timeline
// card (kind 'subagent') so the reader sees the delegation where it happened.
// A `subagent` call whose args do not match the dispatch shape arrives as a
// generic tool card and stays one — the reducer never invents a dispatch.

function upsertChild(children, child) {
  const index = children.findIndex((entry) => entry.n === child.n);
  if (index < 0) return [...children, child].sort((a, b) => a.n - b.n);
  const next = children.slice();
  next[index] = { ...next[index], ...child };
  return next;
}

function startChild(model, payload) {
  // A payload without a valid child number is not a dispatch the host committed
  // (the projection pins `{n, toolCallId, agent, task}`); ignore it rather than
  // inventing an identity.
  const n = payload.n;
  if (!Number.isInteger(n) || n < 0) return model;
  const child = {
    n,
    toolCallId: typeof payload.toolCallId === 'string' ? payload.toolCallId : '',
    agent: typeof payload.agent === 'string' ? payload.agent : '',
    task: typeof payload.task === 'string' ? payload.task : '',
    status: 'running',
    resultPreview: '',
  };
  const children = upsertChild(model.children, child);
  const existing = model.items.findIndex((item) => item.kind === 'subagent' && item.n === n);
  if (existing >= 0) {
    const items = model.items.slice();
    items[existing] = { ...items[existing], ...child };
    return { ...model, children, items };
  }
  return appendItem({ ...model, children }, { kind: 'subagent', ...child });
}

//: R3a 运行中进度（§3.5「运行中进度（R3a）」）：一条节流后的瞬时事实——
//: 最新值取胜（回放按 seq 顺序施加，同一 child 的多条只留最后一条的效果）、
//: 永不参与终态判定、不写权威文档。它只能附着在类型生命周期已经打开的
//: child 上；对未知 n 的进度帧忽略，不凭空造出一个派发。
function updateChildProgress(model, payload) {
  const n = payload.n;
  if (!Number.isInteger(n) || n < 0) return model;
  const child = model.children.find((entry) => entry.n === n);
  if (!child) return model;
  const progress = mergeChildProgress(child.progress, payload);
  const children = model.children.map((entry) => (entry.n === n ? { ...entry, progress } : entry));
  const items = model.items.map(
    (item) => (item.kind === 'subagent' && item.n === n ? { ...item, progress } : item),
  );
  return { ...model, children, items };
}

function mergeChildProgress(previous, payload) {
  const prevSteps = previous && Number.isFinite(previous.steps) ? previous.steps : 0;
  // steps 是计数器：乱序或异常输入不得让它回退（latest-wins 的唯一例外）。
  // 其他字段每帧整体替换，丢中间帧不改变最终呈现。
  const steps = Number.isFinite(payload.steps) ? Math.max(prevSteps, payload.steps) : prevSteps;
  return {
    steps,
    lastToolName: typeof payload.lastToolName === 'string' ? payload.lastToolName : '',
    lastToolTarget: typeof payload.lastToolTarget === 'string' ? payload.lastToolTarget : '',
    bytes: Number.isFinite(payload.bytes) && payload.bytes > 0 ? payload.bytes : 0,
  };
}

function finishChildren(model, payload) {
  const ns = Array.isArray(payload.ns) ? payload.ns.filter((value) => Number.isInteger(value)) : [];
  if (!ns.length) return model;
  const status = typeof payload.status === 'string' && payload.status
    ? payload.status
    : payload.isError ? 'failed' : 'finished';
  // One tool call closes every child it dispatched; the preview is shared, not
  // split (§3.5).
  const preview = typeof payload.resultPreview === 'string' ? payload.resultPreview : '';
  const children = model.children.map(
    (child) => (ns.includes(child.n) ? { ...child, status, resultPreview: preview } : child),
  );
  const items = model.items.map(
    (item) => (item.kind === 'subagent' && ns.includes(item.n) ? { ...item, status, resultPreview: preview } : item),
  );
  return { ...model, children, items };
}

//: A child still running at the verdict follows the run — the same rule as
//: tools (§3.4). The terminal frame's `children` summary is the replay-time
//: source of truth: it settles children whose own finished frame never
//: arrived, and it is the only place a replay learns the final status of a
//: dispatch whose typed events are complete.
function settleChildren(model, status, summary) {
  const follow = status === 'finished' ? 'finished' : INTERRUPTED_TOOL_STATUS[status];
  let children = model.children;
  if (Array.isArray(summary)) {
    for (const entry of summary) {
      if (!entry || !Number.isInteger(entry.n) || entry.n < 0) continue;
      const existing = children.find((child) => child.n === entry.n);
      children = upsertChild(children, {
        n: entry.n,
        toolCallId: existing?.toolCallId ?? '',
        agent: typeof entry.agent === 'string' && entry.agent ? entry.agent : existing?.agent ?? '',
        task: typeof entry.task === 'string' && entry.task ? entry.task : existing?.task ?? '',
        status: typeof entry.status === 'string' && entry.status ? entry.status : existing?.status ?? follow,
        resultPreview: existing?.resultPreview ?? '',
        // R2 增强字段：终帧摘要带什么就附着什么（durationMs/toolCount/artifacts），
        // 未增强的 child 没有这些键——面板据此区分「协议可见」与「增强」。
        ...(typeof entry.durationMs === 'number' ? { durationMs: entry.durationMs } : {}),
        ...(typeof entry.toolCount === 'number' ? { toolCount: entry.toolCount } : {}),
        ...(Array.isArray(entry.artifacts) ? { artifacts: entry.artifacts } : {}),
      });
    }
  }
  children = children.map((child) => (child.status === 'running' ? { ...child, status: follow } : child));
  const byN = new Map(children.map((child) => [child.n, child]));
  const items = model.items.map((item) => {
    if (item.kind !== 'subagent') return item;
    const settled = byN.get(item.n);
    if (!settled) return item.status === 'running' ? { ...item, status: follow } : item;
    return { ...item, agent: settled.agent, task: settled.task, status: settled.status };
  });
  return { ...model, children, items };
}

/** The children panel's rows (agent / task / status / R2 enrichment); empty means the panel stays hidden. */
export function childrenPanel(model) {
  return model.children.map((child) => ({
    n: child.n,
    agent: child.agent,
    task: child.task,
    status: child.status,
    ...(typeof child.durationMs === 'number' ? { durationMs: child.durationMs } : {}),
    ...(typeof child.toolCount === 'number' ? { toolCount: child.toolCount } : {}),
    ...(Array.isArray(child.artifacts) ? { artifacts: child.artifacts } : {}),
  }));
}

function finishRun(model, status, payload, ts) {
  const withStage = appendItem(model, {
    kind: 'stage',
    stage: status,
    label: status === 'finished' ? '本次运行完成' : status === 'cancelled' ? '本次运行已取消' : '本次运行失败',
  });
  const settled = settleChildren(withStage, status, payload.children);
  return {
    ...settled,
    status,
    // The frame's own commit time: replaying a finished run shows when it
    // ended, and the reducer stays free of the clock.
    finishedAt: Number.isFinite(ts) ? ts : settled.finishedAt,
    durationSeconds: payload.durationSeconds ?? settled.durationSeconds,
    hasReport: Boolean(payload.reportPath) || settled.hasReport,
    deliverables: Array.isArray(payload.deliverables)
      ? payload.deliverables.map((file) => ({ ...file, available: file.status === 'ok' }))
      : settled.deliverables,
    sessionId: payload.sessionId ?? settled.sessionId,
    stopReason: payload.stopReason ?? settled.stopReason,
    error: payload.error ?? settled.error,
    usage: payload.usage ?? settled.usage,
    items: settled.items.map((item) => {
      if (item.kind !== 'tool' || item.status !== 'running') return item;
      const terminal = status === 'finished' ? 'done' : INTERRUPTED_TOOL_STATUS[status];
      return { ...item, status: terminal };
    }),
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
