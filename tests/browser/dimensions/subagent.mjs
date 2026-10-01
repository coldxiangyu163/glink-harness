/**
 * Subagent dimension (GLINK-SUB-R2/R3): dispatch cards, the children panel,
 * running progress, artifact links, and replay of an enriched run.
 */

import { waitFor } from '../support/harness.mjs';
import { submitPrompt, waitRunStatus } from '../support/flows.mjs';

export async function run(ctx) {
  const { suite, page } = ctx;
  suite.dimension('subagent');

  await page.click('#new-research');
  await page.waitForTimeout(300);
  await submitPrompt(page, '[scenario:subagent] 浏览器验收 · 子代理派发');
  await page.waitForSelector('#run-view[data-status="running"]', { timeout: 20_000 });
  await page.waitForSelector('#run-view .glink-child-row', { timeout: 20_000 });

  // ── running: cards + progress ─────────────────────────────────────────────
  const sampled = [];
  const progressDeadline = Date.now() + 12_000;
  while (Date.now() < progressDeadline) {
    const sample = await page.evaluate(() => ({
      status: document.querySelector('#run-view')?.dataset.status,
      cards: [...document.querySelectorAll('#run-view .glink-subagent')].map((node) => ({
        text: node.innerText.replace(/\n+/g, ' | '),
        status: node.dataset.status,
        isToolRow: node.classList.contains('glink-tool'),
      })),
      panelTitle: document.querySelector('#run-view .glink-children .glink-output-title')?.textContent,
      panelRows: [...document.querySelectorAll('#run-view .glink-child-row')].map((node) => ({
        text: node.innerText.replace(/\n+/g, ' | '),
        status: node.dataset.status,
      })),
    }));
    sampled.push(sample);
    if (sample.cards.some((card) => /\d+ 步|最近/.test(card.text))) break;
    await page.waitForTimeout(400);
  }
  const runningSample = sampled.find((sample) => sample.cards.some((card) => /\d+ 步|最近/.test(card.text))) ?? sampled.at(-1);

  suite.record(
    '派发卡片：一次派发两个任务 → 两张卡片（不是通用工具行）',
    runningSample.cards.length === 2 &&
      runningSample.cards.every((card) => card.isToolRow && /派发子代理/.test(card.text)) &&
      runningSample.cards.every((card) => /researcher/.test(card.text)),
    runningSample.cards.map((card) => card.text.slice(0, 80)),
  );
  suite.record(
    '运行中进度（R3a）：卡片写「N 步 · 最近<动作> <对象>」',
    runningSample.cards.some((card) => /\d+ 步/.test(card.text)) &&
      runningSample.cards.some((card) => /最近/.test(card.text)),
    runningSample.cards.map((card) => card.text.slice(0, 100)),
  );
  suite.record(
    '子代理面板（运行中）：标题「派发了 2 个子代理」、两行写「进行中」',
    runningSample.panelTitle === '派发了 2 个子代理' &&
      runningSample.panelRows.length === 2 &&
      runningSample.panelRows.every((row) => row.status === 'running' && /进行中/.test(row.text)),
    { title: runningSample.panelTitle, rows: runningSample.panelRows },
  );
  await suite.shot(page, 'subagent-running');

  // ── settled ───────────────────────────────────────────────────────────────
  await waitRunStatus(page, ['finished'], 40_000);
  await page.waitForTimeout(1000);
  const settled = await page.evaluate(() => ({
    status: document.querySelector('#run-view')?.dataset.status,
    rows: [...document.querySelectorAll('#run-view .glink-child-row')].map((node) => ({
      text: node.innerText.replace(/\n+/g, ' | '),
      status: node.dataset.status,
      artifacts: [...node.querySelectorAll('.glink-child-artifact')].map((link) => ({
        name: link.textContent,
        href: link.getAttribute('href'),
      })),
    })),
    answer: document.querySelector('#run-view .glink-answer-body')?.innerText?.slice(0, 120),
  }));
  suite.record(
    '子代理收口：两行转「已完成」并带时长与工具次数',
    settled.status === 'finished' && settled.rows.length === 2 &&
      settled.rows.every((row) => row.status === 'finished' && /已完成/.test(row.text)) &&
      settled.rows.every((row) => /\d+ 秒 · \d+ 次工具调用/.test(row.text)),
    settled.rows.map((row) => row.text),
  );
  suite.record(
    '子代理产物（R2）：每个子代理列出 input/output/meta/transcript 四个可下载链接',
    settled.rows.every((row) => row.artifacts.length === 4) &&
      settled.rows.every((row) => row.artifacts.every((link) => /\/api\/runs\/[0-9a-f]+\/children\/[01]\//.test(link.href || ''))),
    settled.rows.map((row) => row.artifacts.map((link) => link.name)),
  );

  const firstArtifactHref = await page.getAttribute('#run-view .glink-child-artifact', 'href');
  const artifactResponse = await page.request.get(new URL(firstArtifactHref, ctx.baseUrl).toString());
  const artifactBody = artifactResponse.ok() ? await artifactResponse.text() : '';
  suite.record(
    '子代理产物可下载（宿主代理同一道路径校验，正文来自 children/<n>/）',
    artifactResponse.status() === 200 && /子代理/.test(artifactBody),
    { href: firstArtifactHref, status: artifactResponse.status(), bytes: artifactBody.length },
  );

  // API agrees with the panel
  const apiChildren = await page.evaluate(async () => {
    const row = document.querySelector('#run-list [aria-current="page"]');
    const runId = row?.dataset.runId;
    const response = await fetch(`/api/runs/${runId}/children`, { headers: { Accept: 'application/json' } });
    const payload = await response.json();
    return {
      count: payload.children?.length,
      n: payload.children?.map((child) => child.n),
      statuses: payload.children?.map((child) => child.status),
      enrichment: payload.children?.map((child) => child.enrichment),
      artifacts: payload.children?.map((child) => child.artifacts?.length),
    };
  });
  suite.record(
    'children API 与面板一致：两个 child、meta 增强、各 4 个产物',
    apiChildren.count === 2 && apiChildren.statuses.every((status) => status === 'finished') &&
      apiChildren.enrichment.every((value) => value === 'meta') &&
      apiChildren.artifacts.every((count) => count === 4),
    apiChildren,
  );
  await suite.shot(page, 'subagent-settled');

  // ── replay ────────────────────────────────────────────────────────────────
  const subagentRunId = await page.evaluate(async () => {
    const response = await fetch('/api/runs', { headers: { Accept: 'application/json' } });
    const payload = await response.json();
    return (payload.runs ?? []).find((run) => String(run.prompt ?? '').includes('子代理派发'))?.runId ?? null;
  });
  await page.reload({ waitUntil: 'domcontentloaded' });
  await waitFor(async () => page.$('#app-shell:not([hidden])'), { message: 'shell after reload' });
  await page.waitForTimeout(800);
  await page.click(`#run-list [data-run-id="${subagentRunId}"]`);
  await page.waitForSelector('#run-view[data-status]', { timeout: 15_000 });
  await page.waitForTimeout(1200);
  const replayed = await page.evaluate(() => ({
    cards: document.querySelectorAll('#run-view .glink-subagent').length,
    panelTitle: document.querySelector('#run-view .glink-children .glink-output-title')?.textContent,
    rows: [...document.querySelectorAll('#run-view .glink-child-row')].map((node) => node.dataset.status),
    artifacts: document.querySelectorAll('#run-view .glink-child-artifact').length,
  }));
  suite.record(
    '子代理历史重放：卡片、面板、产物链接都从落盘记录重建',
    replayed.cards === 2 && replayed.panelTitle === '派发了 2 个子代理' &&
      replayed.rows.length === 2 && replayed.rows.every((status) => status === 'finished') &&
      replayed.artifacts === 8,
    replayed,
  );
}
