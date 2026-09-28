/**
 * Node regressions for the ChildRun (subagent) front-end model and wording.
 *
 * The typed subagent lifecycle (docs/architecture.md §3.5) replaces generic
 * tool cards for dispatches: `subagent_started` opens a dispatch card and a
 * ChildRun entry, `subagent_finished` settles every child of one call, and the
 * terminal frame's `children` summary is the replay-time source of truth.
 * Historical logs in the old format (toolName=subagent generic tool cards)
 * must keep rendering as plain tool cards — the read side never rewrites
 * history.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';

import { EVENT_VERSIONS } from '../../web/modules/event-schema.gen.mjs';
import {
  childrenPanel,
  createRunState,
  reduce,
  reduceAll,
} from '../../web/modules/run-reducer.mjs';
import { createRunSubscription } from '../../web/modules/run-stream.mjs';
import {
  childCardState,
  childFacts,
  childLabel,
  childProgressText,
  childStateLabel,
  childStatusText,
  childrenPanelTitle,
  describeChild,
  describeTool,
} from '../../web/modules/present.mjs';

const frame = (seq, type, payload = {}) => ({ seq, type, runId: 'r1', payload });

// 手写帧按当前 schema 版本盖 v（帧自带的 v 保留）；版本不符的拒收由
// run-reducer.test.mjs 覆盖。
const stamp = (event) => ({ v: EVENT_VERSIONS[event.type], ...event });
const apply = (model, event) => reduce(model, stamp(event));
const applyAll = (model, events) => reduceAll(model, events.map(stamp));

test('subagent_started opens a ChildRun and a dispatch card; finished settles every child of the call', () => {
  let model = createRunState({ runId: 'r1' });
  model = applyAll(model, [
    // One pi-family call can dispatch several children (args.tasks); each task
    // gets its own started frame and its own card.
    frame(1, 'subagent_started', { n: 0, toolCallId: 'c1', agent: 'researcher', task: '核查跨境资金监管' }),
    frame(2, 'subagent_started', { n: 1, toolCallId: 'c1', agent: 'researcher', task: '比对两地口径' }),
  ]);
  assert.equal(model.children.length, 2);
  assert.deepEqual(model.children.map((child) => child.n), [0, 1]);
  assert.equal(model.children[0].status, 'running');
  assert.equal(model.children[0].resultPreview, '');
  const cards = model.items.filter((item) => item.kind === 'subagent');
  assert.equal(cards.length, 2, '每个 task 一张派发卡片');
  assert.equal(cards[0].agent, 'researcher');
  assert.equal(cards[0].task, '核查跨境资金监管');
  assert.equal(cards[0].status, 'running');
  // A dispatch is not a generic tool card.
  assert.equal(model.items.filter((item) => item.kind === 'tool').length, 0);

  model = apply(model, frame(3, 'subagent_finished', {
    toolCallId: 'c1', ns: [0, 1], status: 'finished', isError: false, resultPreview: '两个子代理都完成了核查',
  }));
  for (const child of model.children) {
    assert.equal(child.status, 'finished');
    assert.equal(child.resultPreview, '两个子代理都完成了核查', 'resultPreview 不切分，每个 child 记同一份');
  }
  for (const card of model.items.filter((item) => item.kind === 'subagent')) {
    assert.equal(card.status, 'finished');
    assert.equal(card.resultPreview, '两个子代理都完成了核查');
  }
});

test('a failed dispatch degrades to the isError fact when the payload carries no status', () => {
  let model = createRunState({ runId: 'r1' });
  model = applyAll(model, [
    frame(1, 'subagent_started', { n: 0, toolCallId: 'c1', agent: 'researcher', task: '核查' }),
    frame(2, 'subagent_finished', { toolCallId: 'c1', ns: [0], isError: true, resultPreview: '子代理报错' }),
  ]);
  assert.equal(model.children[0].status, 'failed');
  assert.equal(model.items.find((item) => item.kind === 'subagent').status, 'failed');
});

test('the terminal frame children summary is the replay-time source of truth', () => {
  // The finished frame for c2 never arrived (the run ended first); the
  // terminal frame's summary — built from the authoritative child.json
  // documents — settles it.
  let model = createRunState({ runId: 'r1' });
  model = applyAll(model, [
    frame(1, 'subagent_started', { n: 0, toolCallId: 'c1', agent: 'researcher', task: '任务一' }),
    frame(2, 'subagent_finished', { toolCallId: 'c1', ns: [0], status: 'finished', isError: false, resultPreview: '摘要一' }),
    frame(3, 'subagent_started', { n: 1, toolCallId: 'c2', agent: 'researcher', task: '任务二' }),
    frame(4, 'run_finished', {
      status: 'finished',
      children: [
        { n: 0, agent: 'researcher', task: '任务一', status: 'finished' },
        { n: 1, agent: 'researcher', task: '任务二', status: 'interrupted' },
      ],
    }),
  ]);
  assert.equal(model.status, 'finished');
  assert.equal(model.children[0].status, 'finished');
  assert.equal(model.children[0].resultPreview, '摘要一', '汇总帧不抹掉已有的结果摘要');
  assert.equal(model.children[1].status, 'interrupted', '未收口的 child 按终帧汇总落定');
  const cards = model.items.filter((item) => item.kind === 'subagent');
  assert.equal(cards[0].status, 'finished');
  assert.equal(cards[1].status, 'interrupted');
});

test('a child still running at the verdict follows the run, like tools do', () => {
  for (const [type, status, expected] of [
    ['run_finished', 'finished', 'finished'],
    ['run_failed', 'failed', 'interrupted'],
    ['run_cancelled', 'cancelled', 'cancelled'],
  ]) {
    let model = createRunState({ runId: 'r1' });
    model = applyAll(model, [
      frame(1, 'subagent_started', { n: 0, toolCallId: 'c1', agent: 'researcher', task: '任务' }),
      frame(2, type, { status }),
    ]);
    assert.equal(model.children[0].status, expected, `${type} must settle a running child as ${expected}`);
    assert.equal(model.items.find((item) => item.kind === 'subagent').status, expected);
  }
});

test('a summary entry without a matching typed event still lands in the collection', () => {
  // Defensive replay: the terminal summary is authoritative, so a child the
  // stream never showed still appears — with only the facts the summary proves.
  let model = createRunState({ runId: 'r1' });
  model = apply(model, frame(1, 'run_finished', {
    status: 'finished',
    children: [{ n: 0, agent: 'researcher', task: '任务', status: 'finished' }],
  }));
  assert.equal(model.children.length, 1);
  assert.equal(model.children[0].agent, 'researcher');
  assert.equal(model.children[0].status, 'finished');
});

test('duplicate typed frames are dropped by the seq watermark, not applied twice', () => {
  let model = createRunState({ runId: 'r1' });
  model = applyAll(model, [
    frame(1, 'subagent_started', { n: 0, toolCallId: 'c1', agent: 'researcher', task: '任务' }),
    frame(1, 'subagent_started', { n: 0, toolCallId: 'c1', agent: 'researcher', task: '任务' }),
  ]);
  assert.equal(model.children.length, 1);
  assert.equal(model.items.filter((item) => item.kind === 'subagent').length, 1);
  assert.equal(model.ignoredFrames, 1);
});

test('historical logs with toolName=subagent keep rendering as plain tool cards', () => {
  // Read-side compatibility is a hard constraint: the old format carries no
  // typed events, so nothing may be upgraded into a ChildRun on read.
  let model = createRunState({ runId: 'r1' });
  model = applyAll(model, [
    frame(1, 'tool_started', {
      toolCallId: 'c1', toolName: 'subagent',
      args: { tasks: [{ agent: 'researcher', task: '核查跨境资金监管' }] },
    }),
    frame(2, 'tool_finished', { toolCallId: 'c1', toolName: 'subagent', result: '核查完成', isError: false }),
    frame(3, 'run_finished', { status: 'finished' }),
  ]);
  const card = model.items.find((item) => item.kind === 'tool');
  assert.ok(card, '旧格式仍是通用工具卡片');
  assert.equal(card.status, 'done');
  assert.equal(card.result, '核查完成');
  assert.equal(model.children.length, 0, '旧格式不产生 ChildRun 条目');
  assert.equal(model.items.filter((item) => item.kind === 'subagent').length, 0);
  // The old card keeps its reader-language description (派发子代理 · 对象).
  const described = describeTool(card.name, card.args);
  assert.equal(described.action, '派发子代理');
  assert.match(described.object, /researcher/);
});

test('childrenPanel rows drive the panel: empty stays hidden, children render rows', () => {
  let model = createRunState({ runId: 'r1' });
  assert.deepEqual(childrenPanel(model), [], '无子代理不渲染空面板');

  model = applyAll(model, [
    frame(1, 'subagent_started', { n: 0, toolCallId: 'c1', agent: 'researcher', task: '核查事实' }),
    frame(2, 'run_finished', { status: 'finished', children: [{ n: 0, agent: 'researcher', task: '核查事实', status: 'finished' }] }),
  ]);
  assert.deepEqual(childrenPanel(model), [
    { n: 0, agent: 'researcher', task: '核查事实', status: 'finished' },
  ]);
});

test('the subscription listens for the typed frames (named SSE frames never reach onmessage)', () => {
  const listeners = new Map();
  const source = {
    readyState: 0,
    addEventListener(type, handler) { listeners.set(type, handler); },
    close() { this.readyState = 2; },
  };
  const subscription = createRunSubscription({
    runId: 'r1',
    eventsUrl: () => '/api/runs/r1/events',
    eventSourceFactory: () => source,
    onEvent: () => {},
  });
  assert.ok(listeners.has('subagent_started'), 'subagent_started 必须有显式监听');
  assert.ok(listeners.has('subagent_finished'), 'subagent_finished 必须有显式监听');
  subscription.close();
});

test('child wording comes from present.mjs and stays reader language', () => {
  const described = describeChild({ agent: 'researcher', task: '核查跨境资金监管新规的落地口径' });
  assert.equal(described.icon, 'agent');
  assert.equal(described.action, '派发子代理 · researcher');
  assert.equal(described.active, '子代理 researcher 运行中');
  assert.equal(described.object, '核查跨境资金监管新规的落地口径');

  assert.equal(describeChild({ agent: '', task: '核查' }).action, '派发子代理');
  assert.equal(childLabel({ agent: 'researcher', task: '核查事实' }), 'researcher：核查事实');
  assert.equal(childLabel({ agent: '', task: '' }), '子代理');
  assert.equal(childStatusText('running'), '进行中');
  assert.equal(childStatusText('finished'), '已完成');
  assert.equal(childStatusText('failed'), '失败');
  assert.equal(childStatusText('interrupted'), '已中断');
  assert.equal(childStatusText('cancelled'), '已停止');
  assert.equal(childStateLabel({ status: 'failed' }), '失败');
  assert.equal(childStateLabel({ status: 'finished' }), '', '完成的子代理不额外写字');
  assert.equal(childrenPanelTitle(1), '派发了 1 个子代理');
  assert.equal(childrenPanelTitle(3), '派发了 3 个子代理');
});

test('a malformed started payload without a valid n is ignored, not rendered', () => {
  let model = createRunState({ runId: 'r1' });
  model = apply(model, frame(1, 'subagent_started', { toolCallId: 'c1', agent: 'researcher' }));
  assert.equal(model.children.length, 0);
  assert.equal(model.items.length, 0);
  assert.equal(model.lastSeq, 1);
});

test('R2 enrichment rides the terminal summary into the panel rows', () => {
  // The run dispatches one child live; the terminal frame then carries the
  // enrichment the host paired from pi-subagents' _meta.json.
  let model = createRunState({ runId: 'r-enrich' });
  model = apply(model, {
    type: 'subagent_started',
    payload: { n: 0, toolCallId: 'call_1', agent: 'researcher', task: '调研甲' },
  });
  model = apply(model, {
    type: 'run_finished',
    payload: {
      status: 'finished',
      children: [
        {
          n: 0, agent: 'researcher', task: '调研甲', status: 'finished',
          durationMs: 43210, toolCount: 6,
          artifacts: [{ name: 'output.md', bytes: 128, source: 'a1b2c3d4_researcher_0_output.md' }],
        },
      ],
    },
  });
  const rows = childrenPanel(model);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].durationMs, 43210, '终帧的增强字段进入面板行');
  assert.equal(rows[0].toolCount, 6);
  assert.deepEqual(rows[0].artifacts, [
    { name: 'output.md', bytes: 128, source: 'a1b2c3d4_researcher_0_output.md' },
  ]);
});

test('childFacts formats duration and tool count, empty without enrichment', () => {
  assert.equal(childFacts({ durationMs: 43210, toolCount: 6 }), '43 秒 · 6 次工具调用');
  assert.equal(childFacts({ durationMs: 95000 }), '1 分 35 秒');
  assert.equal(childFacts({}), '', '未增强的 child 没有事实行');
  assert.equal(childFacts({ durationMs: 0, toolCount: 0 }), '');
});

/* ── R3a 运行中进度（subagent_updated，§3.5） ─────────────────────────────── */

