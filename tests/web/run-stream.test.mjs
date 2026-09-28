/**
 * Node regressions for the front-end event model.
 *
 * `run-reducer.mjs` (pure reduce) and `run-stream.mjs` (subscription) are the
 * front-end half of the event contract, so they are tested where it is
 * cheapest: without a browser. The DOM renderer is covered
 * separately by the browser regression (tests/browser/run-smoke.mjs).
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import { EVENT_VERSIONS } from '../../web/modules/event-schema.gen.mjs';
import {
  createRunState,
  reduce,
  reduceAll,
  runSummary,
} from '../../web/modules/run-reducer.mjs';
import {
  createRunSubscription,
  STREAM_RETRY_DELAYS_MS,
} from '../../web/modules/run-stream.mjs';
import { renderMarkdown } from '../../web/modules/markdown.mjs';

const frame = (seq, type, payload = {}) => ({ seq, type, runId: 'r1', payload });

// 手写帧按当前 schema 版本盖 v（帧自带的 v 保留）；版本不符的拒收由
// run-reducer.test.mjs 覆盖。
const stamp = (event) => ({ v: EVENT_VERSIONS[event.type], ...event });
const apply = (model, event) => reduce(model, stamp(event));
const applyAll = (model, events) => reduceAll(model, events.map(stamp));

test('markdown renderer is importable without a document', () => {
  const html = renderMarkdown('# 标题\n\n正文 **加粗**');
  assert.match(html, /<h1[^>]*>标题<\/h1>/);
  assert.match(html, /<strong>加粗<\/strong>/);
});

test('thinking and text deltas accumulate into blocks per turn', () => {
  let model = createRunState({ runId: 'r1', prompt: '问题' });
  model = applyAll(model, [
    frame(1, 'turn_started'),
    frame(2, 'thinking_delta', { text: '先看 ' }),
    frame(3, 'thinking_delta', { text: '目录' }),
    frame(4, 'assistant_delta', { text: '你好' }),
    frame(5, 'turn_finished'),
    frame(6, 'turn_started'),
    frame(7, 'thinking_delta', { text: '第二轮思考' }),
  ]);

  const kinds = model.items.map((item) => item.kind);
  assert.deepEqual(kinds, ['turn', 'thinking', 'text', 'turn', 'thinking']);
  const thinking = model.items.filter((item) => item.kind === 'thinking');
  assert.equal(thinking[0].text, '先看 目录');
  assert.equal(thinking[0].closed, true, 'turn_finished closes the block');
  assert.equal(thinking[1].closed, false);
  assert.equal(model.items.find((item) => item.kind === 'text').text, '你好');
});

test('tool cards track streaming output and final status', () => {
  let model = createRunState({ runId: 'r1' });
  model = applyAll(model, [
    frame(1, 'tool_started', { toolCallId: 'c1', toolName: 'bash', args: { command: 'ls -la' } }),
    frame(2, 'tool_updated', { toolCallId: 'c1', toolName: 'bash', output: 'total 8\n' }),
    // pi sends the accumulated output so far, so the second frame repeats the
    // first one and adds a line (docs/rpc.md: "not just the delta").
    frame(3, 'tool_updated', { toolCallId: 'c1', toolName: 'bash', output: 'total 8\nreport.md\n' }),
  ]);
  const running = model.items.at(-1);
  assert.equal(running.status, 'running');
  assert.equal(running.output, 'total 8\nreport.md\n');

  model = apply(model, frame(4, 'tool_finished', { toolCallId: 'c1', toolName: 'bash', result: 'done', isError: false }));
  const done = model.items.at(-1);
  assert.equal(done.status, 'done');
  assert.equal(done.result, 'done');

  model = apply(model, frame(5, 'tool_started', { toolCallId: 'c2', toolName: 'read', args: {} }));
  model = apply(model, frame(6, 'tool_finished', { toolCallId: 'c2', toolName: 'read', result: 'ENOENT', isError: true }));
  assert.equal(model.items.at(-1).status, 'error');
  assert.equal(runSummary(model).failedTools, 1);
});

test('cumulative tool snapshots replace the display instead of duplicating prefixes', () => {
  // Mirrors the real run `ebf75f590601`, whose bash tool emitted
  // tool_updated snapshots of length 0 → 53 → 495, each containing the
  // previous one. Appending would grow the card to 548 characters.
  const snapshots = [
    '',
    '/Users/coldxiangyu/.pi-glink/workspaces/run\n',
    '/Users/coldxiangyu/.pi-glink/workspaces/run\n\nPI vars:\nPI_CODING_AGENT_DIR=/data/pi-agent\n',
  ];
  let model = createRunState({ runId: 'r1' });
  model = apply(model, frame(1, 'tool_started', { toolCallId: 'c1', toolName: 'bash', args: { command: 'env' } }));
  snapshots.forEach((output, index) => {
    model = apply(model, frame(index + 2, 'tool_updated', { toolCallId: 'c1', toolName: 'bash', output }));
  });
  const tool = model.items.at(-1);
  assert.equal(tool.output, snapshots.at(-1));
  assert.ok(tool.output.length < snapshots.reduce((sum, s) => sum + s.length, 0), 'replacement must be shorter than concatenation');
  assert.ok(!tool.output.includes('\n\n\n'), 'accumulated prefixes must not be concatenated');
  assert.equal(tool.output.match(/PI vars/g)?.length, 1);
});

test('the audited run ebf75f590601 replays with replacement semantics (real fixture)', () => {
  // `tests/fixtures/tool-update-frames.jsonl` is the frame sequence of that
  // run's bash tool (seq 85–89) as stored in events.jsonl, re-stamped with the
  // schema's envelope version `v: 1` (logs written before the schema carry no
  // `v` and are no longer readable).
  const fixture = readFileSync(new URL('../fixtures/tool-update-frames.jsonl', import.meta.url), 'utf8');
  const frames = fixture.split('\n').filter((line) => line.trim()).map((line) => JSON.parse(line));
  assert.equal(frames.length, 5);
  const updates = frames.filter((event) => event.type === 'tool_updated');
  assert.deepEqual(updates.map((event) => event.payload.output.length), [0, 53, 495]);
  assert.ok(updates[1].payload.output.startsWith(updates[0].payload.output));
  assert.ok(updates[2].payload.output.startsWith(updates[1].payload.output));

  // While the tool runs, each update replaces the card body.
  let model = createRunState({ runId: 'ebf75f590601' });
  model = applyAll(model, frames.filter((event) => event.type !== 'tool_finished'));
  let tool = model.items.find((item) => item.kind === 'tool');
  assert.equal(tool.output, updates[2].payload.output, 'the last snapshot is the display');
  assert.equal(tool.output.length, 495);
  const concatenated = updates.map((event) => event.payload.output).join('');
  assert.equal(concatenated.length, 548, 'appending the frames is what the old code did');
  assert.notEqual(tool.output, concatenated);
  assert.equal(tool.output.match(/workspaces\/ebf75f590601/g)?.length, 1, 'the prefix must appear once');

  // The terminal frame repeats that same text as ``result``: the card must
  // render it once, not once per field.
  model = applyAll(model, frames);
  tool = model.items.find((item) => item.kind === 'tool');
  assert.equal(tool.result, updates[2].payload.output, 'the terminal result carries the full text');
  assert.equal(tool.output, '', 'the streamed preview is dropped once the result repeats it');
  const rendered = tool.output + tool.result;
  assert.equal(rendered.length, 495);
  assert.equal(rendered.match(/workspaces\/ebf75f590601/g)?.length, 1);
  assert.equal(tool.status, 'done');
});

test('a failed or cancelled run never leaves a running tool looking done', () => {
  for (const [type, expected] of [
    ['run_failed', 'interrupted'],
    ['run_cancelled', 'cancelled'],
  ]) {
    let model = createRunState({ runId: 'r1' });
    model = applyAll(model, [
      frame(1, 'tool_started', { toolCallId: 'c1', toolName: 'bash', args: {} }),
      frame(2, type, { status: type === 'run_failed' ? 'failed' : 'cancelled' }),
    ]);
    const tool = model.items.find((item) => item.kind === 'tool');
    assert.equal(tool.status, expected, `${type} must map an unfinished tool to ${expected}`);
    assert.notEqual(tool.status, 'done');
  }

  // A tool that did see its own terminal frame keeps that state.
  let model = createRunState({ runId: 'r1' });
  model = applyAll(model, [
    frame(1, 'tool_started', { toolCallId: 'c1', toolName: 'bash', args: {} }),
    frame(2, 'tool_finished', { toolCallId: 'c1', toolName: 'bash', result: 'ok', isError: false }),
    frame(3, 'run_failed', { status: 'failed' }),
  ]);
  assert.equal(model.items.find((item) => item.kind === 'tool').status, 'done');
});

test('replayed or duplicate frames never rewrite the model', () => {
  let model = createRunState({ runId: 'r1' });
  model = applyAll(model, [
    frame(5, 'assistant_delta', { text: '一次' }),
    frame(5, 'assistant_delta', { text: '一次' }), // duplicate replay
    frame(3, 'assistant_delta', { text: '旧帧' }), // stale replay
  ]);
  assert.equal(model.items.at(-1).text, '一次');
  assert.equal(model.ignoredFrames, 2);
  assert.equal(model.lastSeq, 5);
});

test('a gap in sequence numbers is recorded, not hidden', () => {
  let model = createRunState({ runId: 'r1' });
  model = applyAll(model, [
    frame(1, 'turn_started'),
    frame(4, 'assistant_delta', { text: '断档后的文本' }),
  ]);
  assert.equal(model.gaps, 1);
  assert.equal(model.items.at(-1).text, '断档后的文本');
  assert.equal(runSummary(model).gaps, 1);
});

test('the terminal frame decides the run status and closes running tools', () => {
  let model = createRunState({ runId: 'r1' });
  model = applyAll(model, [
    frame(1, 'turn_started'),
    frame(2, 'tool_started', { toolCallId: 'c1', toolName: 'write', args: {} }),
    frame(3, 'run_finished', { status: 'finished', reportPath: '/tmp/report.md', durationSeconds: 12.5, sessionId: 's1' }),
  ]);
  assert.equal(model.status, 'finished');
  assert.equal(model.hasReport, true);
  assert.equal(model.durationSeconds, 12.5);
  assert.equal(model.sessionId, 's1');
  assert.equal(model.items.find((item) => item.kind === 'tool').status, 'done');
  assert.ok(runSummary(model).isTerminal);
});

test('pi authoritative message replaces the streamed text', () => {
  let model = createRunState({ runId: 'r1' });
  model = applyAll(model, [
    frame(1, 'turn_started'),
    frame(2, 'assistant_delta', { text: '被截断的' }),
    frame(3, 'assistant_message', { text: '被截断的完整回答' }),
  ]);
  assert.equal(model.items.at(-1).text, '被截断的完整回答');
  assert.equal(model.items.at(-1).closed, true);

  // A closed block must not be extended by a later delta.
  model = apply(model, frame(4, 'assistant_delta', { text: '追加' }));
  assert.equal(model.items.at(-1).text, '追加');
  assert.equal(model.items.at(-2).text, '被截断的完整回答');
});

test('notices and stages carry readable labels', () => {
  let model = createRunState({ runId: 'r1' });
  model = applyAll(model, [
    frame(1, 'kernel_status', { level: 'warning', message: 'extension_error', detail: 'boom' }),
    frame(2, 'run_stage', { stage: 'auto_retry_start', attempt: 2 }),
    frame(3, 'run_stage', { stage: 'compaction_start', reason: 'context' }),
  ]);
  assert.equal(model.items[0].kind, 'notice');
  assert.equal(model.items[0].detail, 'boom');
  assert.match(model.items[1].label, /第 2 次/);
  assert.match(model.items[2].label, /上下文压缩/);
});

test('usage frames carry run-cumulative totals and are not summed again', () => {
  // The host accumulates per-request snapshots and publishes the run total; the
  // front-end therefore *replaces* the display value. Summing here would double
  // count every turn (100 + 200 would show 300 after the second frame but 600
  // after a third), and a replayed frame would inflate it once more.
  let model = createRunState({ runId: 'r1' });
  model = apply(
    model,
    frame(1, 'usage_reported', {
      usage: { input: 100, output: 0, totalTokens: 100, requests: 1 },
      requestUsage: { input: 100, output: 0, totalTokens: 100 },
    }),
  );
  assert.equal(model.usage.totalTokens, 100);

  model = apply(
    model,
    frame(2, 'usage_reported', {
      usage: { input: 300, output: 0, totalTokens: 300, requests: 2 },
      requestUsage: { input: 200, output: 0, totalTokens: 200 },
    }),
  );
  assert.equal(model.usage.totalTokens, 300, 'the frame is the run total, not a delta');
  assert.equal(model.usage.requests, 2);

  // A duplicate frame at the same seq is ignored by the watermark (no inflation).
  model = apply(
    model,
    frame(2, 'usage_reported', {
      usage: { input: 300, output: 0, totalTokens: 300, requests: 2 },
    }),
  );
  assert.equal(model.usage.totalTokens, 300);
  assert.equal(model.ignoredFrames, 1);

  // The terminal frame carries the authoritative final total.
  model = apply(
    model,
    frame(3, 'run_finished', {
      status: 'finished',
      usage: { input: 300, output: 0, totalTokens: 300, requests: 2 },
    }),
  );
  assert.equal(model.usage.totalTokens, 300);
});

test('a run with no reported usage shows none rather than a fabricated value', () => {
  let model = createRunState({ runId: 'r1' });
  model = apply(model, frame(1, 'run_finished', { status: 'finished' }));
  assert.equal(model.usage, null);
});

test('unknown event types are ignored, not rendered as noise', () => {
  // 未知类型不在 schema 内，没有当前版本：计为拒收帧，不渲染，水位照常前进。
  let model = createRunState({ runId: 'r1' });
  const before = model.items.length;
  model = applyAll(model, [frame(1, 'future_event_type', { anything: 1 })]);
  assert.equal(model.items.length, before);
  assert.equal(model.lastSeq, 1);
  assert.equal(model.rejectedFrames, 1);
});

test('the front-end carries no Glink-specific report layer', () => {
  // The workbench renders the Markdown pi wrote and nothing more: no charts,
  // citations, fact labels or other product vocabulary in its own sources.
  // （口径说明：bare 'artifact' 曾在此列，义务是禁止报告层的 artifact 画廊类
  // 产品词汇；§3.5 的子代理产物是已批准的独立产品面，有自己的边界与测试，
  // 因此禁令收窄为报告层的确切措辞。）
  const modules = [
    'api.mjs', 'markdown.mjs', 'run-reducer.mjs', 'run-stream.mjs', 'run-view.mjs',
    'run-list-view.mjs', 'ui.mjs', 'workspace.mjs', 'event-schema.gen.mjs',
  ];
  const sources = modules
    .map((name) => readFileSync(new URL(`../../web/modules/${name}`, import.meta.url), 'utf8'))
    .join('\n')
    .toLowerCase();
  for (const forbidden of ['citation', 'qualifier', 'renderchart','report-chart', 'mermaid', 'flowchart', 'report-artifact', 'artifactgallery', 'factlabel']) {
    assert.ok(!sources.includes(forbidden), `front-end must not carry ${forbidden}`);
  }
  // A chart-like fence stays plain code — the renderer has no diagram path.
  const html = renderMarkdown('```chart\n{"labels":["a"]}\n```');
  assert.match(html, /<pre><code class="language-chart">/);
  assert.ok(!html.includes('report-chart'));
});

test('the shipped front-end never parses terminal text', () => {
  const sources = ['workspace.mjs', 'run-reducer.mjs', 'run-stream.mjs', 'run-view.mjs', 'run-list-view.mjs']
    .map((name) => readFileSync(new URL(`../../web/modules/${name}`, import.meta.url), 'utf8'))
    .join('\n');
  // The guard is about terminal emulation / ANSI escapes, not the word
  // "terminal" itself (the model has terminal *statuses*).
  for (const forbidden of ['xterm', 'strip-ansi', 'stripansi', '\u001b[', 'escape_code']) {
    assert.ok(!sources.toLowerCase().includes(forbidden), `front-end must not reference ${forbidden}`);
  }
});

/*
 * Reconnect policy (GLINK-HTTP-01). The SSE endpoint answers an over-limit
 * request with 429 stream_limit, and `EventSource` gives up on a non-200
 * answer without exposing the status; the subscription therefore retries a
 * bounded number of times instead of reporting a permanent failure at once —
 * and instead of reconnecting forever.
 */

