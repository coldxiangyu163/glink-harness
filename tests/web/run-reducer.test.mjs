/**
 * Fixture-driven tests of the pure run reducer (`web/modules/run-reducer.mjs`)
 * and the feed that folds a subscription through it (`run-stream.mjs`).
 *
 * `tests/fixtures/events/*.jsonl` are synthetic runs written in the persisted
 * envelope `{type, v, runId, seq, ts, payload}` (`#` lines are comment headers).
 * `tests/test_event_fixture_contract.py` checks every versioned fixture against
 * the host schema, so the frames fed here are frames the bus would commit.
 * `legacy-unversioned.jsonl` is a log written before the schema existed (no
 * `v`): it must not be applied.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readdirSync, readFileSync } from 'node:fs';

import { EVENT_SCHEMA, EVENT_TYPES, EVENT_VERSIONS, TERMINAL_EVENT_TYPES } from '../../web/modules/event-schema.gen.mjs';
import {
  childrenPanel,
  createRunState,
  isCurrentVersion,
  reduce,
  reduceAll,
  runSummary,
} from '../../web/modules/run-reducer.mjs';
import { createRunFeed } from '../../web/modules/run-stream.mjs';

const FIXTURE_DIR = new URL('../fixtures/events/', import.meta.url);

function load(name) {
  // `#` lines are the fixture's comment header (tests/fixtures/events/README.md).
  return readFileSync(new URL(`${name}.jsonl`, FIXTURE_DIR), 'utf8')
    .split('\n')
    .filter((line) => line.trim() && !line.startsWith('#'))
    .map((line) => JSON.parse(line));
}

function replay(name, run = {}) {
  const frames = load(name);
  return { frames, state: reduceAll(createRunState({ runId: frames[0].runId, ...run }), frames) };
}

test('declarations appear live; terminal results and REST records settle the same cards', () => {
  const frame = (seq, type, payload) => ({ seq, type, payload, v: 1, ts: 10 + seq });
  const declared = reduce(createRunState({ runId: 'r' }), frame(1, 'deliverables_declared', {
    files: [{ path: 'district/brief.md', description: '辖区备忘录' }],
  }));
  assert.deepEqual(declared.deliverables, [{ path: 'district/brief.md',
    description: '辖区备忘录', status: 'declared', available: false }]);
  const terminal = reduce(declared, frame(2, 'run_finished', {
    status: 'finished', reportPath: '/private/artifacts/report.md', reportStatus: 'ok',
    stopReason: null, error: null, cleanup: { released: true }, children: [],
    deliverables: [{ path: 'district/brief.md', description: '辖区备忘录', status: 'ok' }],
  }));
  assert.equal(terminal.hasReport, true);
  assert.equal(terminal.deliverables[0].available, true);
  assert.deepEqual(createRunState({ deliverables: terminal.deliverables }).deliverables, terminal.deliverables);
  assert.deepEqual(createRunState().deliverables, [], 'old pi runs show no delivery section');
  const old = reduce(declared, frame(2, 'unknown_v2', { files: [] }));
  assert.equal(old.lastSeq, 2);
  assert.equal(old.rejectedFrames, 1);
  assert.equal(old.deliverables[0].status, 'declared');
});

test('the generated schema module is the reducer vocabulary', () => {
  assert.deepEqual(Object.keys(EVENT_SCHEMA), [...EVENT_TYPES]);
  for (const type of EVENT_TYPES) {
    assert.equal(EVENT_VERSIONS[type], EVENT_SCHEMA[type].v);
    assert.equal(TERMINAL_EVENT_TYPES.has(type), EVENT_SCHEMA[type].terminal);
  }
  assert.deepEqual([...TERMINAL_EVENT_TYPES].sort(), ['run_cancelled', 'run_failed', 'run_finished']);
});

test('every versioned fixture frame is current; the legacy fixture has none', () => {
  const names = readdirSync(FIXTURE_DIR).filter((name) => name.endsWith('.jsonl')).map((name) => name.slice(0, -6));
  assert.ok(names.length >= 5);
  for (const name of names) {
    const frames = load(name);
    const current = frames.filter(isCurrentVersion).length;
    assert.equal(current, name === 'legacy-unversioned' ? 0 : frames.length, name);
  }
});

test('cumulative tool snapshots replace, deltas append, the authoritative message wins', () => {
  const { frames, state } = replay('tool-snapshot');
  assert.equal(state.prompt, '列出工作目录');
  assert.equal(state.sessionId, 's-1');
  assert.equal(state.model, 'fake/model');

  const tool = state.items.find((item) => item.kind === 'tool');
  assert.equal(tool.status, 'done');
  assert.equal(tool.result, 'line1\nline2\n');
  // The terminal result repeats the last snapshot, so the preview is dropped.
  assert.equal(tool.output, '');

  const thinking = state.items.find((item) => item.kind === 'thinking');
  assert.equal(thinking.text, '先看看目录。');
  assert.equal(thinking.closed, true, 'turn_finished closes every open block');

  const text = state.items.filter((item) => item.kind === 'text');
  assert.equal(text.length, 1);
  assert.equal(text[0].text, '目录里有两行输出。', '权威正文替换流式增量');

  assert.equal(state.status, 'finished');
  assert.equal(state.hasReport, true);
  assert.equal(state.durationSeconds, 3.5);
  assert.equal(state.usage.totalTokens, 15);
  assert.equal(state.finishedAt, frames.at(-1).ts, '结束时间取终帧的提交时间');
  assert.deepEqual(
    { gaps: state.gaps, ignored: state.ignoredFrames, rejected: state.rejectedFrames, lastSeq: state.lastSeq },
    { gaps: 0, ignored: 0, rejected: 0, lastSeq: frames.length },
  );
});

test('old tool frame replays unchanged; optional presentation survives new frame projection', () => {
  const old = load('tool-snapshot').find((frame) => frame.type === 'tool_started');
  const saved = JSON.stringify(old);
  const previous = reduce(createRunState({ runId: old.runId }), old);
  assert.equal(previous.items[0].presentation, undefined);
  assert.equal(JSON.stringify(old), saved, 'replay must not rewrite historical input');
  const withHint = { ...old, payload: { ...old.payload, presentation: 'execute' } };
  const next = reduce(createRunState({ runId: old.runId }), withHint);
  assert.equal(next.items[0].presentation, 'execute');
  assert.equal(JSON.stringify(old), saved);
});

test('a mid-run snapshot shows the latest accumulated output, not a concatenation', () => {
  const frames = load('tool-snapshot');
  const upTo = frames.findLastIndex((frame) => frame.type === 'tool_updated');
  const state = reduceAll(createRunState({ runId: 'fx-tool' }), frames.slice(0, upTo + 1));
  const tool = state.items.find((item) => item.kind === 'tool');
  assert.equal(tool.status, 'running');
  assert.equal(tool.output, 'line1\nline2\n');
});

test('a truncated authoritative frame replaces the streamed answer and stays marked', () => {
  const { state } = replay('truncated-message');
  const text = state.items.filter((item) => item.kind === 'text');
  assert.equal(text.length, 1);
  assert.equal(text[0].text, `${'答'.repeat(100)}\n…[truncated 200 chars]`);
  assert.equal(text[0].truncated, true);
  assert.equal(text[0].closed, true);
});

test('a turn-scoped authoritative frame merge-replaces every text block of its turn', () => {
  // ACP 族（dsh 标准回合）：thinking→正文段→tool→回合末权威帧，以及正文被
  // 工具夹断的回合。权威帧覆盖整回合正文，reducer 用它合并替换本轮全部文本
  // 块，不追加第二份正文（BLOCKER 第 2 轮）。
  const { state } = replay('acp-turn-scoped-message');
  const text = state.items.filter((item) => item.kind === 'text');
  assert.equal(text.length, 2, '两轮各合并为一个文本块，不重复');
  // 第 1 轮（thinking→段一→tool）：合并块落在本轮唯一文本块的原位。
  assert.equal(text[0].text, '段一。');
  assert.equal(text[0].turn, 1);
  assert.equal(text[0].closed, true);
  // 第 2 轮（前段→tool→后段）：合并块落在本轮最后一个文本块的原位，
  // 即工具卡之后、回合收尾处；前段并入它而不是另留一份。
  assert.equal(text[1].text, '前段，后段。');
  assert.equal(text[1].turn, 2);
  const afterTurn2 = state.items.slice(state.items.findIndex((item) => item.kind === 'turn' && item.index === 2));
  assert.deepEqual(afterTurn2.map((item) => item.kind), ['turn', 'tool', 'text', 'stage']);
  // 第 2 轮的合并不触碰第 1 轮已闭合并块（turn 字段限定范围）。
  assert.equal(state.items.filter((item) => item.kind === 'tool').length, 2);
});

test('a truncated turn-scoped frame replaces the turn body with the authoritative copy', () => {
  const frame = (seq, type, payload) => ({ seq, type, payload, v: 1, ts: 20 + seq });
  const frames = [
    frame(1, 'turn_started', {}),
    frame(2, 'assistant_delta', { text: '前半，' }),
    frame(3, 'tool_started', { toolCallId: 't1', toolName: 'bash', args: {} }),
    frame(4, 'tool_finished', { toolCallId: 't1', toolName: 'bash', isError: false, result: 'ok' }),
    frame(5, 'assistant_delta', { text: '后半。' }),
    // 宿主截断后的权威副本比已流式正文短：仍以权威副本为准，仅闭块并标记不完整。
    frame(6, 'assistant_message', { text: '前半，', truncated: true, turnScoped: true }),
    frame(7, 'turn_finished', {}),
  ];
  const state = reduceAll(createRunState({ runId: 'fx-turn-trunc' }), frames);
  const text = state.items.filter((item) => item.kind === 'text');
  assert.equal(text.length, 1);
  assert.equal(text[0].text, '前半，');
  assert.equal(text[0].truncated, true);
  assert.equal(text[0].closed, true);
});

test('a frame without turnScoped keeps the pi per-message rule at any tail', () => {
  // pi 族零回归：权威帧不带 turnScoped 时仍是「替换最后一个未闭文本块，
  // 否则追加」——即使末项是工具卡也不走合并语义。
  const frame = (seq, type, payload) => ({ seq, type, payload, v: 1, ts: 30 + seq });
  const frames = [
    frame(1, 'turn_started', {}),
    frame(2, 'assistant_delta', { text: '流式段。' }),
    frame(3, 'tool_started', { toolCallId: 't1', toolName: 'bash', args: {} }),
    frame(4, 'assistant_message', { text: '权威段。' }),
    frame(5, 'turn_finished', {}),
  ];
  const state = reduceAll(createRunState({ runId: 'fx-pi-tail' }), frames);
  const text = state.items.filter((item) => item.kind === 'text');
  assert.equal(text.length, 2, '不带标记的帧不合并本轮文本块');
  assert.equal(text[0].text, '流式段。');
  assert.equal(text[1].text, '权威段。');
});

test('subagent progress is latest-wins and the terminal children summary settles every child', () => {
  const frames = load('subagents');
  const beforeTerminal = reduceAll(createRunState({ runId: 'fx-sub' }), frames.slice(0, -1));
  const live = beforeTerminal.children.find((child) => child.n === 0);
  assert.equal(live.status, 'finished');
  assert.equal(live.progress.steps, 5);
  assert.equal(live.progress.lastToolName, 'fetch_content');
  assert.equal(beforeTerminal.children.find((child) => child.n === 1).status, 'running');

  const state = reduce(beforeTerminal, frames.at(-1));
  assert.deepEqual(childrenPanel(state), [
    { n: 0, agent: 'researcher', task: '调研甲', status: 'finished', durationMs: 4200, toolCount: 5 },
    { n: 1, agent: 'researcher', task: '调研乙', status: 'failed' },
  ]);
  const cards = state.items.filter((item) => item.kind === 'subagent');
  assert.deepEqual(cards.map((card) => card.status), ['finished', 'failed']);
});

test('a failed run interrupts the tool that never saw its terminal frame', () => {
  const { state } = replay('failed-running-tool');
  assert.equal(state.status, 'failed');
  assert.equal(state.error, 'pi exited with code 1');
  assert.equal(state.stopReason, 'kernel_exit');
  const tool = state.items.find((item) => item.kind === 'tool');
  assert.equal(tool.status, 'interrupted');
  assert.equal(tool.output, 'partial');
  const stage = state.items.find((item) => item.kind === 'stage' && item.stage === 'auto_retry_start');
  assert.equal(stage.label, '自动重试（第 1 次）');
  const notice = state.items.find((item) => item.kind === 'notice');
  assert.deepEqual([notice.level, notice.detail], ['warning', 'exit 1']);
  assert.equal(runSummary(state).isTerminal, true);
});

test('an event log written before the schema (no v) is not applied', () => {
  const { frames, state } = replay('legacy-unversioned', { status: 'running' });
  assert.deepEqual(state.items, [], '旧格式帧不渲染任何条目');
  assert.equal(state.status, 'running', '旧终帧不得改写运行状态');
  assert.equal(state.rejectedFrames, frames.length);
  assert.equal(state.lastSeq, frames.at(-1).seq, '拒收帧仍推进 seq 水位');
  assert.equal(runSummary(state).rejectedFrames, frames.length);
});

test('a frame whose v differs from the schema version is rejected, not guessed', () => {
  const [started] = load('tool-snapshot');
  const base = createRunState({ runId: 'fx-tool' });
  const future = reduce(base, { ...started, v: started.v + 1, payload: { prompt: '新版本' } });
  assert.equal(future.prompt, '');
  assert.equal(future.rejectedFrames, 1);
  const stringly = reduce(base, { ...started, v: String(started.v) });
  assert.equal(stringly.rejectedFrames, 1, 'v 必须是数值且严格相等');
  const current = reduce(base, started);
  assert.equal(current.prompt, '列出工作目录');
  assert.equal(current.rejectedFrames, 0);
});

test('replayed frames at or below the watermark are ignored; the reducer never mutates its input', () => {
  const frames = load('tool-snapshot');
  const once = reduceAll(createRunState({ runId: 'fx-tool' }), frames);
  const frozen = JSON.stringify(once);
  const twice = reduceAll(once, frames);
  assert.equal(JSON.stringify(once), frozen, '输入状态不被修改');
  assert.equal(twice.ignoredFrames, frames.length);
  assert.deepEqual(twice.items, once.items);
});

test('a missing seq range is recorded as a gap', () => {
  const frames = load('tool-snapshot');
  const state = reduceAll(createRunState({ runId: 'fx-tool' }), [frames[0], frames[3], frames[4]]);
  assert.equal(state.gaps, 1);
});

/* ---- createRunFeed: subscription → reducer → owner ---- */

