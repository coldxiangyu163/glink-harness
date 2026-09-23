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
  reportFileName,
  researchTitle,
  stageText,
  statusLabel,
  toolStateLabel,
} from '../../web/modules/present.mjs';
import { collectSources } from '../../web/modules/progress-panel.mjs';

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

test('workspace paths from every execution surface collapse to 工作区', () => {
  assert.equal(displayPath('/Users/someone/.pi-glink/workspaces/1ac8777fd709'), '工作区');
  assert.equal(displayPath('wrote /Users/someone/.pi-glink/workspaces/1ac8777fd709/report.md'), 'wrote 工作区/report.md');
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

  const write = describeTool('write', { path: '/Users/a/.pi-glink/workspaces/abcdef123456/report.md', content: '# x' });
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

test('only unusual tool states get a word', () => {
  assert.equal(toolStateLabel({ status: 'done' }), '');
  assert.equal(toolStateLabel({ status: 'running' }), '');
  assert.equal(toolStateLabel({ status: 'error' }), '失败');
  assert.equal(toolStateLabel({ status: 'interrupted' }), '已中断');
  assert.equal(toolStateLabel({ status: 'cancelled' }), '已停止');
});

test('lifecycle stages stay hidden; stages that change expectations are shown', () => {
  assert.equal(stageText({ stage: 'agent_start' }), null);
  assert.equal(stageText({ stage: 'agent_end' }), null);
  assert.equal(stageText({ stage: 'finished' }), null);
  assert.match(stageText({ stage: 'auto_retry_start', attempt: 2 }), /第 2 次/);
  assert.match(stageText({ stage: 'compaction_start' }), /整理上下文/);
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
    assert.ok(!/\bpi\b|pi-glink|inproc|docker|e2b|执行面|内核|宿主|sessionId|session /i.test(text), `user-visible copy leaks internals: ${text}`);
  }
});