function failingSource(readyState = 2) {
  return {
    readyState,
    closed: false,
    listeners: new Map(),
    addEventListener(type, handler) {
      this.listeners.set(type, handler);
    },
    close() {
      this.closed = true;
      this.readyState = 2;
    },
  };
}

function retryingHarness() {
  const sources = [];
  const scheduled = [];
  const statuses = [];
  const subscription = createRunSubscription({
    runId: 'r1',
    eventsUrl: () => '/api/runs/r1/events',
    eventSourceFactory: () => {
      const source = failingSource();
      sources.push(source);
      return source;
    },
    onStatus: (status) => statuses.push(status),
    schedule: (callback, delayMs) => {
      scheduled.push({ callback, delayMs });
      return scheduled.length;
    },
  });
  return { sources, scheduled, statuses, subscription };
}

test('a refused stream is retried a bounded number of times, then fails', () => {
  const h = retryingHarness();
  assert.equal(h.sources.length, 1, '订阅立刻打开一次');
  assert.deepEqual(h.statuses, ['connecting']);

  const delays = [];
  for (let round = 0; round < STREAM_RETRY_DELAYS_MS.length; round += 1) {
    h.sources.at(-1).onerror();
    assert.equal(h.statuses.at(-1), 'reconnecting');
    const pending = h.scheduled.at(-1);
    delays.push(pending.delayMs);
    pending.callback();
    assert.equal(h.sources.length, round + 2, '重试打开一个新连接');
  }
  assert.deepEqual(delays, STREAM_RETRY_DELAYS_MS);

  h.sources.at(-1).onerror();
  assert.equal(h.statuses.at(-1), 'failed', '重试用尽后如实失败');
  assert.equal(h.scheduled.length, STREAM_RETRY_DELAYS_MS.length, '不得无限重连');
});

