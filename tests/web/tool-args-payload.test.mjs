/**
 * Cross-layer payload contract, from the consumer side.
 *
 * `tests/fixtures/tool-args-projection.json` is produced by the shared corpus:
 * the Python regression asserts the projection emits exactly these product
 * events, and this test feeds the same events through the front-end model and
 * `collectSources`. A search tool's `queries` and a fetch tool's `urls` must
 * survive as arrays — flattening them to one string collapsed a multi-query run
 * to a single source after the payload crossed the layer boundary.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import { EVENT_VERSIONS } from '../../web/modules/event-schema.gen.mjs';
import { createRunState, reduce } from '../../web/modules/run-reducer.mjs';
import { collectSources } from '../../web/modules/progress-panel.mjs';
import { describeTool } from '../../web/modules/present.mjs';

const corpus = JSON.parse(
  readFileSync(new URL('../fixtures/tool-args-projection.json', import.meta.url), 'utf8'),
);

function modelFromProjectedEvents(events) {
  let model = createRunState({ runId: 'r1' });
  let seq = 0;
  for (const event of events) {
    seq += 1;
    model = reduce(model, { ...event, v: EVENT_VERSIONS[event.type], seq, runId: 'r1' });
  }
  return model;
}

test('the projected payload keeps multi-query and multi-URL arrays', () => {
  const model = modelFromProjectedEvents(corpus.projected);
  const searches = model.items.filter((item) => item.name === 'web_search');
  assert.equal(searches.length, 1);
  assert.ok(Array.isArray(searches[0].args.queries), 'queries must stay an array');
  assert.deepEqual(searches[0].args.queries, corpus.expected_sources.queries);
  const fetches = model.items.filter((item) => item.name === 'fetch_content');
  assert.ok(Array.isArray(fetches[0].args.urls), 'urls must stay an array');
  assert.deepEqual(
    fetches[0].args.urls,
    corpus.expected_sources.pages.map((page) => page.href),
  );
});

test('collectSources recovers every query and every page from the projected events', () => {
  const model = modelFromProjectedEvents(corpus.projected);
  const sources = collectSources(model.items);
  assert.deepEqual(sources.queries, corpus.expected_sources.queries);
  assert.deepEqual(sources.pages, corpus.expected_sources.pages);
  assert.equal(sources.pages.length, 2, 'both URLs must survive the projection');
});

test('a flattened payload would have collapsed the sources (negative case)', () => {
  // This is the shape the old projection emitted: one joined string.
  const flattened = {
    ...corpus.projected[0],
    payload: {
      ...corpus.projected[0].payload,
      args: { ...corpus.projected[0].payload.args, queries: '跨境资金监管新规\n跨境资金监管 处罚案例' },
    },
  };
  const model = modelFromProjectedEvents([
    flattened,
    corpus.projected[1],
    corpus.projected[2],
  ]);
  const sources = collectSources(model.items);
  assert.deepEqual(
    sources.queries,
    ['跨境资金监管新规\n跨境资金监管 处罚案例'],
    'a string payload degrades to one query — the regression this corpus guards',
  );
});

test('the tool description reads the first query and reports the count', () => {
  const args = corpus.projected[0].payload.args;
  const described = describeTool('web_search', args);
  assert.match(described.object, /跨境资金监管新规/);
  assert.match(described.object, /2 条|等 2/);
});