function fakeSourceFactory() {
  const sources = [];
  const factory = (url) => {
    const source = {
      url,
      readyState: 1,
      closed: false,
      listeners: new Map(),
      addEventListener(type, handler) {
        this.listeners.set(type, handler);
      },
      close() {
        this.closed = true;
        this.readyState = 2;
      },
      emit(frame) {
        this.listeners.get(frame.type)?.({ data: JSON.stringify(frame) });
      },
    };
    sources.push(source);
    return source;
  };
  return { sources, factory };
}

test('a feed reduces every frame, notifies the owner and closes on the terminal frame', () => {
  const frames = load('subagents');
  const { sources, factory } = fakeSourceFactory();
  const states = [];
  const terminals = [];
  const feed = createRunFeed({
    run: { runId: 'fx-sub' },
    eventsUrl: (runId) => `/api/runs/${runId}/events`,
    eventSourceFactory: factory,
    onState: (state, event) => states.push([event.seq, state.status]),
    onTerminal: (state) => terminals.push(state),
  });
  assert.equal(sources.length, 1);
  assert.equal(sources[0].url, '/api/runs/fx-sub/events');
  for (const type of EVENT_TYPES) assert.ok(sources[0].listeners.has(type), `listens for ${type}`);

  for (const frame of frames) sources[0].emit(frame);
  assert.equal(states.length, frames.length);
  assert.equal(states.at(-1)[1], 'finished');
  assert.equal(terminals.length, 1);
  assert.equal(terminals[0], feed.state);
  assert.equal(sources[0].closed, true, '终帧后关闭流');
  assert.equal(feed.status, 'closed');
  assert.deepEqual(feed.state, reduceAll(createRunState({ runId: 'fx-sub' }), frames), 'feed 与纯 reduce 结果一致');
});

