/**
 * Report drawer + progress panel dimension: the two-column report slot, its
 * split handle, download naming, overlay behaviour on narrow main areas, and
 * the progress/context panel.
 */

import { join } from 'node:path';
import { statSync } from 'node:fs';
import { firstListedRunId, openRunFromList } from '../support/flows.mjs';

export async function run(ctx) {
  const { suite, page, outDir } = ctx;
  suite.dimension('report');

  // Work on the subagent run: a report, a measurable duration (≥1s) and usage
  // facts, so the panel's runtime facts are actually populated.
  const runId = await page.evaluate(async () => {
    const response = await fetch('/api/runs', { headers: { Accept: 'application/json' } });
    const payload = await response.json();
    const runs = payload.runs ?? [];
    const subagent = runs.find(
      (run) => String(run.prompt ?? '').includes('子代理派发') && run.hasReport && (run.durationSeconds ?? 0) >= 1,
    );
    const fallback = runs.find((run) => run.hasReport && run.status === 'finished' && (run.durationSeconds ?? 0) >= 1);
    return (subagent ?? fallback)?.runId ?? null;
  });
  if (runId) await openRunFromList(page, runId);
  else suite.record('报告入口：至少有一个带报告的研究', false, '没有可用的报告 run');

  const reportButton = await page.$('#run-view .glink-report-button');
  if (!reportButton) {
    suite.record('报告入口：已完成的研究给出「打开报告」', false, 'no report button');
    return;
  }
  await page.click('#run-view .glink-report-button');
  await page.waitForSelector('#preview-article h1', { timeout: 15_000 });
  await page.waitForTimeout(600);
  const report = await page.evaluate(() => {
    const surface = document.querySelector('.surface');
    const pane = document.querySelector('.pane');
    const drawer = document.querySelector('#preview-drawer');
    const body = document.querySelector('#preview-body');
    const visibleNames = [...document.querySelectorAll('button, a[href]')]
      .filter((node) => node.offsetParent !== null && node.getAttribute('aria-label'))
      .map((node) => node.getAttribute('aria-label'));
    return {
      open: document.querySelector('#app-shell')?.classList.contains('preview-open'),
      columns: getComputedStyle(surface).gridTemplateColumns.split(' ').length,
      paneW: Math.round(pane.getBoundingClientRect().width),
      drawerW: Math.round(drawer.getBoundingClientRect().width),
      title: document.querySelector('#preview-title')?.textContent,
      titleColor: getComputedStyle(document.querySelector('#preview-title')).color,
      badge: document.querySelector('.preview-name .badge')?.textContent,
      h1: document.querySelector('#preview-article h1')?.textContent,
      h1Size: getComputedStyle(document.querySelector('#preview-article h1')).fontSize,
      paragraph: document.querySelector('#preview-article p')?.textContent,
      bodyScrollable: body.scrollHeight > body.clientHeight || body.clientHeight > 200,
      duplicates: visibleNames.filter((name, index) => visibleNames.indexOf(name) !== index),
      overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
      collapseVisible: !document.querySelector('#conversation-collapse')?.hidden,
      closeIcon: document.querySelector('#preview-close .icon')?.dataset.icon,
    };
  });
  suite.record(
    '报告右槽：两栏一线、默认对半、名字档写文件名与待核验草稿',
    report.open && report.columns === 3 && Math.abs(report.paneW - report.drawerW) <= 2 &&
      report.title === 'report.md' && report.titleColor === 'rgb(23, 23, 23)' &&
      report.badge === '待核验草稿',
    report,
  );
  suite.record(
    '报告正文：Markdown 渲染（H1 + 段落），无横向溢出、无重名控件',
    report.h1 === '合成报告' && Boolean(report.paragraph) && !report.overflow && report.duplicates.length === 0,
    { h1: report.h1, h1Size: report.h1Size, duplicates: report.duplicates },
  );
  suite.record(
    '报告开合键：双箭头收起、对话收起键让位',
    report.closeIcon === 'chevrons-left' && report.collapseVisible,
    { closeIcon: report.closeIcon, collapseVisible: report.collapseVisible },
  );
  await suite.shot(page, 'report-open');

  // split handle drag / clamp / reset
  const handle = await page.$('#split-handle');
  const box = await handle.boundingBox();
  await page.mouse.move(box.x + 0.5, box.y + 200);
  await page.mouse.down();
  await page.mouse.move(box.x + 120, box.y + 200, { steps: 6 });
  await page.mouse.up();
  await page.waitForTimeout(250);
  const dragged = await page.evaluate(() => ({
    paneW: Math.round(document.querySelector('.pane').getBoundingClientRect().width),
    stored: localStorage.getItem('glink-harness-report-split'),
  }));
  suite.record(
    '分割线：可拖改宽并记住（localStorage）',
    Math.abs(dragged.paneW - (report.paneW + 120)) <= 4 && Number(dragged.stored) === dragged.paneW,
    { before: report.paneW, after: dragged.paneW, stored: dragged.stored },
  );
  const moved = await handle.boundingBox();
  await page.mouse.move(moved.x + 0.5, moved.y + 200);
  await page.mouse.down();
  await page.mouse.move(300, moved.y + 200, { steps: 6 });
  await page.mouse.up();
  await page.waitForTimeout(250);
  const clamped = await page.evaluate(() => ({
    paneW: Math.round(document.querySelector('.pane').getBoundingClientRect().width),
    drawerW: Math.round(document.querySelector('#preview-drawer').getBoundingClientRect().width),
  }));
  suite.record(
    '分割线：对话列不窄于 560、报告不窄于 360',
    clamped.paneW === 560 && clamped.drawerW >= 360,
    clamped,
  );
  await page.dblclick('#split-handle');
  await page.waitForTimeout(250);
  const reset = await page.evaluate(() => ({
    paneW: Math.round(document.querySelector('.pane').getBoundingClientRect().width),
    stored: localStorage.getItem('glink-harness-report-split'),
  }));
  suite.record('分割线：双击复位', Math.abs(reset.paneW - report.paneW) <= 2 && reset.stored === null, reset);

  // download
  const downloadPromise = page.waitForEvent('download', { timeout: 20_000 });
  await page.click('#preview-download');
  const download = await downloadPromise;
  const downloadPath = join(outDir, 'downloaded-report.md');
  await download.saveAs(downloadPath);
  const size = statSync(downloadPath).size;
  suite.record(
    '报告下载：文件名可读（研究报告-…）、内容非空',
    /^研究报告/.test(download.suggestedFilename()) && size > 0,
    { filename: download.suggestedFilename(), bytes: size },
  );

  // narrow main area → overlay
  await page.setViewportSize({ width: 1000, height: 900 });
  await page.waitForTimeout(500);
  const overlay = await page.evaluate(() => {
    const surface = document.querySelector('.surface').getBoundingClientRect();
    const drawer = document.querySelector('#preview-drawer').getBoundingClientRect();
    return {
      overlay: document.querySelector('#app-shell')?.classList.contains('slot-overlay'),
      surfaceW: Math.round(surface.width),
      drawerInside: drawer.left >= surface.left - 1 && drawer.right <= surface.right + 1,
      handleHidden: getComputedStyle(document.querySelector('#split-handle')).display === 'none',
      overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
    };
  });
  suite.record(
    '窄主区（1000px）：报告改覆盖浮层、竖线隐藏、不溢出',
    overlay.overlay && overlay.drawerInside && overlay.handleHidden && !overlay.overflow,
    overlay,
  );
  await page.setViewportSize({ width: 1512, height: 900 });
  await page.waitForTimeout(500);

  // progress panel
  suite.dimension('progress');
  await page.click('#progress-toggle');
  await page.waitForTimeout(600);
  // The workbench keeps one slot: opening the progress panel makes the report
  // give way (the same slot state drives both surfaces).
  const panelTookSlot = await page.evaluate(() => ({
    reportOpen: document.querySelector('#app-shell')?.classList.contains('preview-open'),
    drawerHidden: document.querySelector('#preview-drawer')?.hidden,
    panelOpen: document.querySelector('#progress-layer')?.dataset.open,
  }));
  suite.record(
    '单一槽位：打开进度面板时报告让位（不叠两个槽位）',
    panelTookSlot.panelOpen === 'true' && panelTookSlot.reportOpen === false && panelTookSlot.drawerHidden === true,
    panelTookSlot,
  );
  const panel = await page.evaluate(() => ({
    open: document.querySelector('#progress-layer')?.dataset.open,
    expanded: document.querySelector('#progress-toggle')?.getAttribute('aria-expanded'),
    label: document.querySelector('#progress-toggle')?.getAttribute('aria-label'),
    width: Math.round(document.querySelector('#progress-panel').getBoundingClientRect().width),
    steps: document.querySelectorAll('.progress-step').length,
    text: document.querySelector('#progress-panel')?.innerText?.replace(/\n+/g, ' | '),
    tabs: [...document.querySelectorAll('.segmented-item')].map((node) => node.textContent.trim()),
  }));
  suite.record(
    '进度与上下文：340 浮层、开关两态、时间轴有步骤、运行信息齐全',
    panel.open === 'true' && panel.expanded === 'true' && panel.label === '收起进度与上下文' &&
      panel.width === 340 && panel.steps > 0 &&
      ['状态', '用时', '用量', '模型'].every((word) => (panel.text || '').includes(word)),
    { width: panel.width, steps: panel.steps, text: panel.text?.slice(0, 220) },
  );
  suite.record(
    '进度面板：模型名去掉路由前缀（browser-model）',
    /browser-model/.test(panel.text || '') && !/synthetic\/browser-model/.test(panel.text || ''),
    panel.text?.slice(0, 220),
  );
  await page.click('.segmented-item:nth-child(2)');
  await page.waitForTimeout(350);
  const sources = await page.evaluate(() => ({
    selected: document.querySelector('.segmented-item:nth-child(2)')?.getAttribute('aria-selected'),
    shown: !document.querySelector('#progress-sources')?.hidden,
    text: document.querySelector('#progress-panel')?.innerText?.replace(/\n+/g, ' | '),
  }));
  suite.record(
    '进度面板：来源页签可切换、列出检索词',
    sources.selected === 'true' && sources.shown && /浏览器验收|检索/.test(sources.text || ''),
    sources.text?.slice(0, 240),
  );
  await page.keyboard.press('Escape');
  await page.waitForTimeout(400);
  const escaped = await page.evaluate(() => ({
    open: document.querySelector('#progress-layer')?.dataset.open,
    focus: document.activeElement?.id,
    reportStillOpen: document.querySelector('#app-shell')?.classList.contains('preview-open'),
  }));
  suite.record('进度面板：Esc 收起并把焦点还给开关', escaped.open === 'false' && escaped.focus === 'progress-toggle', escaped);
  suite.record(
    '单一槽位：Esc 收起后两个槽位都关闭（报告不自动恢复）',
    escaped.reportStillOpen === false,
    escaped,
  );

  // close the report; the toggle returns to the left header and takes focus.
  // The panel took the slot, so reopen the report and test its close contract
  // on its own.
  if (!escaped.reportStillOpen) {
    await page.click('#run-view .glink-report-button');
    await page.waitForSelector('#preview-article h1', { timeout: 15_000 });
    await page.waitForTimeout(400);
  }
  await page.click('#preview-close');
  await page.waitForTimeout(400);
  const closed = await page.evaluate(() => ({
    open: document.querySelector('#app-shell')?.classList.contains('preview-open'),
    toggleVisible: !document.querySelector('#pane-preview-toggle')?.hidden,
    expanded: document.querySelector('#pane-preview-toggle')?.getAttribute('aria-expanded'),
    focus: document.activeElement?.id,
  }));
  suite.record(
    '报告收起：开关回到左栏栏头并接住焦点',
    closed.open === false && closed.toggleVisible && closed.expanded === 'false' &&
      closed.focus === 'pane-preview-toggle',
    closed,
  );
}
