/**
 * Run-list state: accumulating `/api/runs` pages and merging chains (M9).
 *
 * The host answers one page at a time; this module is the pure half of the
 * sidebar's "load more" — what a page does to the runs already loaded, and how
 * a follow-up chain is folded into one row over the loaded set.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';

import { emptyRunList, groupRunChains, mergeRunPage } from '../../web/modules/run-list-state.mjs';

const run = (runId, overrides = {}) => ({
  runId,
  prompt: runId,
  parentRunId: null,
  hasReport: false,
  ...overrides,
});

test('an empty list has nothing loaded and nothing to ask for', () => {
  assert.deepEqual(emptyRunList(), { runs: [], cursor: null, hasMore: false });
});

test('a page is appended and carries the cursor for the next one', () => {
  const first = mergeRunPage(emptyRunList(), {
    runs: [run('b'), run('a')],
    nextCursor: 'cursor-1',
    hasMore: true,
  });
  assert.deepEqual(first.runs.map((item) => item.runId), ['b', 'a']);
  assert.equal(first.cursor, 'cursor-1');
  assert.equal(first.hasMore, true);

  const second = mergeRunPage(first, {
    runs: [run('c')],
    nextCursor: null,
    hasMore: false,
  });
  assert.deepEqual(second.runs.map((item) => item.runId), ['b', 'a', 'c']);
  assert.equal(second.cursor, null);
  assert.equal(second.hasMore, false);
});

test('a re-sent run never doubles a row', () => {
  const first = mergeRunPage(emptyRunList(), { runs: [run('a')], nextCursor: 'c', hasMore: true });
  const second = mergeRunPage(first, { runs: [run('a'), run('b')], nextCursor: null, hasMore: false });
  assert.deepEqual(second.runs.map((item) => item.runId), ['a', 'b']);
});

test('a page with no pagination metadata keeps the loaded cursor and flag', () => {
  const first = mergeRunPage(emptyRunList(), { runs: [run('a')], nextCursor: 'c', hasMore: true });
  // Older hosts answer `{"runs": […]}` only; such a page must not silently
  // discard the cursor the list already holds.
  const second = mergeRunPage(first, { runs: [run('b')] });
  assert.deepEqual(second.runs.map((item) => item.runId), ['a', 'b']);
  assert.equal(second.cursor, 'c');
  assert.equal(second.hasMore, true);
});

test('a whole follow-up chain folds into one row titled by its root', () => {
  const { rows, unresolvedParents } = groupRunChains([
    run('leaf', { prompt: '追问', parentRunId: 'mid' }),
    run('mid', { prompt: '中期', parentRunId: 'root' }),
    run('root', { prompt: '最初的问题' }),
    run('solo', { prompt: '另一项研究' }),
  ]);
  assert.deepEqual(rows.map((row) => row.run.runId), ['leaf', 'solo']);
  assert.deepEqual(rows.map((row) => row.title), ['最初的问题', '另一项研究']);
  assert.deepEqual(unresolvedParents, []);
});

test('a chain missing its root names the parent that has to be fetched', () => {
  const { rows, unresolvedParents } = groupRunChains([
    run('leaf', { prompt: '追问', parentRunId: 'mid' }),
    run('mid', { prompt: '中期', parentRunId: 'root' }),
  ]);
  assert.deepEqual(rows.map((row) => row.run.runId), ['leaf']);
  assert.equal(rows[0].title, '中期');
  assert.deepEqual(unresolvedParents, ['root']);
});

test('reports keep pointing at the chain leaf over the loaded set', () => {
  const { reports } = groupRunChains([
    run('leaf', { parentRunId: 'root', hasReport: true }),
    run('root', { hasReport: true }),
    run('quiet'),
  ]);
  assert.deepEqual(
    reports.map((entry) => [entry.run.runId, entry.latestRunId]),
    [['leaf', 'leaf'], ['root', 'leaf']],
  );
});