test('a finished run replaying its history keeps the stream open until the terminal frame', () => {
  const frames = load('tool-snapshot');
  const { sources, factory } = fakeSourceFactory();
  const feed = createRunFeed({
    run: { runId: 'fx-tool', status: 'finished' },
    eventsUrl: () => '/events',
    eventSourceFactory: factory,
  });
  sources[0].emit(frames[0]);
  assert.equal(sources[0].closed, false, '终态起步的回放不因状态提前关闭');
  for (const frame of frames.slice(1)) sources[0].emit(frame);
  assert.equal(sources[0].closed, true);
  assert.equal(feed.frames, frames.length);
});

test('a feed drops frames once its owner is no longer active', () => {
  const frames = load('tool-snapshot');
  const { sources, factory } = fakeSourceFactory();
  let active = true;
  const seen = [];
  const feed = createRunFeed({
    run: { runId: 'fx-tool' },
    eventsUrl: () => '/events',
    eventSourceFactory: factory,
    isActive: () => active,
    onState: (_state, event) => seen.push(event.seq),
  });
  sources[0].emit(frames[0]);
  active = false;
  sources[0].emit(frames[1]);
  assert.deepEqual(seen, [1]);
  assert.equal(feed.state.lastSeq, 1);
});

test('a malformed frame reaches onError and leaves the state untouched', () => {
  const { sources, factory } = fakeSourceFactory();
  const errors = [];
  const feed = createRunFeed({
    run: { runId: 'fx' },
    eventsUrl: () => '/events',
    eventSourceFactory: factory,
    onError: (error) => errors.push(error),
  });
  sources[0].listeners.get('run_started')({ data: '{not json' });
  assert.equal(errors.length, 1);
  assert.equal(feed.frames, 0);
  assert.equal(feed.state.lastSeq, 0);
});

