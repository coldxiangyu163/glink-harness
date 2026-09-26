/**
 * Resilience dimension: stream reconnect, unreadable history, module-load
 * failure recovery, and the "no leaks" boundaries (credentials, engine words,
 * session identifiers) across every surface the suite exercised.
 */

import { waitFor, chromeText } from '../support/harness.mjs';
import { submitPrompt, waitRunStatus, openRunFromList, firstListedRunId, newSession } from '../support/flows.mjs';

export async function run(ctx) {
  const { suite, page, baseUrl } = ctx;
  suite.dimension('resilience');

  // ── 1. SSE disconnect → bounded reconnect → run still lands correctly ────
  let eventRequests = 0;
  await page.route('**/api/runs/*/events*', (route) => {
    eventRequests += 1;
    if (eventRequests === 1) route.abort('failed');
    else route.continue();
  });
  await page.click('#new-research');
  await page.waitForTimeout(300);
  await submitPrompt(page, '[scenario:default] 浏览器验收 · 断流重连');
  await page.waitForTimeout(2500);
  await page.unroute('**/api/runs/*/events*');
  const reconnected = await waitFor(
    async () => {
      const state = await page.evaluate(() => ({
        status: document.querySelector('#run-view')?.dataset.status,
        answer: document.querySelector('#run-view .pi-answer-body')?.innerText?.slice(0, 120),
      }));
      return state.status === 'finished' ? state : null;
    },
    { timeout: 30_000, message: 'run finishes after a stream reconnect' },
  ).catch((error) => ({ error: String(error) }));
  suite.record(
    '流断开后前端有界重连：研究仍以 finished 收口且正文完整',
    !reconnected.error && reconnected.status === 'finished' && /合成回答/.test(reconnected.answer || ''),
    { intercepted: eventRequests, ...reconnected },
  );

  // ── 2. unreadable history ────────────────────────────────────────────────
  const target = await firstListedRunId(page);
  if (target) {
    await page.route(`**/api/runs/${target}/events*`, (route) =>
      route.fulfill({ status: 404, contentType: 'application/json', body: '{"error":"unknown_run"}' }),
    );
    await page.reload({ waitUntil: 'domcontentloaded' });
    await waitFor(async () => page.$('#app-shell:not([hidden])'), { message: 'shell after reload' });
    await page.waitForTimeout(600);
    await openRunFromList(page, target).catch(() => {});
    await waitFor(
      async () =>
        page.evaluate(() => {
          const outcome = document.querySelector('#run-view .pi-outcome');
          return Boolean(outcome) && !outcome.hidden && /读不到/.test(outcome.innerText);
        }),
      { timeout: 15_000, message: 'unreadable-history outcome' },
    ).catch(() => {});
    await page.waitForTimeout(400);
    const unreadable = await page.evaluate(() => {
      const outcome = document.querySelector('#run-view .pi-outcome');
      return {
        shown: Boolean(outcome) && !outcome.hidden,
        title: outcome?.querySelector('.pi-outcome-title')?.textContent,
        actions: [...(outcome?.querySelectorAll('.pi-outcome-actions button') ?? [])].map((node) => node.textContent.trim()),
      };
    });
    suite.record(
      '过程记录读不到：给出说明与下一步（重试 / 打开报告），不无限「重连中」',
      unreadable.shown && unreadable.title === '过程记录暂时读不到' && unreadable.actions.includes('重试'),
      unreadable,
    );
    await page.unroute(`**/api/runs/${target}/events*`);
    await page.reload({ waitUntil: 'domcontentloaded' });
    await waitFor(async () => page.$('#app-shell:not([hidden])'), { message: 'shell after unroute' });
    await page.waitForTimeout(600);
  } else {
    suite.skip('过程记录读不到：说明与下一步', '没有可用的历史研究');
  }

  // ── 3. module graph failure → recovery face → retry ─────────────────────
  await page.route('**/modules/workspace.mjs*', (route) => route.abort());
  await page.goto(baseUrl, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('#boot-failure', { timeout: 12_000 });
  const failure = await page.evaluate(() => {
    const overlay = document.getElementById('boot-failure');
    const button = overlay.querySelector('button');
    const style = getComputedStyle(button);
    return {
      role: overlay.getAttribute('role'),
      title: overlay.querySelector('h2')?.textContent,
      buttonH: style.height,
      buttonBg: style.backgroundColor,
      buttonText: button.textContent.trim(),
    };
  });
  suite.record(
    '模块图失败：恢复面（role=alert）+ 近黑胶囊重试键',
    failure.role === 'alert' && failure.buttonH === '40px' && failure.buttonBg === 'rgb(23, 23, 23)',
    failure,
  );
  await page.unroute('**/modules/workspace.mjs*');
  await page.click('#boot-failure button');
  await waitFor(async () => page.$('#app-shell:not([hidden])'), { timeout: 15_000, message: 'shell after retry' });
  await page.waitForTimeout(600);
  const recovered = await page.evaluate(() => ({
    shellVisible: !document.querySelector('#app-shell')?.hidden,
    rows: document.querySelectorAll('#run-list [data-run-id]').length,
    bootFailureHidden: document.querySelector('#boot-failure')?.hidden !== false,
  }));
  suite.record(
    '重试后工作台完整恢复（历史仍在）',
    recovered.shellVisible && recovered.rows > 0,
    recovered,
  );

  // ── 4. credential + engine-word leak boundaries ──────────────────────────
  const secrets = ['sk-alpha-browser', 'sk-beta-browser', 'sk-local-browser', ctx.fixture.password];
  const leaks = await page.evaluate((needles) => {
    const haystacks = {
      html: document.documentElement.outerHTML,
      text: document.body.innerText,
      storage: JSON.stringify(localStorage),
      url: location.href,
    };
    const found = [];
    for (const [where, haystack] of Object.entries(haystacks)) {
      for (const needle of needles) {
        if (needle && haystack.includes(needle)) found.push(`${where}:${needle}`);
      }
    }
    return found;
  }, secrets);
  suite.record('页面（HTML/可见文字/存储/URL）不含任何合成凭据', leaks.length === 0, leaks);

  const engineWords = await chromeText(page);
  const engineLeaks = ['pi-glink', '执行面', 'inproc', '内核', '宿主', 'session ', 'openai/'].filter((word) =>
    engineWords.includes(word),
  );
  suite.record(
    '界面文字不暴露仓库名 / 执行面 / 内核 / 会话 id / 模型全名',
    engineLeaks.length === 0,
    engineLeaks,
  );

  // ── 5. run-detail API does not leak host paths or session ids ────────────
  const detailLeak = await page.evaluate(async () => {
    const row = document.querySelector('#run-list [aria-current="page"]') ?? document.querySelector('#run-list [data-run-id]');
    const runId = row?.dataset.runId;
    if (!runId) return { error: 'no run' };
    const response = await fetch(`/api/runs/${runId}`, { headers: { Accept: 'application/json' } });
    const raw = await response.text();
    return {
      status: response.status,
      hasAbsPath: /\/var\/folders\/|\/private\/tmp\/|\/Users\//.test(raw),
      hasSessionId: /synthetic-browser-session/.test(raw),
    };
  });
  suite.record(
    '研究详情 API：不吐宿主绝对路径（会话 id 属产品字段，界面不显示）',
    detailLeak.error === undefined ? detailLeak.hasAbsPath === false : false,
    detailLeak,
  );
  const sessionIdLeak = await page.evaluate(async () => {
    const haystacks = { html: document.documentElement.outerHTML, text: document.body.innerText };
    return Object.entries(haystacks)
      .filter(([, value]) => value.includes('synthetic-browser-session'))
      .map(([where]) => where);
  });
  suite.record('界面（HTML/可见文字）不显示会话 id', sessionIdLeak.length === 0, sessionIdLeak);

  // ── 6. console cleanliness (401 probes are the expected anonymous face) ──
  // The suite deliberately injects failures: 401/403/404/409 responses
  // (anonymous probe, cross-tenant matrix, unreadable history, follow-up gate,
  // refused `/`-command),
  // an aborted SSE request (reconnect drill) and an aborted module import
  // (recovery face). Those console lines are expected; anything else is a
  // finding.
  const injected = [
    /Failed to load resource: the server responded with a status of (400|401|403|404|409)/,
    /Failed to load resource: net::ERR_FAILED/,
    /Failed to fetch dynamically imported module/,
    /\[workbench\] bootstrap failed/,
  ];
  const unexpected = suite.consoleErrors.filter((line) => !injected.some((pattern) => pattern.test(line)));
  suite.record(
    '浏览器控制台：除套件注入的失败（401/403/404/409、断流、模块图）外没有其他错误',
    unexpected.length === 0,
    unexpected.slice(0, 5),
  );

  await suite.shot(page, 'resilience-final');
}
