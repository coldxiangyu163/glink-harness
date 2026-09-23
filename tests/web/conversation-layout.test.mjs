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

test('process and its step rows fill the available conversation pane, while answers stay readable', () => {
  assert.match(rule('.conversation'), /(?:^|;)\s*width:\s*100%/);
  assert.match(rule('.pi-step-row'), /max-width:\s*100%/);
  assert.match(rule('.pi-thinking-body'), /max-width:\s*100%/);
  assert.match(rule('.pi-tool-detail-wrap'), /max-width:\s*100%/);
  assert.match(rule('.pi-answer'), /max-width:\s*var\(--conversation-width\)/);
});
