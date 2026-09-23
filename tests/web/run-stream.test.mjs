/**
 * Node regressions for the front-end event model.
 *
 * `run-stream.mjs` is the front-end half of the event contract, so it is tested
 * where it is cheapest: without a browser. The DOM renderer is covered
 * separately by the browser regression (tests/browser/run-smoke.mjs).
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import {
  applyEvent,
  applyEvents,
  createRunModel,
  runSummary,
} from '../../web/modules/run-stream.mjs';
import { renderMarkdown } from '../../web/modules/markdown.mjs';

const frame = (seq, type, payload = {}) => ({ seq, type, runId: 'r1', payload });

test('markdown renderer is importable without a document', () => {
  const html = renderMarkdown('# 标题\n\n正文 **加粗**');
  assert.match(html, /<h1[^>]*>标题<\/h1>/);
  assert.match(html, /<strong>加粗<\/strong>/);
});

test('thinking and text deltas accumulate into blocks per turn', () => {
  let model = createRunModel({ runId: 'r1', prompt: '问题' });
  model = applyEvents(model, [
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
  let model = createRunModel({ runId: 'r1' });
  model = applyEvents(model, [
    frame(1, 'tool_started', { toolCallId: 'c1', toolName: 'bash', args: { command: 'ls -la' } }),
    frame(2, 'tool_updated', { toolCallId: 'c1', toolName: 'bash', output: 'total 8\n' }),
    // pi sends the accumulated output so far, so the second frame repeats the
    // first one and adds a line (docs/rpc.md: "not just the delta").
    frame(3, 'tool_updated', { toolCallId: 'c1', toolName: 'bash', output: 'total 8\nreport.md\n' }),
  ]);
  const running = model.items.at(-1);
  assert.equal(running.status, 'running');
  assert.equal(running.output, 'total 8\nreport.md\n');

  model = applyEvent(model, frame(4, 'tool_finished', { toolCallId: 'c1', toolName: 'bash', result: 'done', isError: false }));
  const done = model.items.at(-1);
  assert.equal(done.status, 'done');
  assert.equal(done.result, 'done');

  model = applyEvent(model, frame(5, 'tool_started', { toolCallId: 'c2', toolName: 'read', args: {} }));
  model = applyEvent(model, frame(6, 'tool_finished', { toolCallId: 'c2', toolName: 'read', result: 'ENOENT', isError: true }));
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
  let model = createRunModel({ runId: 'r1' });
  model = applyEvent(model, frame(1, 'tool_started', { toolCallId: 'c1', toolName: 'bash', args: { command: 'env' } }));
  snapshots.forEach((output, index) => {
    model = applyEvent(model, frame(index + 2, 'tool_updated', { toolCallId: 'c1', toolName: 'bash', output }));
  });
  const tool = model.items.at(-1);
  assert.equal(tool.output, snapshots.at(-1));
  assert.ok(tool.output.length < snapshots.reduce((sum, s) => sum + s.length, 0), 'replacement must be shorter than concatenation');
  assert.ok(!tool.output.includes('\n\n\n'), 'accumulated prefixes must not be concatenated');
  assert.equal(tool.output.match(/PI vars/g)?.length, 1);
});

test('the audited run ebf75f590601 replays with replacement semantics (real fixture)', () => {
  // `tests/fixtures/tool-update-frames.jsonl` is the verbatim frame sequence
  // of that run's bash tool (seq 85–89) as stored in events.jsonl.
  const fixture = readFileSync(new URL('../fixtures/tool-update-frames.jsonl', import.meta.url), 'utf8');
  const frames = fixture.split('\n').filter((line) => line.trim()).map((line) => JSON.parse(line));
  assert.equal(frames.length, 5);
  const updates = frames.filter((event) => event.type === 'tool_updated');
  assert.deepEqual(updates.map((event) => event.payload.output.length), [0, 53, 495]);
  assert.ok(updates[1].payload.output.startsWith(updates[0].payload.output));
  assert.ok(updates[2].payload.output.startsWith(updates[1].payload.output));

  // While the tool runs, each update replaces the card body.
  let model = createRunModel({ runId: 'ebf75f590601' });
  model = applyEvents(model, frames.filter((event) => event.type !== 'tool_finished'));
  let tool = model.items.find((item) => item.kind === 'tool');
  assert.equal(tool.output, updates[2].payload.output, 'the last snapshot is the display');
  assert.equal(tool.output.length, 495);
  const concatenated = updates.map((event) => event.payload.output).join('');
  assert.equal(concatenated.length, 548, 'appending the frames is what the old code did');
  assert.notEqual(tool.output, concatenated);
  assert.equal(tool.output.match(/workspaces\/ebf75f590601/g)?.length, 1, 'the prefix must appear once');

  // The terminal frame repeats that same text as ``result``: the card must
  // render it once, not once per field.
  model = applyEvents(model, frames);
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
    let model = createRunModel({ runId: 'r1' });
    model = applyEvents(model, [
      frame(1, 'tool_started', { toolCallId: 'c1', toolName: 'bash', args: {} }),
      frame(2, type, { status: type === 'run_failed' ? 'failed' : 'cancelled' }),
    ]);
    const tool = model.items.find((item) => item.kind === 'tool');
    assert.equal(tool.status, expected, `${type} must map an unfinished tool to ${expected}`);
    assert.notEqual(tool.status, 'done');
  }

  // A tool that did see its own terminal frame keeps that state.
  let model = createRunModel({ runId: 'r1' });
  model = applyEvents(model, [
    frame(1, 'tool_started', { toolCallId: 'c1', toolName: 'bash', args: {} }),
    frame(2, 'tool_finished', { toolCallId: 'c1', toolName: 'bash', result: 'ok', isError: false }),
    frame(3, 'run_failed', { status: 'failed' }),
  ]);
  assert.equal(model.items.find((item) => item.kind === 'tool').status, 'done');
});

test('replayed or duplicate frames never rewrite the model', () => {
  let model = createRunModel({ runId: 'r1' });
  model = applyEvents(model, [
    frame(5, 'assistant_delta', { text: '一次' }),
    frame(5, 'assistant_delta', { text: '一次' }), // duplicate replay
    frame(3, 'assistant_delta', { text: '旧帧' }), // stale replay
  ]);
  assert.equal(model.items.at(-1).text, '一次');
  assert.equal(model.ignoredFrames, 2);
  assert.equal(model.lastSeq, 5);
});

test('a gap in sequence numbers is recorded, not hidden', () => {
  let model = createRunModel({ runId: 'r1' });
  model = applyEvents(model, [
    frame(1, 'turn_started'),
    frame(4, 'assistant_delta', { text: '断档后的文本' }),
  ]);
  assert.equal(model.gaps, 1);
  assert.equal(model.items.at(-1).text, '断档后的文本');
  assert.equal(runSummary(model).gaps, 1);
});

test('the terminal frame decides the run status and closes running tools', () => {
  let model = createRunModel({ runId: 'r1' });
  model = applyEvents(model, [
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
  let model = createRunModel({ runId: 'r1' });
  model = applyEvents(model, [
    frame(1, 'turn_started'),
    frame(2, 'assistant_delta', { text: '被截断的' }),
    frame(3, 'assistant_message', { text: '被截断的完整回答' }),
  ]);
  assert.equal(model.items.at(-1).text, '被截断的完整回答');
  assert.equal(model.items.at(-1).closed, true);

  // A closed block must not be extended by a later delta.
  model = applyEvent(model, frame(4, 'assistant_delta', { text: '追加' }));
  assert.equal(model.items.at(-1).text, '追加');
  assert.equal(model.items.at(-2).text, '被截断的完整回答');
});

test('notices and stages carry readable labels', () => {
  let model = createRunModel({ runId: 'r1' });
  model = applyEvents(model, [
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
  let model = createRunModel({ runId: 'r1' });
  model = applyEvent(
    model,
    frame(1, 'usage_reported', {
      usage: { input: 100, output: 0, totalTokens: 100, requests: 1 },
      requestUsage: { input: 100, output: 0, totalTokens: 100 },
    }),
  );
  assert.equal(model.usage.totalTokens, 100);

  model = applyEvent(
    model,
    frame(2, 'usage_reported', {
      usage: { input: 300, output: 0, totalTokens: 300, requests: 2 },
      requestUsage: { input: 200, output: 0, totalTokens: 200 },
    }),
  );
  assert.equal(model.usage.totalTokens, 300, 'the frame is the run total, not a delta');
  assert.equal(model.usage.requests, 2);

  // A duplicate frame at the same seq is ignored by the watermark (no inflation).
  model = applyEvent(
    model,
    frame(2, 'usage_reported', {
      usage: { input: 300, output: 0, totalTokens: 300, requests: 2 },
    }),
  );
  assert.equal(model.usage.totalTokens, 300);
  assert.equal(model.ignoredFrames, 1);

  // The terminal frame carries the authoritative final total.
  model = applyEvent(
    model,
    frame(3, 'run_finished', {
      status: 'finished',
      usage: { input: 300, output: 0, totalTokens: 300, requests: 2 },
    }),
  );
  assert.equal(model.usage.totalTokens, 300);
});

test('a run with no reported usage shows none rather than a fabricated value', () => {
  let model = createRunModel({ runId: 'r1' });
  model = applyEvent(model, frame(1, 'run_finished', { status: 'finished' }));
  assert.equal(model.usage, null);
});

test('unknown event types are ignored, not rendered as noise', () => {
  let model = createRunModel({ runId: 'r1' });
  const before = model.items.length;
  model = applyEvents(model, [frame(1, 'future_event_type', { anything: 1 })]);
  assert.equal(model.items.length, before);
  assert.equal(model.lastSeq, 1);
});

test('the front-end carries no Glink-specific report layer', () => {
  // The workbench renders the Markdown pi wrote and nothing more: no charts,
  // citations, fact labels or other product vocabulary in its own sources.
  const modules = ['api.mjs', 'markdown.mjs', 'run-stream.mjs', 'run-view.mjs', 'ui.mjs', 'workspace.mjs'];
  const sources = modules
    .map((name) => readFileSync(new URL(`../../web/modules/${name}`, import.meta.url), 'utf8'))
    .join('\n')
    .toLowerCase();
  for (const forbidden of ['citation', 'qualifier', 'renderchart','report-chart', 'mermaid', 'flowchart', 'artifact', 'factlabel']) {
    assert.ok(!sources.includes(forbidden), `front-end must not carry ${forbidden}`);
  }
  // A chart-like fence stays plain code — the renderer has no diagram path.
  const html = renderMarkdown('```chart\n{"labels":["a"]}\n```');
  assert.match(html, /<pre><code class="language-chart">/);
  assert.ok(!html.includes('report-chart'));
});

test('the shipped front-end never parses terminal text', () => {
  const sources = ['workspace.mjs', 'run-stream.mjs', 'run-view.mjs']
    .map((name) => readFileSync(new URL(`../../web/modules/${name}`, import.meta.url), 'utf8'))
    .join('\n');
  // The guard is about terminal emulation / ANSI escapes, not the word
  // "terminal" itself (the model has terminal *statuses*).
  for (const forbidden of ['xterm', 'strip-ansi', 'stripansi', '\u001b[', 'escape_code']) {
    assert.ok(!sources.toLowerCase().includes(forbidden), `front-end must not reference ${forbidden}`);
  }
});