test('subagent_updated attaches progress to the child and its card; the latest frame wins', () => {
  let model = createRunState({ runId: 'r1' });
  model = applyAll(model, [
    frame(1, 'subagent_started', { n: 0, toolCallId: 'c1', agent: 'researcher', task: '核查跨境资金监管' }),
    frame(2, 'subagent_updated', { n: 0, steps: 2, lastToolName: 'web_search', lastToolTarget: '跨境资金监管', bytes: 512 }),
    frame(3, 'subagent_updated', { n: 0, steps: 3, lastToolName: 'read', lastToolTarget: 'report.md', bytes: 1024 }),
  ]);
  // 回放按 seq 顺序施加，同一 child 的多条 updated 只留最后一条的效果。
  const expected = { steps: 3, lastToolName: 'read', lastToolTarget: 'report.md', bytes: 1024 };
  assert.deepEqual(model.children[0].progress, expected);
  const card = model.items.find((item) => item.kind === 'subagent');
  assert.deepEqual(card.progress, expected, '派发卡片与 child 同步进度');
  assert.equal(model.children[0].status, 'running', '进度帧不参与终态判定');
});

test('the step counter never regresses on stale or malformed updated frames', () => {
  let model = createRunState({ runId: 'r1' });
  model = applyAll(model, [
    frame(1, 'subagent_started', { n: 0, toolCallId: 'c1', agent: 'researcher', task: '核查' }),
    frame(2, 'subagent_updated', { n: 0, steps: 5, lastToolName: 'read', lastToolTarget: 'a.md', bytes: 100 }),
    // 乱序/异常帧：steps 回退与非法值都被挡下，其余字段仍是 latest-wins。
    frame(3, 'subagent_updated', { n: 0, steps: 2, lastToolName: 'write', lastToolTarget: 'b.md', bytes: 200 }),
    frame(4, 'subagent_updated', { n: 0, steps: 'many', lastToolName: 'edit', lastToolTarget: 'b.md', bytes: 300 }),
  ]);
  const { progress } = model.children[0];
  assert.equal(progress.steps, 5, 'steps 只增不回退');
  assert.equal(progress.lastToolName, 'edit', '其他字段取最后一帧');
  assert.equal(progress.bytes, 300);
});

