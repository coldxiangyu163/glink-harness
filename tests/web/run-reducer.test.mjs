/**
 * Fixture-driven tests of the pure run reducer (`web/modules/run-reducer.mjs`)
 * and the feed that folds a subscription through it (`run-stream.mjs`).
 *
 * `tests/fixtures/events/*.jsonl` are synthetic runs written in the persisted
 * envelope `{type, v, runId, seq, ts, payload}`. `tests/test_event_schema.py`
 * checks every versioned fixture against the host schema, so the frames fed
 * here are frames the bus would commit. `legacy-unversioned.jsonl` is a log
 * written before the schema existed (no `v`): it must not be applied.
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
  return readFileSync(new URL(`${name}.jsonl`, FIXTURE_DIR), 'utf8')
    .split('\n')
    .filter((line) => line.trim())
    .map((line) => JSON.parse(line));
}

function replay(name, run = {}) {
  const frames = load(name);
  return { frames, state: reduceAll(createRunState({ runId: frames[0].runId, ...run }), frames) };
}

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

test('a mid-run snapshot shows the latest accumulated output, not a concatenation', () => {
  const frames = load('tool-snapshot');
  const upTo = frames.findLastIndex((frame) => frame.type === 'tool_updated');
  const state = reduceAll(createRunState({ runId: 'fx-tool' }), frames.slice(0, upTo + 1));
  const tool = state.items.find((item) => item.kind === 'tool');
  assert.equal(tool.status, 'running');
  assert.equal(tool.output, 'line1\nline2\n');
});

test('a truncated authoritative frame keeps the longer streamed answer', () => {
  const { state } = replay('truncated-message');
  const text = state.items.filter((item) => item.kind === 'text');
  assert.equal(text.length, 1);
  assert.equal(text[0].text, '答'.repeat(300));
  assert.equal(text[0].truncated, true);
  assert.equal(text[0].closed, true);
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