test('a retry that reaches live resets the retry budget', () => {
  const h = retryingHarness();
  h.sources[0].onerror();
  h.scheduled[0].callback();
  h.sources[1].onopen();
  assert.equal(h.statuses.at(-1), 'live');

  // A later drop gets the full ladder again.
  h.sources[1].onerror();
  assert.equal(h.scheduled[1].delayMs, STREAM_RETRY_DELAYS_MS[0]);
});

test('closing the subscription cancels a pending retry', () => {
  const h = retryingHarness();
  h.sources[0].onerror();
  assert.equal(h.scheduled.length, 1);
  h.subscription.close();
  assert.equal(h.subscription.status, 'closed');
  h.scheduled[0].callback();
  assert.equal(h.sources.length, 1, '关闭后不得再打开连接');
});

test('a dropped connection still reports reconnecting, not failed', () => {
  const h = retryingHarness();
  h.sources[0].readyState = 0; // CONNECTING: EventSource retries this itself
  h.sources[0].onerror();
  assert.equal(h.statuses.at(-1), 'reconnecting');
  assert.equal(h.scheduled.length, 0, '不重复调度：浏览器自己会重连');
});

/* The host clips an oversized authoritative message; the streamed deltas are
 * still the more complete record of what the model said (GLINK-INTEG-01). */