test('an updated frame for an unknown child is ignored, not materialised', () => {
  let model = createRunState({ runId: 'r1' });
  model = apply(model, frame(1, 'subagent_updated', { n: 0, steps: 3, lastToolName: 'read', bytes: 10 }));
  assert.equal(model.children.length, 0, '进度帧不能凭空造出 child');
  assert.equal(model.items.length, 0);
  assert.equal(model.lastSeq, 1, '帧仍计入水位线');
});

test('a replay applies every updated frame in order, so only the last one stands', () => {
  // 回放路径与实时路径同是 reduce：中间帧丢失不改变最终呈现。
  let full = createRunState({ runId: 'r1' });
  full = applyAll(full, [
    frame(1, 'subagent_started', { n: 0, toolCallId: 'c1', agent: 'researcher', task: '核查' }),
    frame(2, 'subagent_updated', { n: 0, steps: 1, lastToolName: 'web_search', lastToolTarget: '甲', bytes: 100 }),
    frame(3, 'subagent_updated', { n: 0, steps: 2, lastToolName: 'read', lastToolTarget: '乙.md', bytes: 200 }),
    frame(4, 'subagent_updated', { n: 0, steps: 3, lastToolName: 'write', lastToolTarget: '丙.md', bytes: 300 }),
  ]);
  let tailOnly = createRunState({ runId: 'r1' });
  tailOnly = applyAll(tailOnly, [
    frame(1, 'subagent_started', { n: 0, toolCallId: 'c1', agent: 'researcher', task: '核查' }),
    frame(4, 'subagent_updated', { n: 0, steps: 3, lastToolName: 'write', lastToolTarget: '丙.md', bytes: 300 }),
  ]);
  assert.deepEqual(full.children[0].progress, tailOnly.children[0].progress);
});

