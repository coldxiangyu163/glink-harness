/**
 * XSS regressions for the hand-written Markdown renderer (M14).
 *
 * `markdown.mjs` is the only path in the repo that turns model output into
 * `innerHTML`: `run-view.mjs` renders closed tool/text blocks and
 * `workspace.mjs` renders the fetched `report.md` preview. The safety claim in
 * its header — every text node is escaped before it is emitted and every URL
 * goes through `safeUrl` — is therefore load-bearing, yet only two happy-path
 * assertions covered it (`run-stream.test.mjs`). These tests pin the escaping
 * and URL allow-list contracts directly, then drive the whole pipeline with the
 * hostile vectors a model or a report could emit.
 *
 * They run in Node without a document, like the other `tests/web` suites.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';

import { escapeHtml, safeUrl } from '../../web/modules/ui.mjs';
import { renderMarkdown } from '../../web/modules/markdown.mjs';

//: The tags the renderer itself is allowed to emit. Everything else in the
//: output must be entity text, so stripping these tags must leave no raw `<`/`>`
//: behind — that is the "no bare tag injection" oracle below.
const RENDERED_TAG =
  /<\/?(?:p|h[1-6]|strong|em|del|code|pre|blockquote|ul|ol|li|hr|a|img|div|table|thead|tbody|tr|th|td)(?:\s[^<>]*)?>/g;

/** Opening tags in `html`; every `<` in the output belongs to one of these. */
function renderedTags(html) {
  return [...html.matchAll(/<[a-z][^<>]*>/g)].map((match) => match[0]);
}

/** Attribute pairs of the first `<name ...>` tag in `html`. */
function tagAttributes(html, name) {
  const match = html.match(new RegExp(`<${name}\\b([^>]*)>`));
  assert.ok(match, `expected a <${name}> tag in ${JSON.stringify(html)}`);
  return [...match[1].matchAll(/([a-zA-Z-]+)="([^"]*)"/g)].map(([, key, value]) => [key, value]);
}

test('escapeHtml neutralises tags and both quote contexts', () => {
  assert.equal(escapeHtml('<script>alert(1)</script>'), '&lt;script&gt;alert(1)&lt;/script&gt;');
  assert.equal(escapeHtml('</td></tr><img src=x>'), '&lt;/td&gt;&lt;/tr&gt;&lt;img src=x&gt;');
  // Attribute contexts: a double-quoted attribute cannot be closed with `"`,
  // a single-quoted one cannot be closed with `'`.
  assert.equal(escapeHtml('a" onmouseover="alert(1)'), 'a&quot; onmouseover=&quot;alert(1)');
  assert.equal(escapeHtml("a' onfocus='alert(1)"), 'a&#39; onfocus=&#39;alert(1)');
});

test('escapeHtml escapes the ampersand first, so entities cannot round-trip', () => {
  assert.equal(escapeHtml('&'), '&amp;');
  assert.equal(escapeHtml('&lt;script&gt;'), '&amp;lt;script&amp;gt;');
  assert.equal(escapeHtml('&amp;'), '&amp;amp;');
  assert.equal(escapeHtml('&#x3c;script&#x3e;'), '&amp;#x3c;script&amp;#x3e;');
  assert.equal(escapeHtml('&quot; onmouseover=&quot;'), '&amp;quot; onmouseover=&amp;quot;');
  // The order matters: escaping `<`/`>`/`"` first would leave a raw `&`.
  assert.ok(!escapeHtml('&lt;').includes('&lt;'), 'the literal entity must not survive as markup');
});

test('escapeHtml coerces absent values instead of throwing', () => {
  assert.equal(escapeHtml(null), '');
  assert.equal(escapeHtml(undefined), '');
  assert.equal(escapeHtml(0), '0');
});

test('safeUrl drops script-bearing and non-allowlisted schemes', () => {
  for (const value of [
    'javascript:alert(1)',
    'JavaScript:alert(1)',
    'JAVASCRIPT:alert(1)',
    '  javascript:alert(1)  ',
    'data:text/html,<script>alert(1)</script>',
    'DATA:image/svg+xml,<svg/onload=alert(1)>',
    'vbscript:msgbox(1)',
    'VBScript:MsgBox(1)',
    'file:///etc/passwd',
    'blob:https://page.example/abcd',
    'about:blank',
  ]) {
    assert.equal(safeUrl(value), '', `must drop ${JSON.stringify(value)}`);
  }
});

