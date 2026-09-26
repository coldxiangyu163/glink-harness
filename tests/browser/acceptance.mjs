/**
 * Browser acceptance suite (各维度全量浏览器测试).
 *
 * Drives the production front-end in Chromium against an isolated real host
 * (`tests/fixtures/accounts_server.py`: real Runner/RunStore/EventBus/HTTP/SSE,
 * scripted fake execution face — no pi, no Docker, no sandbox, no model call)
 * and records every check as JSON evidence.
 *
 * Usage:
 *   node tests/browser/acceptance.mjs --mode accounts|local [--out <dir>] [--only a,b]
 *
 * Playwright is project-local and lockfile-pinned (`tests/browser/package.json`);
 * this suite is not part of `scripts/check.sh`.
 */

import { chromium } from 'playwright';
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

import { Suite } from './support/harness.mjs';
import { startHost } from './support/host.mjs';
import { newSession, signIn, waitShell } from './support/flows.mjs';
import * as identity from './dimensions/identity.mjs';
import * as shell from './dimensions/shell.mjs';
import * as lifecycle from './dimensions/lifecycle.mjs';
import * as report from './dimensions/report.mjs';
import * as subagent from './dimensions/subagent.mjs';
import * as tenancy from './dimensions/tenancy.mjs';
import * as resilience from './dimensions/resilience.mjs';
import * as gateway from './dimensions/gateway.mjs';

const REPO_ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..');

function argValue(flag, fallback) {
  const index = process.argv.indexOf(flag);
  return index >= 0 && process.argv[index + 1] ? process.argv[index + 1] : fallback;
}

const mode = argValue('--mode', 'accounts');
if (!['accounts', 'local', 'gateway'].includes(mode)) {
  console.error(`[fail] --mode must be accounts|local|gateway, got ${mode}`);
  process.exit(2);
}
const only = argValue('--only', '')
  .split(',')
  .map((item) => item.trim())
  .filter(Boolean);
const wanted = (name) => only.length === 0 || only.includes(name);

const outDir = argValue('--out', mkdtempSync(join(tmpdir(), `pi-glink-acceptance-${mode}-`)));
mkdirSync(outDir, { recursive: true });
const stateDir = join(outDir, 'state');
mkdirSync(stateDir, { recursive: true });
const requestLog = join(outDir, 'requests.jsonl');

const host = await startHost({ repoRoot: REPO_ROOT, mode, stateDir, logFile: join(outDir, 'fixture.log'), requestLog });
host.requestLog = requestLog;
const browser = await chromium.launch();

const suite = new Suite({ baseUrl: host.baseUrl, outDir, mode });
const ctx = {
  mode,
  suite,
  browser,
  baseUrl: host.baseUrl,
  fixture: host,
  outDir,
  requestLog,
  removeSessionFile(runId) {
    rmSync(join(stateDir, 'data', 'sessions', runId, 'session.jsonl'), { force: true });
  },
};

