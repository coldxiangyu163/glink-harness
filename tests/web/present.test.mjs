/**
 * Node regressions for the product vocabulary (`web/modules/present.mjs`).
 *
 * The workbench must speak the reader's language: no engine names, no tool
 * identifiers, no host paths, no raw float seconds. These tests pin the wording
 * contract without a browser.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import {
  describeTool,
  displayPath,
  formatDuration,
  formatTokens,
  modelLabel,
  noticeText,
  reportFileName,
  researchTitle,
  stageText,
  statusLabel,
  toolStateLabel,
} from '../../web/modules/present.mjs';
import { collectSources } from '../../web/modules/progress-panel.mjs';
import { createRunState, reduce } from '../../web/modules/run-reducer.mjs';

test('durations read as minutes and seconds, never raw floats', () => {
  assert.equal(formatDuration(537.838), '8 分 58 秒');
  assert.equal(formatDuration(42.4), '42 秒');
  assert.equal(formatDuration(120), '2 分');
  assert.equal(formatDuration(3900), '1 小时 5 分');
  assert.equal(formatDuration(null), '');
  assert.equal(formatDuration(0), '');
});

test('status words: stopping is not a failure, finishing is not success-coloured copy', () => {
  assert.equal(statusLabel('running'), '进行中');
  assert.equal(statusLabel('finished'), '已完成');
  assert.equal(statusLabel('cancelled'), '已停止');
  assert.equal(statusLabel('failed'), '未完成');
});

test('research titles are the first clause, at most 20 characters', () => {
  assert.equal(researchTitle('以律师身份写一个法律意见书，申请人周丽申请…'), '以律师身份写一个法律意见书');
  assert.equal(researchTitle('先在本工作区创建 notes.md，写入三行'), '先在本工作区创建 notes.md');
  assert.equal(researchTitle(''), '新研究');
  const long = researchTitle('请在沙箱里运行 shell 命令 env | grep -c OPENAI_API_KEY 然后汇报');
  assert.ok(Array.from(long).length <= 21, long);
  assert.ok(long.endsWith('…'));
});

test('a delegated batch shows its agents and tasks, not empty strings', () => {
  // 投影把数组里的对象元素送成紧凑 JSON 文本（host/glink_harness/kernel/projection.py）：
  // 卡片必须解析它并说出派了谁、做什么；这一条是 2026-09-24 E2B 真机 run 里
  // tasks 显示为 ["","","",""] 的回归。
  const batch = describeTool('subagent', {
    tasks: [
      JSON.stringify({ agent: 'researcher', task: '中国大陆个税与六年规则取证' }),
      JSON.stringify({ agent: 'researcher', task: '加拿大税务居民与信托规则' }),
      JSON.stringify({ agent: 'researcher', task: '澳洲受托人信托与CFC' }),
    ],
    concurrency: 3,
    async: true,
  });
  assert.equal(batch.action, '派发子代理');
  assert.equal(batch.object, 'researcher：中国大陆个税与六年规则取证 等 3 个子代理');

  const single = describeTool('subagent', { tasks: [{ agent: 'researcher', task: '香港FSIE' }] });
  assert.equal(single.object, 'researcher：香港FSIE');

  // 管理动作不显示引擎动词，未识别的动作不猜测内容。
  assert.equal(describeTool('subagent', { action: 'list' }).object, '列出可用子代理');
  assert.equal(describeTool('subagent', { action: 'weird-thing' }).object, '');
  assert.equal(describeTool('subagent', {}).object, '');

  // 截断到非法的 JSON 前缀不会让卡片崩溃或显示空白：退回原文。
  const clipped = describeTool('subagent', { tasks: ['{"agent":"researcher","task":"长'.concat('x'.repeat(300))] });
  assert.match(clipped.object, /^researcher/);
});

test('workspace paths from every execution surface collapse to 工作区', () => {
  assert.equal(displayPath('/Users/someone/.glink-harness/workspaces/1ac8777fd709'), '工作区');
  assert.equal(displayPath('wrote /Users/someone/.glink-harness/workspaces/1ac8777fd709/report.md'), 'wrote 工作区/report.md');
  assert.equal(displayPath('cd /workspace && ls'), 'cd 工作区 && ls');
  assert.equal(displayPath('/home/user/workspace/notes.md'), '工作区/notes.md');
  assert.equal(displayPath('/workspaces-archive/x'), '/workspaces-archive/x');
});

test('tool rows speak in actions and objects, never engine tool names', () => {
  const search = describeTool('web_search', { query: '', queries: ['跨境资金监管', '外汇管理条例'] });
  assert.equal(search.action, '检索');
  assert.equal(search.object, '跨境资金监管 等 2 条');
  assert.equal(search.mono, false);

  const bash = describeTool('bash', { command: 'pwd && ls\nsecond line' });
  assert.equal(bash.action, '执行');
  assert.equal(bash.object, 'pwd && ls');
  assert.equal(bash.mono, true);

  const write = describeTool('write', { path: '/Users/a/.glink-harness/workspaces/abcdef123456/report.md', content: '# x' });
  assert.equal(write.action, '写入');
  assert.equal(write.object, 'report.md');

  const fetchPage = describeTool('fetch_content', { url: 'https://www.safe.gov.cn/safe/2024/index.html' });
  assert.equal(fetchPage.action, '读取网页');
  assert.equal(fetchPage.object, 'www.safe.gov.cn/safe/2024/index.html');

  const unknown = describeTool('mystery_tool', { target: 'x' });
  assert.equal(unknown.action, '调用工具');
  for (const described of [search, bash, write, fetchPage, unknown]) {
    assert.ok(!/web_search|bash|fetch_content|mystery_tool/.test(`${described.action}${described.active}`));
  }
});

test('projected presentation wins over names; legacy tool frames still use their old names', () => {
  const projected = describeTool('bash', { command: 'pwd' }, 'fetch_page');
  assert.equal(projected.action, '读取网页');
  assert.equal(projected.icon, 'globe');
  assert.equal(describeTool('bash', { command: 'pwd' }).action, '执行');
  const acpOther = describeTool('web_search', { query: '甲' }, 'generic');
  assert.equal(acpOther.action, '调用工具', 'ACP other must not inherit pi tool-name meaning');
  assert.equal(describeTool('tool', {}, 'delete').action, '删除');
  assert.equal(describeTool('tool', {}, 'think').action, '思考');
  assert.equal(describeTool('tool', {}, 'unknown_hint').action, '调用工具');
});

test('only unusual tool states get a word', () => {
  assert.equal(toolStateLabel({ status: 'done' }), '');
  assert.equal(toolStateLabel({ status: 'running' }), '');
  assert.equal(toolStateLabel({ status: 'error' }), '失败');
  assert.equal(toolStateLabel({ status: 'interrupted' }), '已中断');
  assert.equal(toolStateLabel({ status: 'cancelled' }), '已停止');
});

//: 一帧宿主会真实提交的 run_stage(plan)（投影与字段裁剪见
//: host/glink_harness/kernel/acp_projection.py `_agent_plan`），按持久化 envelope
//: `{type, v, runId, seq, ts, payload}` 经 reducer 施加。计划文案断言必须
//: 走这条事件 → 状态 → 文案链路：手工构造 stage item 测不出 reducer 丢
//: entries 的回归（「研究计划已更新」曾在真实链路上永不出现）。
function planStage(entries) {
  const state = reduce(createRunState({ runId: 'present-plan' }), {
    type: 'run_stage',
    v: 1,
    runId: 'present-plan',
    seq: 1,
    ts: 1,
    payload: { stage: 'plan', entries },
  });
  assert.equal(state.rejectedFrames, 0);
  const item = state.items[state.items.length - 1];
  assert.ok(item && item.kind === 'stage' && item.stage === 'plan');
  return item;
}

test('lifecycle stages stay hidden; stages that change expectations are shown', () => {
  assert.equal(stageText({ stage: 'agent_start' }), null);
  assert.equal(stageText({ stage: 'agent_end' }), null);
  assert.equal(stageText({ stage: 'finished' }), null);
  assert.match(stageText({ stage: 'auto_retry_start', attempt: 2 }), /第 2 次/);
  assert.match(stageText({ stage: 'compaction_start' }), /整理上下文/);
  assert.equal(stageText(planStage([{ content: '查询资料', status: 'in_progress', priority: 'high' }])), '正在整理研究计划 · 查询资料');
  assert.equal(stageText(planStage([])), '正在整理研究计划');
  assert.equal(stageText(planStage([{ content: '完成', status: 'completed', priority: 'high' }])), '研究计划已更新');
});

test('warnings use reader vocabulary instead of exposing protocol or failure codes', () => {
  const messages = {
    report_rejected: '研究报告未能保存',
    child_progress_failed: '子代理的进度暂时无法更新',
    child_enrichment_failed: '子代理的附加资料暂时无法读取',
    acp_permission_denied: '一项工具操作未获授权',
    acp_content_not_projected: '部分研究内容无法显示',
    acp_usage_invalid: '使用量信息暂时无法显示',
  };
  for (const [message, expected] of Object.entries(messages)) {
    assert.equal(noticeText({ message }), expected);
    assert.ok(!/acp_|child_|report_/.test(expected));
  }
  assert.notEqual(noticeText({ message: 'acp_future_notice' }), '研究过程中出现了一条提示');
  assert.notEqual(noticeText({ message: 'child_future_failed' }), '研究过程中出现了一条提示');
});

test('usage, model and download names are reader-facing', () => {
  assert.equal(formatTokens(8420), '8,420');
  assert.equal(formatTokens(349210), '约 34.9 万');
  assert.equal(modelLabel('openai/gpt-5.6-sol'), 'gpt-5.6-sol');
  assert.equal(reportFileName('以律师身份写一个法律意见书，申请人…'), '研究报告-以律师身份写一个法律意见书.md');
});

test('sources panel lists queries and pages the run touched, deduplicated', () => {
  const items = [
    { kind: 'tool', name: 'web_search', args: { queries: ['甲', '乙'] } },
    { kind: 'tool', name: 'web_search', args: { query: '甲' } },
    { kind: 'tool', name: 'fetch_content', args: { url: 'https://example.com/a' } },
    { kind: 'tool', name: 'fetch_content', args: { url: 'javascript:alert(1)' } },
    { kind: 'tool', name: 'bash', args: { command: 'curl https://x' } },
  ];
  const { queries, pages } = collectSources(items);
  assert.deepEqual(queries, ['甲', '乙']);
  assert.deepEqual(pages.map((page) => page.href), ['https://example.com/a']);
});

test('the shipped UI never names the engine or the execution surface', () => {
  const files = [
    '../../web/index.html',
    '../../web/app.js',
    '../../web/modules/workspace.mjs',
    '../../web/modules/run-view.mjs',
    '../../web/modules/progress-panel.mjs',
    '../../web/modules/present.mjs',
  ];
  // Only user-visible string literals matter; comments may explain the engine.
  const visible = files
    .map((file) => readFileSync(new URL(file, import.meta.url), 'utf8'))
    .map((source) => source.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/[^\n]*/g, '').replace(/<!--[\s\S]*?-->/g, ''))
    .join('\n');
  const literals = [...visible.matchAll(/'([^'\n]*)'|"([^"\n]*)"|>([^<>\n]+)</g)]
    .map((match) => match[1] ?? match[2] ?? match[3] ?? '')
    .filter((text) => /[\u4e00-\u9fff]/.test(text));
  for (const text of literals) {
    assert.ok(!/\bpi\b|glink-harness|inproc|docker|e2b|执行面|内核|宿主|sessionId|session /i.test(text), `user-visible copy leaks internals: ${text}`);
  }
});