test('safeUrl drops whitespace-obfuscated scheme prefixes', () => {
  // Browsers strip tab/CR/LF anywhere in a URL before parsing the scheme, so a
  // renderer that only tested the raw string would wave these through.
  for (const value of [
    'java\tscript:alert(1)',
    'java\nscript:alert(1)',
    'java\rscript:alert(1)',
    'jav\ta\nscript:alert(1)',
    '\u0001javascript:alert(1)',
    '\tjavascript:alert(1)',
    '\njavascript:alert(1)',
    ' javascript:alert(1) ',
  ]) {
    assert.equal(safeUrl(value), '', `must drop ${JSON.stringify(value)}`);
  }
});

test('safeUrl drops protocol-relative URLs: //host is another origin, not a relative path', () => {
  // `//evil.example/x` resolves against the page scheme and yields
  // https://evil.example/x, so an injected image or link would make the
  // reader's browser contact a third-party origin. Browsers normalise `\` to
  // `/` and strip tab/CR/LF, so every spelling below must be refused.
  for (const value of [
    '//evil.example/x',
    '//evil.example',
    '/\t/evil.example/x',
    '/\n/evil.example/x',
    '/\\evil.example/x',
    '\\\\evil.example/x',
    '/\\/evil.example/x',
  ]) {
    assert.equal(safeUrl(value), '', `must drop ${JSON.stringify(value)}`);
  }
});

test('safeUrl keeps http(s), mailto, anchors and explicitly relative paths', () => {
  for (const value of [
    'https://example.com/a?b=1#c',
    'http://example.com/',
    'HTTPS://EXAMPLE.COM/report',
    'mailto:author@example.com?subject=hi',
    '#section-3',
    '/reports/2026-09-28.md',
    './chart.png',
    '../assets/diagram.svg',
  ]) {
    assert.equal(safeUrl(value), value, `must keep ${JSON.stringify(value)}`);
  }
  assert.equal(safeUrl(''), '');
  assert.equal(safeUrl('   '), '');
  assert.equal(safeUrl(null), '');
  // A tab inside a *single*-slash path is not a second origin: browsers strip
  // it to `/evil.example/x`, still same-origin, so it must stay allowed.
  assert.equal(safeUrl('/\tevil.example/x'), '/\tevil.example/x');
});

test('renderMarkdown escapes raw HTML in every block context', () => {
  const payload = '<img src=x onerror=alert(1)>';
  const sources = [
    payload,
    `# ${payload}`,
    `> ${payload}`,
    `- ${payload}`,
    `**${payload}**`,
    `${payload}\n\nnext paragraph`,
    `| ${payload} | b |\n| --- | --- |\n| c | d |`,
  ];
  for (const source of sources) {
    const html = renderMarkdown(source);
    assert.ok(!html.includes('<img'), `raw <img> leaked from ${JSON.stringify(source)}`);
    assert.ok(html.includes('&lt;img src=x onerror=alert(1)&gt;'), `payload not escaped in ${JSON.stringify(source)}`);
  }
});

test('code fences escape both the body and the language class', () => {
  const html = renderMarkdown('```js"><img/x>\n</code></pre><script>alert(1)</script>\n```');
  assert.match(html, /^<pre><code class="language-js&quot;&gt;&lt;img\/x&gt;">/);
  assert.ok(html.includes('&lt;/code&gt;&lt;/pre&gt;&lt;script&gt;alert(1)&lt;/script&gt;'), 'fence body must be entity text');
  assert.ok(!html.includes('<script'), 'a fence must not open a script element');
  assert.ok(!/<code class="[^"]*"[^>]*\son[a-z]+=/i.test(html), 'the language class must not add attributes');
});