try {
  if (mode === 'gateway') {
    // No in-app identity: the whole runnable surface is the unauthenticated
    // gateway face plus the static assets behind it.
    if (wanted('gateway')) await gateway.run(ctx);
    if (wanted('assets')) {
      suite.dimension('assets');
      const { context, page } = await newSession(ctx);
      const responses = [];
      page.on('response', (response) => responses.push({ url: response.url(), status: response.status() }));
      await page.goto(host.baseUrl, { waitUntil: 'networkidle' });
      await page.waitForTimeout(800);
      const failed = responses.filter((item) => item.status >= 400 && !/\/api\/me$/.test(item.url));
      const loaded = responses.filter((item) => /\.(css|mjs|svg|png|woff2?)$/.test(item.url));
      suite.record(
        '静态资产：前端资源全部 200（CSS/模块/图标/字体）',
        failed.length === 0 && loaded.length >= 5,
        { failed, loadedCount: loaded.length },
      );
      const unexpected = suite.consoleErrors.filter((line) => !/401/.test(line));
      suite.record('网关入口控制台无错误（401 探测除外）', unexpected.length === 0, unexpected.slice(0, 5));
      await suite.shot(page, 'gateway-assets');
      await context.close();
    }
    const { jsonPath, summary } = suite.write();
    console.log('');
    console.log(`结果: ${summary.passed} 通过 / ${summary.failed} 失败 / ${summary.skipped} 跳过（共 ${summary.total}）`);
    console.log(`证据: ${jsonPath}`);
    await browser.close().catch(() => {});
    const exitState = await host.stop();
    writeFileSync(join(outDir, 'host-exit.json'), `${JSON.stringify({ pid: host.pid, ...exitState }, null, 2)}\n`, 'utf8');
    console.log(`隔离宿主已停止（pid ${host.pid}，exit=${JSON.stringify(exitState)}）`);
    process.exit(summary.failed > 0 ? 1 : 0);
  }
  if (mode === 'accounts') {
    if (wanted('identity')) {
      ctx.identity = await identity.run(ctx);
    } else if (wanted('tenancy')) {
      // tenancy needs the member account; create it silently when identity is skipped
    }
  } else {
    suite.dimension('identity');
    suite.skip('账号身份面（登录/邀请/注册/注销）', 'local 模式没有站内账号');
  }

  const { context: mainContext, page: main } = await newSession(ctx);
  ctx.context = mainContext;
  ctx.page = main;
  if (mode === 'accounts') {
    await signIn(ctx, main);
  } else {
    await main.goto(host.baseUrl, { waitUntil: 'domcontentloaded' });
    await main.waitForSelector('#app-shell:not([hidden])', { timeout: 15_000 });
    await main.waitForTimeout(400);
    const localFace = await main.evaluate(() => ({
      authHidden: document.querySelector('#auth-view')?.hidden,
      shellVisible: !document.querySelector('#app-shell')?.hidden,
      accountHidden: document.querySelector('#account-area')?.hidden,
    }));
    suite.dimension('identity');
    suite.record(
      '本地模式入口：无登录面、无账号区、直接进入工作台',
      localFace.authHidden === true && localFace.shellVisible && localFace.accountHidden === true,
      localFace,
    );
    const me = await main.evaluate(async () => (await fetch('/api/me', { headers: { Accept: 'application/json' } })).json());
    suite.record('本地模式 /api/me：mode=local、未认证', me.mode === 'local' && me.authenticated === false, me);
  }

  if (wanted('shell')) await shell.run(ctx);
  if (wanted('lifecycle')) ctx.lifecycle = await lifecycle.run(ctx);
  // The subagent run carries the measurable duration the report/progress
  // dimensions need, so it runs before them.
  if (wanted('subagent')) await subagent.run(ctx);
  if (wanted('report')) await report.run(ctx);
  if (mode === 'accounts') {
    if (wanted('tenancy')) await tenancy.run(ctx);
  } else if (wanted('tenancy')) {
    suite.dimension('tenancy');
    suite.skip('跨租户与角色门禁', 'local 模式没有租户身份');
  }
  if (wanted('resilience')) await resilience.run(ctx);
} catch (error) {
  suite.dimension('suite');
  suite.record('套件执行异常（后续检查未执行）', false, { message: String(error), stack: String(error?.stack ?? '').slice(0, 1200) });
  await suite.shot(ctx.page, 'suite-failure').catch(() => {});
} finally {
  const { jsonPath, summary } = suite.write();
  console.log('');
  console.log(`结果: ${summary.passed} 通过 / ${summary.failed} 失败 / ${summary.skipped} 跳过（共 ${summary.total}）`);
  console.log(`证据: ${jsonPath}`);
  console.log(`截图与日志目录: ${outDir}`);
  await browser.close().catch(() => {});
  const exitState = await host.stop();
  writeFileSync(
    join(outDir, 'host-exit.json'),
    `${JSON.stringify({ pid: host.pid, ...exitState }, null, 2)}\n`,
    'utf8',
  );
  console.log(`隔离宿主已停止（pid ${host.pid}，exit=${JSON.stringify(exitState)}）`);
  process.exit(summary.failed > 0 ? 1 : 0);
}