const LONG_ANSWER = '答'.repeat(20000);
const CLIPPED_ANSWER = `${LONG_ANSWER.slice(0, 16000)}\n…[truncated 4000 chars]`;

test('a truncated authoritative frame never shortens a longer streamed answer', () => {
  let model = createRunState({ runId: 'r1', prompt: '问题' });
  model = apply(model, frame(1, 'assistant_delta', { text: LONG_ANSWER }));
  model = apply(model, frame(2, 'assistant_message', { text: CLIPPED_ANSWER, truncated: true }));

  const answer = model.items.filter((item) => item.kind === 'text').at(-1);
  assert.equal(answer.text, LONG_ANSWER, '更完整的流式正文必须保留');
  assert.equal(answer.text.length, 20000);
  assert.equal(answer.closed, true, '终帧仍然收口');
  assert.equal(answer.truncated, true);
});

test('an untruncated authoritative frame still replaces the streamed text', () => {
  let model = createRunState({ runId: 'r1', prompt: '问题' });
  model = apply(model, frame(1, 'assistant_delta', { text: '流式正文' }));
  model = apply(model, frame(2, 'assistant_message', { text: '权威正文' }));

  const answer = model.items.filter((item) => item.kind === 'text').at(-1);
  assert.equal(answer.text, '权威正文', 'pi 的权威副本优先');
  assert.equal(answer.truncated, false);
});

test('a truncated frame still lands when nothing was streamed before it', () => {
  let model = createRunState({ runId: 'r1', prompt: '问题' });
  model = apply(model, frame(1, 'assistant_message', { text: CLIPPED_ANSWER, truncated: true }));
  const answer = model.items.filter((item) => item.kind === 'text').at(-1);
  assert.equal(answer.text, CLIPPED_ANSWER);
  assert.equal(answer.truncated, true);
});
