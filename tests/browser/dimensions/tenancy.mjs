/**
 * Tenancy dimension: cross-tenant isolation and same-tenant role gates for the
 * run read/cancel/follow-up surfaces (accounts mode).
 */

import { newSession, signIn, csrfToken } from '../support/flows.mjs';

export async function run(ctx) {
  const { suite } = ctx;
  suite.dimension('tenancy');

  const alphaRunId = await ctx.page.evaluate(() => document.querySelector('#run-list [data-run-id]')?.dataset.runId ?? null);
  if (!alphaRunId) {
    suite.record('越权矩阵：需要一个属于组织甲的研究', false, '侧栏没有研究记录');
    return;
  }

  // ── beta tenant: nothing of alpha is reachable ────────────────────────────
  const { context: betaContext, page: beta } = await newSession(ctx);
  await signIn(ctx, beta, 'betaAdmin');
  const betaView = await beta.evaluate(() => ({
    rows: document.querySelectorAll('#run-list [data-run-id]').length,
    emptyTitle: document.querySelector('#run-list .empty-state-title')?.textContent,
  }));
  const betaList = await beta.evaluate(async () => {
    const response = await fetch('/api/runs', { headers: { Accept: 'application/json' } });
    const payload = await response.json();
    return { status: response.status, ids: (payload.runs ?? []).map((run) => run.runId) };
  });
  suite.record(
    '异租户列表：看不到组织甲的任何研究（UI 空态 + API 也不含）',
    betaView.rows === 0 && betaView.emptyTitle === '还没有研究记录' &&
      betaList.status === 200 && !betaList.ids.includes(alphaRunId),
    { ui: betaView, api: betaList },
  );

  const token = await csrfToken(beta);
  const matrix = await beta.evaluate(
    async ({ runId, csrf }) => {
      const post = (path) =>
        fetch(path, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-Glink-Harness-CSRF': csrf, Accept: 'application/json' },
          body: JSON.stringify({ prompt: '跨租户追问尝试', parentRunId: runId }),
        });
      const response = (path, options) => fetch(path, options);
      const detail = await response(`/api/runs/${runId}`, { headers: { Accept: 'application/json' } });
      const events = await response(`/api/runs/${runId}/events`, { headers: { Accept: 'text/event-stream' } });
      const report = await response(`/api/runs/${runId}/report`);
      const children = await response(`/api/runs/${runId}/children`, { headers: { Accept: 'application/json' } });
      const stop = await response(`/api/runs/${runId}/stop`, {
        method: 'POST',
        headers: { 'X-Glink-Harness-CSRF': csrf, Accept: 'application/json' },
      });
      const followup = await post('/api/runs');
      const followupBody = await followup.json().catch(() => ({}));
      return {
        detail: detail.status,
        events: events.status,
        report: report.status,
        children: children.status,
        stop: stop.status,
        stopBody: await stop.json().catch(() => ({})),
        followup: followup.status,
        followupError: followupBody.error,
        parentRunId: runId,
      };
    },
    { runId: alphaRunId, csrf: token },
  );
  suite.record(
    '异租户读面：详情 / SSE / 报告 / 子代理一律 404（同一 unknown_run 口径）',
    matrix.detail === 404 && matrix.events === 404 && matrix.report === 404 && matrix.children === 404,
    { detail: matrix.detail, events: matrix.events, report: matrix.report, children: matrix.children },
  );
  suite.record(
    '异租户写面：取消与追问都不泄露该 run 存在',
    matrix.stop === 404 && matrix.stopBody?.error === 'unknown_run' &&
      matrix.followup === 404 && matrix.followupError === 'unknown_run',
    { stop: matrix.stop, stopBody: matrix.stopBody, followup: matrix.followup, followupError: matrix.followupError },
  );

  // cross-site POST is refused before any identity is trusted
  const crossSite = await beta.evaluate(async (runId) => {
    const response = await fetch(`/api/runs/${runId}/stop`, {
      method: 'POST',
      headers: { Origin: 'https://evil.example', Accept: 'application/json' },
    });
    return { status: response.status, body: await response.json().catch(() => ({})) };
  }, alphaRunId);
  suite.record(
    '跨站来源的 POST：被拒绝（Origin 校验先于身份）',
    crossSite.status === 403 && ['csrf_invalid', 'cross_site_origin'].includes(crossSite.body?.error),
    crossSite,
  );
  await betaContext.close();

  // ── same-tenant member: read yes, stop no ────────────────────────────────
  if (ctx.member) {
    const member = ctx.member.page;
    const memberToken = await csrfToken(member);
    const memberMatrix = await member.evaluate(
      async ({ runId, csrf }) => {
        const detail = await fetch(`/api/runs/${runId}`, { headers: { Accept: 'application/json' } });
        const stop = await fetch(`/api/runs/${runId}/stop`, {
          method: 'POST',
          headers: { 'X-Glink-Harness-CSRF': csrf, Accept: 'application/json' },
        });
        return {
          detail: detail.status,
          stop: stop.status,
          stopBody: await stop.json().catch(() => ({})),
        };
      },
      { runId: alphaRunId, csrf: memberToken },
    );
    suite.record(
      '同租户成员：可读他人研究但不能取消（403 forbidden）',
      memberMatrix.detail === 200 && memberMatrix.stop === 403 &&
        memberMatrix.stopBody?.error === 'forbidden',
      memberMatrix,
    );
  } else {
    suite.skip('同租户成员读/取消门禁', '身份维度未创建成员账号');
  }

  // ── alpha admin can still read its own run ───────────────────────────────
  const own = await ctx.page.evaluate(async (runId) => {
    const response = await fetch(`/api/runs/${runId}`, { headers: { Accept: 'application/json' } });
    const payload = await response.json().catch(() => ({}));
    return { status: response.status, runId: payload.run?.runId };
  }, alphaRunId);
  suite.record(
    '同租户管理员：自己的研究仍可读（拒绝是租户/角色范围而非全面封锁）',
    own.status === 200 && own.runId === alphaRunId,
    own,
  );
}