test('a lazy feed connects only when the owner asks, and never twice', () => {
  const frames = load('subagents');
  const { sources, factory } = fakeSourceFactory();
  const states = [];
  const terminals = [];
  const feed = createRunFeed({
    run: { runId: 'fx-sub' },
    eventsUrl: (runId) => `/api/runs/${runId}/events`,
    eventSourceFactory: factory,
    lazy: true,
    onState: (_state, event) => states.push(event.seq),
    onTerminal: (state) => terminals.push(state),
  });
  assert.equal(sources.length, 0, '构造时不建立连接');
  assert.equal(feed.status, 'idle');

  feed.open();
  assert.equal(sources.length, 1, 'open() 建立唯一连接');
  feed.open();
  assert.equal(sources.length, 1, 'open() 幂等');

  for (const frame of frames) sources[0].emit(frame);
  assert.equal(states.length, frames.length);
  assert.equal(terminals.length, 1);
  assert.equal(sources[0].closed, true, '终帧后关闭流');
  assert.equal(feed.status, 'closed');

  // A finished feed must never reopen the stream.
  feed.open();
  assert.equal(sources.length, 1);
});

test('a lazy feed closed before it opens never connects', () => {
  const { sources, factory } = fakeSourceFactory();
  const feed = createRunFeed({
    run: { runId: 'fx-lazy' },
    eventsUrl: () => '/events',
    eventSourceFactory: factory,
    lazy: true,
  });
  feed.close();
  assert.equal(feed.status, 'closed');
  feed.open();
  assert.equal(sources.length, 0, '关闭后 open() 不得再建立连接');
});
