/**
 * Paginated research history (M9).
 *
 * `GET /api/runs` is a page (newest first, opaque `nextCursor`), not the whole
 * history: the sidebar used to treat its response as everything, so past the
 * host's old 100-run clamp history vanished silently, and a follow-up chain cut
 * by the boundary was titled after one of its intermediate turns. These tests
 * drive the real `createWorkspace` against the stub DOM: the list must keep the
 * pages it already has, offer the next one, and complete a chain a page boundary
 * cut in half.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';

import { createWorkspace } from '../../web/modules/workspace.mjs';
import { createDomStub } from './dom-stub.mjs';

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

function runView(runId, prompt = runId, overrides = {}) {
  return {
    runId,
    status: 'finished',
    prompt,
    createdAt: 1,
    finishedAt: 2,
    durationSeconds: 1,
    sessionId: null,
    hasReport: false,
    stopReason: null,
    error: null,
    usage: null,
    eventCount: 0,
    parentRunId: null,
    ...overrides,
  };
}

function harness({ listRuns = null, getRun = null } = {}) {
  const dom = createDomStub();
  dom.install();
  const listCalls = [];
  const api = {
    me: async () => ({ mode: 'local', authenticated: false }),
    health: async () => ({ pi: { available: true }, configured: { model: true } }),
    config: async () => ({ model: 'test-model' }),
    listRuns: async (options = {}) => {
      listCalls.push(options);
      return listRuns ? listRuns(options) : { runs: [] };
    },
    getRun: async (runId) => {
      if (!getRun) throw new Error(`unexpected getRun(${runId})`);
      return { run: await getRun(runId) };
    },
    startRun: async () => ({ run: runView('new-run') }),
    stopRun: async () => ({ requested: true }),
    eventsUrl: (runId) => `/api/runs/${runId}/events`,
    reportUrl: (runId) => `/api/runs/${runId}/report`,
  };
  // A subscription must never be opened by these tests: every run here is
  // finished and the list view never streams.
  const eventSourceFactory = () => ({ addEventListener() {}, close() {} });
  const workspace = createWorkspace({ api, eventSourceFactory });
  return { dom, workspace, api, listCalls };
}

function navRows(dom) {
  const list = dom.getElement('run-list');
  return list.children.filter((child) => child.dataset.runId !== undefined);
}

function moreButton(dom) {
  return dom.getElement('run-list').children.find((child) => child.id === 'run-list-more') ?? null;
}

test('the sidebar keeps the pages it has and asks for the next one (M9)', async () => {
  const h = harness({
    listRuns: (options) =>
      options.cursor
        ? { runs: [runView('A', '最初的研究')], nextCursor: null, hasMore: false }
        : { runs: [runView('B', '最新的研究')], nextCursor: 'page-2', hasMore: true },
  });
  await h.workspace.init();

  assert.deepEqual(h.listCalls, [{ limit: 50 }], 'the first page is requested with an explicit size');
  assert.deepEqual(navRows(h.dom).map((row) => row.dataset.runId), ['B']);
  const more = moreButton(h.dom);
  assert.ok(more, 'a non-final page has to expose a way to load the rest');
  assert.equal(more.disabled, false);

  more.dispatch('click', {});
  await tick();

  assert.deepEqual(h.listCalls.at(-1), { limit: 50, cursor: 'page-2' });
  assert.deepEqual(
    navRows(h.dom).map((row) => row.dataset.runId),
    ['B', 'A'],
    'the new page is appended to the loaded history, not swapped in for it',
  );
  assert.equal(moreButton(h.dom), null, 'the entry disappears once the last page is in');
  assert.deepEqual(h.workspace.runs.map((run) => run.runId), ['B', 'A']);
});

test('a chain cut by a page boundary is completed instead of surfacing a middle turn', async () => {
  const chain = {
    M: runView('M', '追问·第一轮', { parentRunId: 'R' }),
    R: runView('R', '最初的研究问题'),
  };
  const h = harness({
    listRuns: () => ({
      runs: [runView('L', '追问·第二轮', { parentRunId: 'M' })],
      nextCursor: null,
      hasMore: false,
    }),
    getRun: async (runId) => chain[runId],
  });
  await h.workspace.init();

  assert.deepEqual(navRows(h.dom).map((row) => row.dataset.runId), ['L']);
  assert.deepEqual(
    navRows(h.dom).map((row) => row.children[1].textContent),
    ['最初的研究问题'],
    'the row is titled by the chain root, not by the newest (or an intermediate) turn',
  );
});

test('an unreachable ancestor leaves the newest turn visible instead of hiding the chain', async () => {
  const h = harness({
    listRuns: () => ({
      runs: [runView('L', '追问·第二轮', { parentRunId: 'gone' })],
      nextCursor: null,
      hasMore: false,
    }),
    getRun: async () => {
      throw Object.assign(new Error('unknown_run'), { status: 404, code: 'unknown_run' });
    },
  });
  await h.workspace.init();

  // A 404 (deleted run, another tenant) must not make the run vanish: the row
  // stays, titled by the oldest turn that is actually loadable.
  assert.deepEqual(navRows(h.dom).map((row) => row.dataset.runId), ['L']);
  assert.equal(navRows(h.dom)[0].children[1].textContent, '追问·第二轮');
});