test('link URLs cannot break out of the href attribute', () => {
  const html = renderMarkdown('[click](https://a.example/q?x=1"onmouseover="alert(1))');
  const attributes = tagAttributes(html, 'a');
  assert.deepEqual(attributes.map(([key]) => key), ['href', 'target', 'rel']);
  const [, href] = attributes[0];
  assert.ok(href.includes('&quot;'), 'the quote from the URL must be escaped in the attribute');
  assert.ok(!href.includes('"'), 'no raw quote may close the href attribute');
  assert.ok(!/\son[a-z]+\s*=\s*"/i.test(renderedTags(html).join(' ')), 'no injected event handler attribute');
});

test('image URLs and alt text cannot break out of their attributes', () => {
  const fromUrl = renderMarkdown('![alt](https://a.example/i.png"onerror="alert(1))');
  assert.deepEqual(tagAttributes(fromUrl, 'img').map(([key]) => key), ['src', 'alt', 'loading']);
  assert.ok(!fromUrl.includes('onerror="'), 'no injected event handler attribute from the URL');

  const fromAlt = renderMarkdown('![x"onerror="alert(1)](https://a.example/i.png)');
  const [, alt] = tagAttributes(fromAlt, 'img')[1];
  assert.equal(alt, 'x&quot;onerror=&quot;alert(1)');
});

test('javascript:/data: links and images are dropped, keeping escaped text', () => {
  assert.ok(!renderMarkdown('[click](javascript:alert(1))').includes('<a'));
  assert.ok(!renderMarkdown('[click](JaVaScRiPt:alert(1))').includes('<a'));
  assert.ok(!renderMarkdown('[click](vbscript:msgbox(1))').includes('<a'));
  assert.ok(!renderMarkdown('[click](data:text/html,<script>alert(1)</script>)').includes('<a'));
  assert.ok(!renderMarkdown('![x](javascript:alert(1))').includes('<img'));
  assert.ok(!renderMarkdown('![x](data:image/svg+xml,<svg/onload=alert(1)>)').includes('<img'));
  // The link text still renders, but only as escaped text.
  assert.equal(renderMarkdown('[<img src=x onerror=alert(1)>](javascript:x)'), '<p>&lt;img src=x onerror=alert(1)&gt;</p>');
  // An allowed href whose *text* is markup is escaped too.
  const html = renderMarkdown('[<img src=x onerror=alert(1)>](https://ok.example)');
  assert.match(html, /<a href="https:\/\/ok\.example"[^>]*>&lt;img src=x onerror=alert\(1\)&gt;<\/a>/);
});

test('protocol-relative assets and links never reach the reader as raw href/src', () => {
  const image = renderMarkdown('![shot](//evil.example/track.png)');
  assert.ok(!image.includes('<img'), 'a protocol-relative image must not load another origin');
  const link = renderMarkdown('[x](//evil.example)');
  assert.ok(!link.includes('<a'), 'a protocol-relative link must not be emitted as an external href');
});

test('every tag in renderMarkdown output comes from the renderer, not the input', () => {
  const hostile = [
    '<script>alert(1)</script>',
    '<div onmouseover="alert(1)">x</div>',
    '# <img src=x onerror=alert(1)>',
    '- <svg/onload=alert(1)>',
    '> </blockquote><script>alert(1)</script>',
    '[x](https://a.example/a"onmouseover="alert(1))',
    '![x"onerror="alert(1)](https://a.example/i.png)',
    '| <img src=x onerror=alert(1)> | b |\n| --- | --- |\n| c | d |',
    '```\n</code></pre><script>alert(1)</script>\n```',
    '<!-- keep --><iframe src="javascript:alert(1)"></iframe>',
  ];
  for (const source of hostile) {
    const html = renderMarkdown(source);
    const leftover = html.replace(RENDERED_TAG, '');
    assert.ok(!/[<>]/.test(leftover), `unescaped markup leaked from ${JSON.stringify(source)}: ${JSON.stringify(leftover)}`);
  }
});

test('the innerHTML consumer path (model text -> renderMarkdown -> innerHTML) stays inert', () => {
  // Mirrors run-view.mjs (`node.innerHTML = renderMarkdown(item.text)`) and
  // workspace.mjs (`previewArticle.innerHTML = renderMarkdown(text)`): the whole
  // hostile document is handed to the renderer in one go.
  const report = [
    '# <script>alert(1)</script>',
    '<img src=x onerror=alert(1)>',
    '[link](javascript:alert(1))',
    '![x](//evil.example/track.png)',
    '![x](data:image/svg+xml,<svg/onload=alert(1)>)',
    '**<script>alert(2)</script>**',
    '```html\n<script>alert(3)</script>\n```',
  ].join('\n\n');
  const html = renderMarkdown(report);
  assert.ok(!/<script|<iframe|<svg|<img\s+src=x/i.test(html), 'no injected element may survive');
  for (const tag of renderedTags(html)) {
    assert.ok(!/\son[a-z]+\s*=/i.test(tag), `no event handler attribute may survive: ${tag}`);
    assert.ok(!/(?:href|src)="\s*javascript:/i.test(tag), `no javascript: URL may survive: ${tag}`);
    assert.ok(!/(?:href|src)="\/\//.test(tag), `no protocol-relative origin may survive: ${tag}`);
  }
  assert.ok(html.includes('&lt;script&gt;alert(1)&lt;/script&gt;'), 'the payload must be visible as escaped text');
});
