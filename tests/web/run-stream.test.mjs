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
import { summarizeArgs } from '../../web/modules/run-view.mjs';
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
    frame(3, 'tool_updated', { toolCallId: 'c1', toolName: 'bash', output: 'report.md\n' }),
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

test('unknown event types are ignored, not rendered as noise', () => {
  let model = createRunModel({ runId: 'r1' });
  const before = model.items.length;
  model = applyEvents(model, [frame(1, 'future_event_type', { anything: 1 })]);
  assert.equal(model.items.length, before);
  assert.equal(model.lastSeq, 1);
});

test('tool argument summaries prefer the interesting keys and stay bounded', () => {
  const summary = summarizeArgs({ content: 'x'.repeat(200), command: 'ls -la', cwd: '/tmp' });
  assert.match(summary, /command: ls -la/);
  assert.ok(summary.length < 400, 'summary must stay short');
  assert.equal(summarizeArgs({}), '');
  assert.equal(summarizeArgs(null), '');
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
