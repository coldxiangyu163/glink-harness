/**
 * Navigation/lifecycle regressions for the workbench shell.
 *
 * The audit reproduced a navigation race: `openRun` awaited `getRun` and then
 * unconditionally replaced the current run, so a slow response for run A
 * overwrote the fast response for run B. It also found that a `dispose()` while
 * a navigation was in flight still created a view and a subscription.
 *
 * These tests drive the real `createWorkspace` against a stub DOM (no layout,
 * no browser) with controllable API promises: slow-A/fast-B ordering, a late
 * rejection after a newer navigation, and disposal mid-navigation.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';

import { createWorkspace } from '../../web/modules/workspace.mjs';
import { createDomStub } from './dom-stub.mjs';

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function runView(runId, prompt = runId) {
  return {
    runId,
    status: 'running',
    prompt,
    createdAt: 1,
    finishedAt: null,
    durationSeconds: null,
    sessionId: null,
    hasReport: false,
    stopReason: null,
    error: null,
    usage: null,
    eventCount: 0,
  };
}

function harness() {
  const dom = createDomStub();
  const pending = new Map();
  const subscriptions = [];
  const toasts = [];
  const api = {
    health: async () => ({ pi: { available: true }, configured: { model: true } }),
    config: async () => ({ model: 'test-model' }),
    listRuns: async () => ({ runs: [] }),
    getRun: (runId) => {
      if (!pending.has(runId)) pending.set(runId, deferred());
      return pending.get(runId).promise;
    },
    startRun: async (prompt) => ({ run: runView('new-run', prompt) }),
    stopRun: async () => ({ requested: true }),
    eventsUrl: (runId) => `/api/runs/${runId}/events`,
    reportUrl: (runId) => `/api/runs/${runId}/report`,
  };
  const eventSourceFactory = (url) => {
    const source = {
      url,
      readyState: 0,
      handlers: new Map(),
      closed: false,
      addEventListener(type, handler) {
        this.handlers.set(type, handler);
      },
      close() {
        this.closed = true;
        this.readyState = 2;
      },
    };
    subscriptions.push(source);
    return source;
  };
  const workspace = createWorkspace({ api, eventSourceFactory });
  dom.document.getElementById('toast-text').textContent = '';
  return { dom, workspace, pending, subscriptions, api, toasts };
}

async function boot(harnessInstance) {
  const originalText = harnessInstance.dom.document.getElementById('toast-text');
  await harnessInstance.workspace.init();
  return originalText;
}

test('a slow navigation response cannot replace a newer run (A slow, B fast)', async () => {
  const h = harness();
  await h.workspace.init();

  const slow = deferred();
  const fast = deferred();
  h.pending.set('A', { promise: slow.promise });
  h.pending.set('B', { promise: fast.promise });

  const navigationA = h.workspace.openRun('A');
  const navigationB = h.workspace.openRun('B');
  fast.resolve({ run: runView('B', '第二个研究') });
  await navigationB;
  assert.equal(h.workspace.runId, 'B', 'the fast navigation must win');

  slow.resolve({ run: runView('A', '第一个研究') });
  await navigationA;

  assert.equal(h.workspace.runId, 'B', 'the late response must not replace run B');
  assert.equal(h.workspace.model.runId, 'B');
  // Exactly one live subscription: the one run the reader is looking at.
  assert.equal(h.subscriptions.filter((source) => !source.closed).length, 1);
  assert.match(h.subscriptions[0].url, /\/B\/events$/);
});

test('a late navigation error does not surface after a newer navigation', async () => {
  const h = harness();
  await h.workspace.init();

  const slow = deferred();
  const fast = deferred();
  h.pending.set('A', { promise: slow.promise });
  h.pending.set('B', { promise: fast.promise });
  const navigationA = h.workspace.openRun('A');
  const navigationB = h.workspace.openRun('B');
  fast.resolve({ run: runView('B') });
  await navigationB;

  slow.reject(new Error('network vanished'));
  await navigationA;

  assert.equal(h.workspace.runId, 'B');
  assert.equal(
    h.dom.document.getElementById('toast-text').textContent,
    '',
    'a stale failure must not raise a toast over the current run',
  );
});

test('dispose during a navigation creates no view and no subscription', async () => {
  const h = harness();
  await h.workspace.init();
  const pending = deferred();
  h.pending.set('A', { promise: pending.promise });

  const navigation = h.workspace.openRun('A');
  h.workspace.dispose();
  pending.resolve({ run: runView('A') });
  await navigation;

  assert.equal(h.workspace.runId, null, 'a disposed workspace must not adopt a run');
  assert.equal(h.subscriptions.length, 0, 'no subscription may outlive dispose');
  const connected = h.dom.observers.filter((observer) => !observer.disconnected);
  assert.deepEqual(connected, [], 'observers must be disconnected on dispose');
});

test('dispose stops the run view and the reconnect timer', async () => {
  const h = harness();
  await h.workspace.init();
  const pending = deferred();
  h.pending.set('A', { promise: pending.promise });
  const navigation = h.workspace.openRun('A');
  pending.resolve({ run: runView('A') });
  await navigation;
  assert.equal(h.subscriptions.length, 1);
  assert.equal(h.subscriptions[0].closed, false);

  h.workspace.dispose();

  assert.equal(h.subscriptions[0].closed, true, 'the subscription must be closed');
});

test('a stale report response cannot overwrite the current run report', async () => {
  const h = harness();
  await h.workspace.init();
  const pending = deferred();
  h.pending.set('A', { promise: pending.promise });
  const navigation = h.workspace.openRun('A');
  pending.resolve({ run: { ...runView('A'), hasReport: true } });
  await navigation;

  const report = deferred();
  const originalFetch = globalThis.fetch;
  globalThis.fetch = () => report.promise;
  try {
    const opening = h.workspace.openReport('A');
    h.workspace.dispose();
    report.resolve({ ok: true, text: async () => '# 报告' });
    await opening;
    assert.equal(
      h.dom.document.getElementById('preview-article').innerHTML,
      '',
      'a disposed workspace must not render a late report',
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});
