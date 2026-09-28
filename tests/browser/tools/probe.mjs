/**
 * State probe: dumps what the production front-end actually renders for the
 * fixture scenarios, so the acceptance assertions are written against measured
 * DOM, not guesses. Diagnostic tool — the acceptance suite is the deliverable.
 *
 * Usage: node tests/browser/tools/probe.mjs [--mode accounts|local]
 */

import { chromium } from 'playwright';
import { mkdtempSync, mkdirSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { startHost } from '../support/host.mjs';

const REPO_ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..', '..');
const mode = process.argv.includes('--mode') ? process.argv[process.argv.indexOf('--mode') + 1] : 'accounts';
const outDir = mkdtempSync(join(tmpdir(), 'pi-glink-probe-'));
const stateDir = join(outDir, 'state');
mkdirSync(stateDir, { recursive: true });

const host = await startHost({
  repoRoot: REPO_ROOT,
  mode,
  stateDir,
  logFile: join(outDir, 'fixture.log'),
});
const browser = await chromium.launch();
const context = await browser.newContext({ viewport: { width: 1512, height: 900 } });
const page = await context.newPage();
const consoleErrors = [];
page.on('pageerror', (error) => consoleErrors.push(`pageerror: ${error}`));
page.on('console', (message) => {
  if (message.type() === 'error') consoleErrors.push(`console: ${message.text()}`);
});

const dump = {};
const snap = async (name, fn) => {
  try {
    dump[name] = await fn();
  } catch (error) {
    dump[name] = { error: String(error) };
  }
};

await page.goto(host.baseUrl, { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(800);
await snap('beforeLogin', () =>
  page.evaluate(() => ({
    authVisible: !document.querySelector('#auth-view')?.hidden,
    shellVisible: !document.querySelector('#app-shell')?.hidden,
    title: document.querySelector('#auth-title')?.textContent,
    foot: document.querySelector('#login-foot')?.innerText,
    formVisible: !document.querySelector('#login-form')?.hidden,
    registerVisible: !document.querySelector('#register-form')?.hidden,
    bodyText: document.body.innerText.slice(0, 600),
  })),
);

if (mode === 'accounts') {
  await page.fill('#login-email', host.users.alphaAdmin.email);
  await page.fill('#login-password', 'wrong-password');
  await page.click('#login-submit');
  await page.waitForTimeout(900);
  await snap('badLogin', () =>
    page.evaluate(() => ({
      error: document.querySelector('#login-error')?.textContent,
      errorHidden: document.querySelector('#login-error')?.hidden,
      shellVisible: !document.querySelector('#app-shell')?.hidden,
      fieldInvalid: document.querySelector('#login-password')?.classList.contains('invalid') ||
        document.querySelector('#login-password')?.getAttribute('aria-invalid'),
    })),
  );
  await page.fill('#login-password', host.password);
  await page.click('#login-submit');
  await page.waitForSelector('#app-shell:not([hidden])', { timeout: 15_000 });
  await page.waitForTimeout(600);
}

await snap('afterLogin', () =>
  page.evaluate(() => ({
    accountAreaVisible: !document.querySelector('#account-area')?.hidden,
    org: document.querySelector('#account-org')?.textContent,
    name: document.querySelector('#account-name')?.textContent,
    avatar: document.querySelector('#account-avatar')?.textContent,
    view: document.querySelector('#app-shell')?.dataset.view,
    homeTitle: document.querySelector('#home-view .home-title')?.textContent,
    homeCards: document.querySelectorAll('.home-card').length,
    emptyStates: [...document.querySelectorAll('.empty-state')].map((node) => ({
      where: node.parentElement?.id,
      title: node.querySelector('.empty-state-title')?.textContent,
      role: node.getAttribute('role'),
    })),
    navRows: document.querySelectorAll('#run-list [data-run-id]').length,
  })),
);

// breakpoints incl. account area behaviour
const widths = [1512, 1250, 1050, 1049, 900, 721, 720, 600];
dump.breakpoints = [];
for (const width of widths) {
  await page.setViewportSize({ width, height: 900 });
  await page.waitForTimeout(320);
  dump.breakpoints.push(
    await page.evaluate((viewportWidth) => {
      const lines = document.querySelector('.account-lines');
      const button = document.querySelector('#account-button');
      const rect = (selector) => {
        const node = document.querySelector(selector);
        const box = node.getBoundingClientRect();
        return { w: Math.round(box.width), x: Math.round(box.x) };
      };
      return {
        width: viewportWidth,
        sidebar: rect('#sidebar').w,
        header: rect('.pane-header'),
        accountLinesDisplay: lines ? getComputedStyle(lines).display : null,
        accountButton: rect('#account-button'),
        accountAvatar: rect('#account-avatar'),
        homeCards: document.querySelector('.home-cards')
          ? getComputedStyle(document.querySelector('.home-cards')).gridTemplateColumns.split(' ').length
          : null,
        overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
      };
    }, width),
  );
}
await page.setViewportSize({ width: 1512, height: 900 });
await page.waitForTimeout(300);

// account menu
await page.click('#account-button');
await page.waitForTimeout(400);
await snap('accountMenu', () =>
  page.evaluate(() => ({
    hidden: document.querySelector('#account-menu')?.hidden,
    expanded: document.querySelector('#account-button')?.getAttribute('aria-expanded'),
    org: document.querySelector('#account-menu-org')?.textContent,
    email: document.querySelector('#account-menu-email')?.textContent,
    role: document.querySelector('#account-menu-role')?.textContent,
    inviteHidden: document.querySelector('#account-invite')?.hidden,
    logoutHidden: document.querySelector('#account-logout')?.hidden,
  })),
);
await page.keyboard.press('Escape');
await page.waitForTimeout(300);

// default scenario run
await page.fill('#prompt-input', '[scenario:default] 探测默认场景');
await page.press('#prompt-input', 'Enter');
await page.waitForSelector('#run-view[data-status="finished"], #run-view[data-status="failed"]', { timeout: 30_000 });
await page.waitForTimeout(900);
await snap('defaultRun', () =>
  page.evaluate(() => ({
    status: document.querySelector('#run-view')?.dataset.status,
    question: document.querySelector('#run-question')?.innerText?.slice(0, 200),
    outcomeHidden: document.querySelector('.pi-outcome')?.hidden,
    outcomeText: document.querySelector('.pi-outcome')?.innerText,
    answerText: document.querySelector('.pi-answer-body')?.innerText?.slice(0, 200),
    outputHidden: document.querySelector('[aria-label="生成的文件"]')?.hidden,
    outputText: document.querySelector('[aria-label="生成的文件"]')?.innerText?.slice(0, 200),
    actions: [...document.querySelectorAll('.pi-answer-actions > *')]
      .filter((node) => !node.hidden)
      .map((node) => node.getAttribute('aria-label')),
    runListRows: [...document.querySelectorAll('#run-list [data-run-id]')].map((node) => ({
      id: node.dataset.runId,
      text: node.innerText.replace(/\n/g, ' | ').slice(0, 120),
    })),
    previewToggleVisible: !document.querySelector('#pane-preview-toggle')?.hidden,
  })),
);
await page.screenshot({ path: join(outDir, 'default-run.png') });

// tools scenario
await page.click('#new-research');
await page.waitForTimeout(300);
await page.fill('#prompt-input', '[scenario:tools] 探测工具场景');
await page.press('#prompt-input', 'Enter');
await page.waitForSelector('#run-view[data-status="finished"], #run-view[data-status="failed"]', { timeout: 30_000 });
await page.waitForTimeout(800);
await snap('toolsRun', () =>
  page.evaluate(() => ({
    status: document.querySelector('#run-view')?.dataset.status,
    tools: [...document.querySelectorAll('.pi-tool')].map((node) => ({
      label: node.querySelector('.pi-tool-name')?.textContent,
      object: node.querySelector('.pi-tool-object')?.textContent,
      state: node.querySelector('.pi-tool-state')?.textContent,
      hasDetail: node.dataset.hasDetail,
    })),
    thinking: document.querySelectorAll('.pi-thinking').length,
    processLabel: document.querySelector('.pi-process-label')?.textContent,
    answerText: document.querySelector('.pi-answer-body')?.innerText?.slice(0, 120),
    foldExpanded: document.querySelector('.pi-process-toggle')?.getAttribute('aria-expanded'),
  })),
);
await page.screenshot({ path: join(outDir, 'tools-run.png') });

// failed scenario
await page.click('#new-research');
await page.waitForTimeout(300);
await page.fill('#prompt-input', '[scenario:fail] 探测失败场景');
await page.press('#prompt-input', 'Enter');
await page.waitForSelector('#run-view[data-status="failed"]', { timeout: 30_000 });
await page.waitForTimeout(800);
await snap('failedRun', () =>
  page.evaluate(() => ({
    status: document.querySelector('#run-view')?.dataset.status,
    outcomeText: document.querySelector('.pi-outcome')?.innerText,
    outcomeTone: document.querySelector('.pi-outcome')?.dataset.tone,
    detailsButton: [...document.querySelectorAll('.pi-outcome button')].map((node) => node.textContent.trim()),
  })),
);

// length scenario
await page.click('#new-research');
await page.waitForTimeout(300);
await page.fill('#prompt-input', '[scenario:length] 探测截断场景');
await page.press('#prompt-input', 'Enter');
await page.waitForSelector('#run-view[data-status="finished"]', { timeout: 30_000 });
await page.waitForTimeout(800);
await snap('lengthRun', () =>
  page.evaluate(() => ({
    status: document.querySelector('#run-view')?.dataset.status,
    outcomeText: document.querySelector('.pi-outcome')?.innerText,
    outcomeHidden: document.querySelector('.pi-outcome')?.hidden,
  })),
);

// subagent scenario: sample while running, then after settle
await page.click('#new-research');
await page.waitForTimeout(300);
await page.fill('#prompt-input', '[scenario:subagent] 探测子代理场景');
await page.press('#prompt-input', 'Enter');
await page.waitForSelector('.pi-child-row, .pi-child-card', { timeout: 20_000 });
await page.waitForTimeout(2200);
await snap('subagentRunning', () =>
  page.evaluate(() => ({
    status: document.querySelector('#run-view')?.dataset.status,
    childCards: [...document.querySelectorAll('.pi-child-card, .pi-subagent')].map((node) => ({
      cls: node.className,
      text: node.innerText.replace(/\n/g, ' | ').slice(0, 160),
      status: node.dataset.status,
    })),
    panelHidden: document.querySelector('.pi-children')?.hidden,
    panelText: document.querySelector('.pi-children')?.innerText?.replace(/\n/g, ' | ').slice(0, 300),
    timelineSubagentItems: [...document.querySelectorAll('.pi-step-row')].map((n) => n.className).slice(0, 20),
  })),
);
await page.screenshot({ path: join(outDir, 'subagent-running.png') });
await page.waitForSelector('#run-view[data-status="finished"]', { timeout: 30_000 });
await page.waitForTimeout(1200);
await snap('subagentDone', () =>
  page.evaluate(() => ({
    status: document.querySelector('#run-view')?.dataset.status,
    panelText: document.querySelector('.pi-children')?.innerText?.replace(/\n/g, ' | ').slice(0, 400),
    panelRows: [...document.querySelectorAll('.pi-child-row')].map((node) => ({
      text: node.innerText.replace(/\n/g, ' | '),
      status: node.dataset.status,
      artifacts: [...node.querySelectorAll('.pi-child-artifact')].map((a) => ({ name: a.textContent, href: a.getAttribute('href') })),
    })),
    answerText: document.querySelector('.pi-answer-body')?.innerText?.slice(0, 100),
  })),
);
await page.screenshot({ path: join(outDir, 'subagent-done.png') });

// report drawer + progress panel on the subagent run (it has a report)
await snap('reportClosed', () =>
  page.evaluate(() => ({
    previewOpen: document.querySelector('#app-shell')?.classList.contains('preview-open'),
    reportButtonVisible: !document.querySelector('.pi-report-button')?.hidden,
    previewToggleHidden: document.querySelector('#pane-preview-toggle')?.hidden,
  })),
);
await page.click('.pi-report-button');
await page.waitForSelector('#preview-article h1', { timeout: 15_000 });
await page.waitForTimeout(600);
await snap('reportOpen', () =>
  page.evaluate(() => {
    const surface = document.querySelector('.surface');
    const pane = document.querySelector('.pane');
    const drawer = document.querySelector('#preview-drawer');
    return {
      open: document.querySelector('#app-shell')?.classList.contains('preview-open'),
      columns: getComputedStyle(surface).gridTemplateColumns,
      paneW: Math.round(pane.getBoundingClientRect().width),
      drawerW: Math.round(drawer.getBoundingClientRect().width),
      title: document.querySelector('#preview-title')?.textContent,
      badge: document.querySelector('.preview-name .badge')?.textContent,
      h1: document.querySelector('#preview-article h1')?.textContent,
      articleHtml: document.querySelector('#preview-article')?.innerHTML.slice(0, 500),
      downloadName: document.querySelector('#preview-download')?.getAttribute('download'),
      splitHandleVisible: getComputedStyle(document.querySelector('#split-handle')).display !== 'none',
    };
  }),
);
await page.screenshot({ path: join(outDir, 'report-open.png') });
await page.click('#preview-close');
await page.waitForTimeout(400);

await page.click('#progress-toggle');
await page.waitForTimeout(600);
await snap('progressPanel', () =>
  page.evaluate(() => ({
    open: document.querySelector('#progress-layer')?.dataset.open,
    width: Math.round(document.querySelector('#progress-panel').getBoundingClientRect().width),
    text: document.querySelector('#progress-panel')?.innerText?.replace(/\n/g, ' | ').slice(0, 900),
    steps: document.querySelectorAll('.progress-step').length,
    tabs: [...document.querySelectorAll('.segmented-item')].map((node) => node.textContent.trim()),
  })),
);
await page.click('.segmented-item:nth-child(2)');
await page.waitForTimeout(300);
await snap('progressSources', () =>
  page.evaluate(() => document.querySelector('#progress-panel')?.innerText?.replace(/\n/g, ' | ').slice(0, 600)),
);
await page.keyboard.press('Escape');
await page.waitForTimeout(300);

await page.screenshot({ path: join(outDir, 'final.png') });
dump.consoleErrors = consoleErrors;
writeFileSync(join(outDir, 'probe.json'), `${JSON.stringify(dump, null, 2)}\n`, 'utf8');
console.log(`probe written to ${outDir}`);
console.log(JSON.stringify(dump, null, 2));

await browser.close();
const exit = await host.stop();
console.log(`fixture stopped: ${JSON.stringify(exit)}`);
