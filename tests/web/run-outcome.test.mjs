/**
 * Outcome states of the run view (GLINK-INTEG-01).
 *
 * `outcomeState` is the pure half of the run view: which connection/outcome card
 * the reader sees for a given model. A `finished` run whose model was cut off at
 * its output limit must say the delivery is incomplete — showing only "已完成"
 * claims more than the facts support.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';

import { outcomeState } from '../../web/modules/run-view.mjs';

test('a run cut off at the output limit is shown as an incomplete delivery', () => {
  const spec = outcomeState({ status: 'finished', stopReason: 'length', hasReport: true }, null);
  assert.ok(spec, '必须给出明确提示，不能当作普通完成');
  assert.equal(spec.title, '交付不完整');
  assert.match(spec.text, /截断|不完整/);
  assert.equal(spec.tone, 'slow', '沿用标准库的 warning 色调，不新造视觉');
  assert.ok(spec.icon && spec.live, '四个要件齐全：图标/标题/说明/下一步');
});

test('a normally finished run keeps no outcome card', () => {
  assert.equal(outcomeState({ status: 'finished', stopReason: null }, null), null);
  assert.equal(outcomeState({ status: 'finished', stopReason: 'no_report' }, null), null);
  assert.equal(outcomeState({ status: 'running' }, null), null);
});

test('the existing outcome states are unchanged', () => {
  assert.equal(outcomeState({ status: 'cancelled' }, null).key, 'stopped');
  assert.equal(outcomeState({ status: 'failed', error: 'boom' }, null).key, 'failed:boom');
  assert.equal(outcomeState({ status: 'running' }, 'reconnecting').key, 'slow');
  assert.equal(outcomeState({ status: 'running', hasReport: true }, 'unavailable').key, 'unavailable');
  // An expired length run with no connection problem still shows the notice.
  assert.equal(outcomeState({ status: 'finished', stopReason: 'length' }, 'live').key, 'incomplete');
});

/**
 * A follow-up whose session was never established (the agent refused
 * `session/resume`; run 71779b32e35a) must not promise that retrying continues
 * the conversation: the host fails closed, and the only next step is a new
 * research without the earlier context.
 */
test('a follow-up that could not reattach says so and offers a new research', () => {
  const spec = outcomeState({
    status: 'failed', parentRunId: 'parent1', sessionId: null,
    error: 'AcpRequestError: session/resume failed: -32603: Internal error (unknown event)',
  }, null);
  assert.equal(spec.title, '追问没能接上之前的研究');
  assert.equal(spec.tone, 'failed');
  assert.match(spec.text, /之前的研究和结果不受影响/);
  assert.match(spec.text, /新研究.*不带之前的上下文/);
  assert.doesNotMatch(spec.text, /重试/);
  assert.deepEqual(spec.actions, [['作为新研究开始', 'rerun']]);
  assert.match(spec.detail, /unknown event/);
  assert.ok(spec.key.startsWith('resume-failed:'));
});

test('a follow-up that failed after reattaching keeps the generic failure card', () => {
  const spec = outcomeState({ status: 'failed', parentRunId: 'parent1', sessionId: 's1', error: 'boom' }, null);
  assert.equal(spec.key, 'failed:boom');
  assert.equal(spec.title, '这次研究没有完成');
});
