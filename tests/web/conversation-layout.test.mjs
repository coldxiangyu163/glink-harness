import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const css = readFileSync(new URL('../../web/styles.css', import.meta.url), 'utf8');
function rule(selector) {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const match = css.match(new RegExp(`(?:^|\\n)${escaped}\\s*\\{([^}]*)\\}`, 'm'));
  assert.ok(match, `missing ${selector}`);
  return match[1];
}

test('process and answer share the centered reading column; details stay within it', () => {
  assert.match(rule('.conversation'), /(?:^|;)\s*width:\s*100%/);
  for (const selector of ['.pi-process', '.pi-answer']) {
    assert.match(rule(selector), /max-width:\s*var\(--conversation-width\)/);
    assert.match(rule(selector), /margin-inline:\s*auto/);
  }
  assert.match(rule('.pi-step-row'), /max-width:\s*100%/);
  assert.match(rule('.pi-thinking-body'), /max-width:\s*100%/);
  assert.match(rule('.pi-tool-detail-wrap'), /max-width:\s*100%/);
});