test('the card shows progress only while running; a settled card goes back to its status word', () => {
  const progress = { steps: 3, lastToolName: 'read', lastToolTarget: 'report.md', bytes: 1024 };
  assert.equal(childCardState({ status: 'running', progress }), '3 步 · 最近读取 report.md');
  assert.equal(childCardState({ status: 'finished', progress }), '', '完成后进度行消失');
  assert.equal(childCardState({ status: 'failed', progress }), '失败');
  assert.equal(childCardState({ status: 'cancelled', progress }), '已停止');
  assert.equal(childCardState({ status: 'interrupted', progress }), '已中断');
  // 终态落定后模型里的 progress 数据不再驱动呈现。
  let model = createRunState({ runId: 'r1' });
  model = applyAll(model, [
    frame(1, 'subagent_started', { n: 0, toolCallId: 'c1', agent: 'researcher', task: '核查' }),
    frame(2, 'subagent_updated', { n: 0, steps: 3, lastToolName: 'read', lastToolTarget: 'report.md', bytes: 1024 }),
    frame(3, 'subagent_finished', { toolCallId: 'c1', ns: [0], status: 'finished', isError: false, resultPreview: '核查完成' }),
  ]);
  const card = model.items.find((item) => item.kind === 'subagent');
  assert.equal(childCardState(card), '', '收口的卡片不渲染进度行');
});

