/**
 * Recording and assertion helpers for the browser acceptance suite.
 *
 * Every check is one `record()` line with a machine-readable detail payload;
 * the suite writes them to JSON so the report quotes evidence instead of
 * re-deriving it. Failures never stop the run: a broken surface should be
 * visible next to the surfaces that still work.
 */

import { mkdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

export class Suite {
  constructor({ baseUrl, outDir, mode }) {
    this.baseUrl = baseUrl;
    this.outDir = outDir;
    this.mode = mode;
    this.checks = [];
    this.consoleErrors = [];
    this.currentDimension = 'init';
    this.startedAt = new Date().toISOString();
    mkdirSync(outDir, { recursive: true });
  }

  dimension(name) {
    this.currentDimension = name;
  }

  record(name, ok, detail) {
    const check = {
      dimension: this.currentDimension,
      name,
      ok: Boolean(ok),
      ...(detail === undefined ? {} : { detail }),
    };
    this.checks.push(check);
    const rendered = typeof detail === 'string' ? detail : JSON.stringify(detail ?? '');
    console.log(`${ok ? '[ok]  ' : '[fail]'} ${this.currentDimension} · ${name}${rendered ? ` · ${rendered}` : ''}`);
    return check;
  }

  skip(name, reason) {
    this.checks.push({ dimension: this.currentDimension, name, ok: true, skipped: true, detail: reason });
    console.log(`[skip] ${this.currentDimension} · ${name} · ${reason}`);
  }

  async shot(page, name) {
    const path = join(this.outDir, `${name}.png`);
    await page.screenshot({ path, fullPage: false }).catch(() => {});
    return path;
  }

  /** Attach console/page error collection to one page. */
  watch(page) {
    page.on('pageerror', (error) => this.consoleErrors.push(`pageerror: ${String(error)}`));
    page.on('console', (message) => {
      if (message.type() === 'error') this.consoleErrors.push(`console: ${message.text()}`);
    });
    return page;
  }

  summary() {
    const passed = this.checks.filter((check) => check.ok && !check.skipped).length;
    const skipped = this.checks.filter((check) => check.skipped).length;
    const failed = this.checks.filter((check) => !check.ok).length;
    return { total: this.checks.length, passed, skipped, failed };
  }

  write() {
    const summary = this.summary();
    const payload = {
      baseUrl: this.baseUrl,
      mode: this.mode,
      startedAt: this.startedAt,
      finishedAt: new Date().toISOString(),
      dimensions: [...new Set(this.checks.map((check) => check.dimension))],
      summary,
      checks: this.checks,
      consoleErrors: this.consoleErrors,
    };
    const jsonPath = join(this.outDir, 'acceptance.json');
    writeFileSync(jsonPath, `${JSON.stringify(payload, null, 2)}\n`, 'utf8');
    const lines = [
      `glink-harness 浏览器验收 · mode=${this.mode}`,
      `baseUrl: ${this.baseUrl}`,
      `命令: node tests/browser/acceptance.mjs --mode ${this.mode}`,
      `时间: ${this.startedAt} → ${payload.finishedAt}`,
      '',
      ...this.checks.map((check) => {
        const tag = check.skipped ? '[skip]' : check.ok ? '[ok]  ' : '[fail]';
        const detail = check.detail === undefined ? '' : ` · ${typeof check.detail === 'string' ? check.detail : JSON.stringify(check.detail)}`;
        return `${tag} ${check.dimension} · ${check.name}${detail}`;
      }),
      '',
      `结果: ${summary.passed} 通过 / ${summary.failed} 失败 / ${summary.skipped} 跳过（共 ${summary.total}）`,
      `console 错误: ${this.consoleErrors.length}`,
      '',
    ];
    writeFileSync(join(this.outDir, 'acceptance.txt'), lines.join('\n'), 'utf8');
    return { jsonPath, summary };
  }
}

/** Poll `predicate` until it returns a truthy value (or the timeout wins). */
export async function waitFor(predicate, { timeout = 10_000, interval = 150, message = 'condition' } = {}) {
  const deadline = Date.now() + timeout;
  let last;
  for (;;) {
    try {
      last = await predicate();
      if (last) return last;
    } catch (error) {
      last = String(error);
    }
    if (Date.now() > deadline) {
      throw new Error(`timed out waiting for ${message} (last: ${JSON.stringify(last)})`);
    }
    await new Promise((resolve) => setTimeout(resolve, interval));
  }
}

/** Whether an element is present and rendered (visible to the user). */
export async function visible(page, selector) {
  return page.evaluate((sel) => {
    const node = document.querySelector(sel);
    if (!node) return false;
    if (node.hidden) return false;
    return node.offsetParent !== null || getComputedStyle(node).position === 'fixed';
  }, selector);
}

/** The visible body text with history/user-content containers masked out. */
export async function chromeText(page) {
  return page.evaluate(() => {
    const clone = document.body.cloneNode(true);
    for (const sel of ['#run-list', '#home-run-list', '#home-report-list', '#run-question', '#conversation', '#preview-article', '#prompt-input']) {
      clone.querySelector(sel)?.replaceChildren();
    }
    const holder = document.createElement('div');
    holder.style.cssText = 'position:absolute;left:-99999px;top:0;width:1512px';
    holder.append(clone);
    document.body.append(holder);
    const text = clone.innerText;
    holder.remove();
    return text;
  });
}
