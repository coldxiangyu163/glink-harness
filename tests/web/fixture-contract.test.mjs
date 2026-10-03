/**
 * Fold contract tests of the web reducer over the authoritative event sample
 * library (ADR 0021): every `tests/fixtures/events/*.jsonl` sample is folded
 * through `run-reducer.mjs` and the resulting snapshot must match the `web`
 * section of `manifest.json`. The Python-side contract test
 * (`tests/test_event_fixture_contract.py`) folds the same samples into
 * trajectory projection rows against the same manifest, so both consumers are
 * pinned to one shared expectation — a drift on either side turns red here or
 * there, never silently apart.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readdirSync, readFileSync } from 'node:fs';

import { childrenPanel, createRunState, reduceAll } from '../../web/modules/run-reducer.mjs';

const FIXTURE_DIR = new URL('../fixtures/events/', import.meta.url);
const MANIFEST = JSON.parse(readFileSync(new URL('manifest.json', FIXTURE_DIR), 'utf8'));
const SAMPLES = Object.fromEntries(Object.entries(MANIFEST).filter(([name]) => !name.startsWith('$')));

function load(name) {
  // `#` lines are the fixture's comment header (tests/fixtures/events/README.md).
  return readFileSync(new URL(`${name}.jsonl`, FIXTURE_DIR), 'utf8')
    .split('\n')
    .filter((line) => line.trim() && !line.startsWith('#'))
    .map((line) => JSON.parse(line));
}

//: Supported `web` expectation keys → how each is read from the folded state.
//: A key absent from the manifest entry is simply not asserted for that sample.
const INVARIANTS = {
  status: (state) => state.status,
  texts: (state) => state.items.filter((item) => item.kind === 'text').map((item) => item.text),
  textTruncated: (state) =>
    state.items.filter((item) => item.kind === 'text').map((item) => item.truncated === true),
  toolCount: (state) => state.items.filter((item) => item.kind === 'tool').length,
  toolStatuses: (state) => state.items.filter((item) => item.kind === 'tool').map((item) => item.status),
  toolResults: (state) => state.items.filter((item) => item.kind === 'tool').map((item) => item.result),
  notices: (state) =>
    state.items.filter((item) => item.kind === 'notice').map((item) => ({ level: item.level, message: item.message })),
  turnCount: (state) => state.items.filter((item) => item.kind === 'turn').length,
  rejectedFrames: (state) => state.rejectedFrames,
  ignoredFrames: (state) => state.ignoredFrames,
  gaps: (state) => state.gaps,
  sessionId: (state) => state.sessionId,
  model: (state) => state.model,
  hasReport: (state) => state.hasReport,
  stopReason: (state) => state.stopReason,
  error: (state) => state.error,
  usageTotalTokens: (state) => state.usage?.totalTokens,
  deliverables: (state) =>
    state.deliverables.map(({ path, status, available }) => ({ path, status, available })),
  children: (state) => childrenPanel(state),
};

test('the manifest only uses invariant keys this suite knows how to check', () => {
  for (const [name, entry] of Object.entries(SAMPLES)) {
    for (const key of Object.keys(entry.web)) {
      assert.ok(key in INVARIANTS, `${name}: unknown web invariant ${key}`);
    }
    assert.ok(entry.trajectory && Array.isArray(entry.trajectory.milestones), name);
  }
});

test('the manifest covers every fixture on disk', () => {
  const onDisk = readdirSync(FIXTURE_DIR)
    .filter((file) => file.endsWith('.jsonl'))
    .map((file) => file.slice(0, -6));
  assert.deepEqual(onDisk.sort(), Object.keys(SAMPLES).sort());
});

for (const [name, entry] of Object.entries(SAMPLES)) {
  test(`fixture ${name}: reducer snapshot matches the manifest`, () => {
    const frames = load(name);
    const state = reduceAll(createRunState({ runId: frames[0].runId }), frames);
    for (const [key, expected] of Object.entries(entry.web)) {
      assert.deepEqual(INVARIANTS[key](state), expected, `${name}: ${key}`);
    }
  });
}

test('the shared core invariants agree between the web and trajectory sections', () => {
  for (const [name, entry] of Object.entries(SAMPLES)) {
    const snapshot = entry.trajectory.snapshot;
    if (snapshot === null) {
      assert.equal(entry.web.status, 'running', name);
      assert.deepEqual(entry.web.texts, [], name);
      continue;
    }
    assert.equal(entry.web.status, snapshot.status, name);
    assert.deepEqual(entry.web.texts, snapshot.texts, `${name}: 正文不变量两侧必须一致`);
    assert.equal(entry.web.toolCount, snapshot.toolCount, name);
  }
});