test('a running child without a progress source renders no progress line', () => {
  assert.equal(childProgressText({ status: 'running' }), '');
  assert.equal(childCardState({ status: 'running' }), '', '无进度源不伪造进度行');
  assert.equal(childProgressText({ progress: { steps: 0, lastToolName: '', lastToolTarget: '', bytes: 0 } }), '');
});

test('childProgressText speaks reader language and bounds the target', () => {
  assert.equal(
    childProgressText({ progress: { steps: 3, lastToolName: 'read', lastToolTarget: 'report.md', bytes: 9 } }),
    '3 步 · 最近读取 report.md',
  );
  assert.equal(
    childProgressText({ progress: { steps: 1, lastToolName: 'web_search', lastToolTarget: '跨境资金监管', bytes: 9 } }),
    '1 步 · 最近检索 跨境资金监管',
  );
  // 引擎工具名不上屏：未知工具走 describeTool 的读者语言回退。
  const unknown = childProgressText({ progress: { steps: 2, lastToolName: 'mcp__internal_x', bytes: 9 } });
  assert.ok(!unknown.includes('mcp__internal_x'));
  assert.equal(childProgressText({ progress: { steps: 4, lastToolName: '', bytes: 9 } }), '4 步');
});

test('the subscription also listens for subagent_updated (named SSE frames never reach onmessage)', () => {
  const listeners = new Map();
  const source = {
    readyState: 0,
    addEventListener(type, handler) { listeners.set(type, handler); },
    close() { this.readyState = 2; },
  };
  const subscription = createRunSubscription({
    runId: 'r1',
    eventsUrl: () => '/api/runs/r1/events',
    eventSourceFactory: () => source,
    onEvent: () => {},
  });
  assert.ok(listeners.has('subagent_updated'), 'subagent_updated 必须有显式监听');
  subscription.close();
});
