import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRunState, reduceAll } from '../../web/modules/run-reducer.mjs';
import { collectSources } from '../../web/modules/progress-panel.mjs';
import { describeTool, planEntries, stageText, describeChild } from '../../web/modules/present.mjs';

const frames = readFileSync(new URL('../fixtures/events/acp-presentation.jsonl', import.meta.url), 'utf8')
  .trim().split('\n').map(JSON.parse);
const model = reduceAll(createRunState({ runId: 'acp-fixture' }), frames);

test('ACP fixture: search, fetch title, todos, dispatch, thinking and usage survive the consumer', () => {
  const search = model.items.find((item) => item.name === 'web_search');
  assert.equal(describeTool(search.name, search.args, search.presentation).object,
    'AI drug discovery clinical trial results 等 2 条');
  const { queries, pages } = collectSources(model.items);
  assert.deepEqual(queries, ['AI drug discovery clinical trial results', '创新药临床数据 2025']);
  assert.equal(pages.length, 1);
  assert.equal(pages[0].title, 'Clinical trial results for drug discovery');
  const plan = model.items.find((item) => item.name === 'todo_write');
  assert.deepEqual(planEntries(plan.args), [
    { content: '核验临床试验', status: 'in_progress' },
    { content: '整理证据', status: 'pending' },
  ]);
  assert.equal(describeTool(plan.name, plan.args, plan.presentation).object, '核验临床试验 · 0/2 项');
  const stage = model.items.find((item) => item.kind === 'stage');
  assert.deepEqual(stage.entries, [{ content: '资料核验', status: 'in_progress', priority: 'high' }]);
  assert.match(stageText(stage), /资料核验/);
  assert.equal(describeChild(model.items.find((item) => item.kind === 'subagent')).object,
    'AI 制药市场规模与产业格局');
  assert.equal(model.items.find((item) => item.kind === 'thinking').text, '先核对一手来源');
  assert.equal(model.usage.totalTokens, 1200);
});

test('old frames and adjacent malformed inputs degrade without invented sources or plans', () => {
  const old = reduceAll(createRunState(), [
    { ...frames[4], seq: 1, payload: { ...frames[4].payload, presentation: undefined } },
    { ...frames[5], seq: 2, payload: { ...frames[5].payload, result: 'Fetched (HTTP 200)\n\n## Main\n\nText.' } },
  ]);
  assert.equal(collectSources(old.items).pages[0].title, undefined);
  assert.deepEqual(planEntries({ todos: ['broken', '{"content":"OK","status":"done"}'] }),
    [{ content: 'OK', status: 'done' }]);
  assert.equal(collectSources([{ kind: 'tool', name: 'web_fetch', args: { url: 'javascript:alert(1)' } }]).pages.length, 0);
});
