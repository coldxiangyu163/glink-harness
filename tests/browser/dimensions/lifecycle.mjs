/**
 * Run lifecycle dimension: submit → stream → terminal state for every scripted
 * scenario (default / tools / fail / length / slow-stop), history replay,
 * follow-up turns — all through the production UI on the isolated host.
 */

import { waitFor } from '../support/harness.mjs';
import { submitPrompt, waitRunStatus, openRunFromList, listedRunIds, firstListedRunId, csrfToken, waitShell } from '../support/flows.mjs';

export async function run(ctx) {
  const { suite, page, baseUrl } = ctx;
  suite.dimension('lifecycle');

  // ── 1. slow run: running affordances, then stop ───────────────────────────
  await submitPrompt(page, '[scenario:slow] 浏览器验收 · 停止研究');
  await waitRunStatus(page, ['running'], 20_000);
  await page.waitForTimeout(1200);
  const running = await page.evaluate(() => ({
    status: document.querySelector('#run-view')?.dataset.status,
    stopVisible: !document.querySelector('#stop-run')?.hidden,
    sendHidden: document.querySelector('#send-button')?.hidden,
    readOnly: document.querySelector('#prompt-input')?.readOnly,
    placeholder: document.querySelector('#prompt-input')?.placeholder,
    foldExpanded: document.querySelector('.pi-process-toggle')?.getAttribute('aria-expanded'),
    processLabel: document.querySelector('.pi-process-label')?.textContent,
    marked: document.querySelectorAll('#run-list [aria-current="page"]').length,
    dock: getComputedStyle(document.querySelector('#conversation-pane')).getPropertyValue('--composer-dock').trim(),
  }));
  suite.record(
    '运行中：停止键原位替换发送键、输入只读、侧栏只标记当前研究',
    running.status === 'running' && running.stopVisible && running.sendHidden &&
      running.readOnly === true && running.marked === 1,
    running,
  );
  suite.record(
    '运行中：过程折叠展开并写动作（不写内核生命周期）',
    running.foldExpanded === 'true' && /^正在/.test(running.processLabel || ''),
    running.processLabel,
  );
  suite.record('运行中：输入器实测高度写回留白变量', /^\d+px$/.test(running.dock), running.dock);
  await suite.shot(page, 'lifecycle-running');

  // A reload during the run must resume it from the server's record, not lose it.
  const runningId = await page.evaluate(
    () => document.querySelector('#run-list [data-run-id]')?.dataset.runId ?? null,
  );
  await page.reload({ waitUntil: 'domcontentloaded' });
  await waitShell(page);
  await page.waitForSelector(`#run-list [data-run-id="${runningId}"]`, { timeout: 15_000 });
  await openRunFromList(page, runningId);
  const resumed = await page.evaluate(() => ({
    status: document.querySelector('#run-view')?.dataset.status,
    stopVisible: !document.querySelector('#stop-run')?.hidden,
    reportEntry: document.querySelector('#run-view .pi-report-button')?.offsetParent !== null,
  }));
  suite.record(
    '运行中刷新：服务端记录让页面恢复运行中（可继续停止），运行中没有报告入口',
    resumed.status === 'running' && resumed.stopVisible && resumed.reportEntry === false,
    resumed,
  );

  await page.click('#stop-run');
  await waitRunStatus(page, ['cancelled'], 30_000);
  await page.waitForTimeout(600);
  const stopped = await page.evaluate(() => ({
    status: document.querySelector('#run-view')?.dataset.status,
    tone: document.querySelector('.pi-outcome')?.dataset.tone,
    text: document.querySelector('.pi-outcome')?.innerText?.replace(/\n+/g, ' | '),
    icon: document.querySelector('.pi-outcome-icon .icon')?.dataset.icon,
    stopHidden: document.querySelector('#stop-run')?.hidden,
    sendVisible: !document.querySelector('#send-button')?.hidden,
    editable: !document.querySelector('#prompt-input')?.readOnly,
  }));
  suite.record(
    '停止：中性终态（虚线圆、说明「这不是错误」）、输入恢复',
    stopped.status === 'cancelled' && stopped.tone === 'stopped' &&
      /已停止研究/.test(stopped.text || '') && stopped.icon === 'dashed-circle' &&
      stopped.stopHidden === true && stopped.sendVisible === true && stopped.editable === true,
    stopped,
  );
  await suite.shot(page, 'lifecycle-stopped');

  // ── 2. default run ────────────────────────────────────────────────────────
  await page.click('#new-research');
  await page.waitForTimeout(300);
  const question = '[scenario:default] 浏览器验收 · 默认场景';
  await submitPrompt(page, question);
  await waitRunStatus(page, ['finished'], 30_000);
  await page.waitForTimeout(600);
  const defaultRun = await page.evaluate(() => ({
    status: document.querySelector('#run-view')?.dataset.status,
    question: document.querySelector('#run-question')?.innerText?.trim(),
    outcomeHidden: document.querySelector('.pi-outcome')?.hidden,
    answer: document.querySelector('#run-view .pi-answer-body')?.innerText?.slice(0, 200),
    draft: document.querySelector('#run-view .pi-answer-head .badge')?.textContent,
    actions: [...document.querySelectorAll('#run-view .pi-answer-actions > *')]
      .filter((node) => !node.hidden)
      .map((node) => node.getAttribute('aria-label')),
    reportButton: !document.querySelector('#run-view .pi-report-button')?.hidden,
  }));
  suite.record(
    '默认场景：问题进入气泡、回答带待核验草稿、成功不画终态卡',
    defaultRun.status === 'finished' && defaultRun.question === question &&
      defaultRun.outcomeHidden === true && /合成回答/.test(defaultRun.answer || '') &&
      defaultRun.draft === '待核验草稿',
    defaultRun,
  );
  suite.record(
    '回答动作条：复制 / 下载报告 / 重新研究',
    ['复制回答', '下载报告'].every((label) => defaultRun.actions.includes(label)) && defaultRun.reportButton,
    defaultRun.actions,
  );


  // ── 3. tools run (tool cards, failure word, detail, fold) ────────────────
  await page.click('#new-research');
  await page.waitForTimeout(300);
  await submitPrompt(page, '[scenario:tools] 浏览器验收 · 工具场景');
  await waitRunStatus(page, ['finished'], 30_000);
  await page.waitForTimeout(600);
  const tools = await page.evaluate(() => ({
    status: document.querySelector('#run-view')?.dataset.status,
    rows: [...document.querySelectorAll('#run-view .pi-tool')].map((node) => ({
      label: node.querySelector('.pi-tool-name')?.textContent,
      state: node.querySelector('.pi-tool-state')?.textContent,
      hasDetail: node.dataset.hasDetail,
    })),
    processLabel: document.querySelector('#run-view .pi-process-label')?.textContent,
    foldExpanded: document.querySelector('#run-view .pi-process-toggle')?.getAttribute('aria-expanded'),
  }));
  suite.record(
    '工具卡：三条工具行按动作命名（执行/读取/检索），失败行写状态字',
    tools.rows.length === 3 &&
      tools.rows.map((row) => row.label).join(',') === '执行,读取,检索' &&
      tools.rows[1].state === '失败' &&
      tools.rows.every((row) => !/web_search|bash|read/.test(row.label)),
    tools.rows,
  );
  suite.record(
    '过程折叠：完成后写「已完成 · N 个步骤」并自动收起',
    tools.foldExpanded === 'false' && /^已完成 · \d+ 个步骤$/.test(tools.processLabel || ''),
    tools.processLabel,
  );
  const toolDetail = await page.evaluate(async () => {
    const row = [...document.querySelectorAll('#run-view .pi-tool')].find((node) => node.dataset.hasDetail === 'true');
    if (!row) return null;
    const head = row.querySelector('.pi-tool-head');
    const closedHeight = Math.round(row.getBoundingClientRect().height);
    head.click();
    await new Promise((resolve) => setTimeout(resolve, 450));
    const detail = `${row.querySelector('.pi-tool-output')?.textContent ?? ''}${row.querySelector('.pi-tool-result')?.textContent ?? ''}`;
    const openedHeight = Math.round(row.getBoundingClientRect().height);
    const open = row.open;
    head.click();
    await new Promise((resolve) => setTimeout(resolve, 400));
    return {
      open,
      closedHeight,
      openedHeight,
      closedAgain: Math.round(row.getBoundingClientRect().height),
      detailChars: detail.length,
      hostPath: /\/workspaces\/[0-9a-f]{6,}/.test(detail) || /\/var\/folders\//.test(detail),
    };
  });
  suite.record(
    '工具行可展开看细节（高度张开 > 收起）、不露宿主绝对路径',
    Boolean(toolDetail) && toolDetail.open && toolDetail.openedHeight > toolDetail.closedHeight &&
      toolDetail.closedAgain <= toolDetail.closedHeight && !toolDetail.hostPath,
    toolDetail,
  );
  await suite.shot(page, 'lifecycle-tools');

  // ── 4. failed run ─────────────────────────────────────────────────────────
  await page.click('#new-research');
  await page.waitForTimeout(300);
  await submitPrompt(page, '[scenario:fail] 浏览器验收 · 失败场景');
  await waitRunStatus(page, ['failed'], 30_000);
  await page.waitForTimeout(600);
  const failed = await page.evaluate(() => ({
    status: document.querySelector('#run-view')?.dataset.status,
    tone: document.querySelector('.pi-outcome')?.dataset.tone,
    title: document.querySelector('.pi-outcome-title')?.textContent,
    actions: [...document.querySelectorAll('.pi-outcome-actions button')].map((node) => node.textContent.trim()),
    answerKept: /我已经开始了这次研究/.test(document.querySelector('#run-view .pi-answer-body')?.innerText ?? ''),
  }));
  suite.record(
    '失败场景：失败卡 + 已交付内容保留 + 重试/查看详情',
    failed.status === 'failed' && failed.actions.includes('重试') && failed.actions.includes('查看详情') &&
      failed.answerKept,
    failed,
  );
  await page.click('.pi-outcome-actions button:has-text("查看详情")');
  await page.waitForTimeout(400);
  const detail = await page.evaluate(() => {
    const node = document.querySelector('.pi-outcome-detail');
    return { visible: Boolean(node) && node.offsetParent !== null, text: node?.innerText?.slice(0, 200) };
  });
  suite.record(
    '失败详情：展开后能看到原始错误',
    detail.visible && /synthetic model failure|500/.test(detail.text || ''),
    detail,
  );
  await suite.shot(page, 'lifecycle-failed');

  // ── 5. truncated run (length) ─────────────────────────────────────────────
  await page.click('#new-research');
  await page.waitForTimeout(300);
  await submitPrompt(page, '[scenario:length] 浏览器验收 · 截断场景');
  await waitRunStatus(page, ['finished'], 30_000);
  await page.waitForTimeout(600);
  const length = await page.evaluate(() => ({
    status: document.querySelector('#run-view')?.dataset.status,
    tone: document.querySelector('.pi-outcome')?.dataset.tone,
    text: document.querySelector('.pi-outcome')?.innerText?.replace(/\n+/g, ' | '),
  }));
  suite.record(
    '截断场景：finished + stopReason=length 显示「交付不完整」',
    length.status === 'finished' && /交付不完整/.test(length.text || '') && /可能不完整/.test(length.text || ''),
    length,
  );

  // ── 6. history list collapses a follow-up conversation ────────────────────
  const listed = await listedRunIds(page);
  suite.record(
    '历史列表：一次停止 + 四个已结束研究，按会话合并列出',
    listed.length === 5,
    { rows: listed.length },
  );

  // ── 7. replay a finished run ──────────────────────────────────────────────
  const defaultRunId = await page.evaluate(async () => {
    const response = await fetch('/api/runs', { headers: { Accept: 'application/json' } });
    const payload = await response.json();
    const match = (payload.runs ?? []).find((run) => String(run.prompt ?? '').includes('默认场景'));
    return match?.runId ?? null;
  });
  if (defaultRunId) {
    await openRunFromList(page, defaultRunId);
    const replay = await page.evaluate(() => ({
      status: document.querySelector('#run-view')?.dataset.status,
      thinking: document.querySelectorAll('#run-view .pi-thinking').length,
      answer: document.querySelectorAll('#run-view .pi-answer-body .pi-text').length,
      answerText: document.querySelector('#run-view .pi-answer-body')?.innerText?.slice(0, 80),
      // `.pi-output` is also the children panel's class; the file card is the
      // one without `pi-children`.
      outputHidden: document.querySelector('#run-view .pi-output:not(.pi-children)')?.hidden,
      foldExpanded: document.querySelector('#run-view .pi-process-toggle')?.getAttribute('aria-expanded'),
      distance: (() => {
        const scroll = document.querySelector('#conversation-scroll');
        return Math.round(scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight);
      })(),
      composerPlaceholder: document.querySelector('#prompt-input')?.placeholder,
    }));
    suite.record(
      '历史重放：时间线完整、贴底、过程收起、文件卡给出报告',
      replay.status === 'finished' && replay.thinking > 0 && replay.answer > 0 &&
        replay.distance === 0 && replay.foldExpanded === 'false' && replay.outputHidden === false,
      replay,
    );
    suite.record(
      '历史重放的追问入口：输入器写「继续追问」',
      /继续追问/.test(replay.composerPlaceholder || ''),
      replay.composerPlaceholder,
    );

    // ── 8. follow-up turn ───────────────────────────────────────────────────
    await submitPrompt(page, '[scenario:tools] 浏览器验收 · 追问续接');
    await waitRunStatus(page, ['finished'], 30_000);
    await page.waitForTimeout(800);
    const followup = await page.evaluate(() => ({
      status: document.querySelector('#run-view')?.dataset.status,
      earlierCount: document.querySelectorAll('#earlier-turns > *').length,
      earlierText: document.querySelector('#earlier-turns')?.innerText?.replace(/\n+/g, ' | ').slice(0, 220),
      rows: document.querySelectorAll('#run-list [data-run-id]').length,
      url: location.href.replace(location.origin, ''),
    }));
    suite.record(
      '追问：父轮折叠进「之前的研究」、当前轮独立、列表行数不变',
      followup.earlierCount === 1 && /默认场景|合成回答/.test(followup.earlierText || '') &&
        followup.rows === 5,
      followup,
    );
    await suite.shot(page, 'lifecycle-followup');
  } else {
    suite.record('历史重放：找到默认场景条目', false, '未在侧栏找到默认场景的研究');
  }

  // ── 9. history survives a reload ──────────────────────────────────────────
  await page.reload({ waitUntil: 'domcontentloaded' });
  await waitFor(async () => page.$('#app-shell:not([hidden])'), { message: 'shell after reload' });
  await page.waitForTimeout(800);
  const afterReload = await page.evaluate(() => ({
    rows: document.querySelectorAll('#run-list [data-run-id]').length,
    homeCards: document.querySelectorAll('.home-card').length,
    homeRecent: document.querySelectorAll('#home-run-list [data-run-id]').length,
  }));
  suite.record(
    '刷新后历史仍在：侧栏条目与首页最近研究都非空',
    afterReload.rows === 5 && afterReload.homeRecent > 0,
    afterReload,
  );

  // ── 10. a `/`-prefixed prompt is refused before any run exists ───────────
  const beforeRows = (await listedRunIds(page)).length;
  await submitPrompt(page, '/run --bg 浏览器验收 · 命令门禁');
  await page.waitForTimeout(2000);
  const commandGate = await page.evaluate(() => ({
    rows: document.querySelectorAll('#run-list [data-run-id]').length,
    status: document.querySelector('#run-view')?.dataset.status ?? null,
    toast: document.querySelector('#toast-text')?.textContent,
    toastVisible: document.querySelector('#toast')?.hidden === false,
  }));
  suite.record(
    '命令门禁：`/run …` 被拒（不创建研究、不进入运行态）',
    commandGate.rows === beforeRows && commandGate.status === null,
    { beforeRows, ...commandGate },
  );
  await page.fill('#prompt-input', '');
  await page.waitForTimeout(200);

  // ── 11. follow-up gate when the parent session is unusable ───────────────
  const followupTarget = await firstListedRunId(page);
  if (followupTarget) {
    await ctx.removeSessionFile(followupTarget);
    const csrf = await csrfToken(page);
    const gate = await page.evaluate(
      async ({ runId, token }) => {
        const response = await fetch('/api/runs', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            Origin: location.origin,
            'X-Pi-Glink-CSRF': token,
          },
          body: JSON.stringify({ prompt: '浏览器验收 · 会话缺失的追问', parentRunId: runId }),
        });
        return { status: response.status, body: await response.json().catch(() => ({})) };
      },
      { runId: followupTarget, token: csrf },
    );
    suite.record(
      '追问门禁：父会话文件不可用时拒绝续接（不静默新起会话）',
      gate.status === 409 && gate.body?.error === 'session_unavailable',
      gate,
    );
  }

  // ── 12. “重新研究” opens a fresh run with the same question ──────────────
  // Pick a row the list actually shows (a follow-up conversation collapses to
  // its newest turn, so the original default run may no longer have a row).
  const rerunTarget = await firstListedRunId(page);
  if (rerunTarget) {
    await openRunFromList(page, rerunTarget);
    const rerunQuestion = await page.evaluate(() => document.querySelector('#run-question')?.innerText?.trim());
    const rowsBeforeRerun = (await listedRunIds(page)).length;
    await page.click('#run-view .pi-answer-actions button[aria-label="用同一个问题重新研究"]');
    await waitRunStatus(page, ['finished'], 30_000);
    await page.waitForTimeout(600);
    const rerun = await page.evaluate(() => ({
      rows: document.querySelectorAll('#run-list [data-run-id]').length,
      question: document.querySelector('#run-question')?.innerText?.trim(),
      status: document.querySelector('#run-view')?.dataset.status,
    }));
    suite.record(
      '重新研究：同一问题开启新一轮，列表新增一条',
      rerun.status === 'finished' && rerun.rows === rowsBeforeRerun + 1 && rerun.question === rerunQuestion,
      { rowsBefore: rowsBeforeRerun, rerunQuestion, ...rerun },
    );
  } else {
    suite.skip('重新研究', '列表没有可用的研究行');
  }

  return { defaultRunId };
}
