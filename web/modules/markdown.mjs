/**
 * Minimal Markdown renderer for run text and report previews.
 *
 * Deliberately small: headings, paragraphs, emphasis, links, images, code,
 * lists, quotes, rules and pipe tables — what a research report actually
 * needs. It renders the Markdown pi wrote and nothing more: presentation
 * features that belong to a product stay out of the front-end.
 *
 * Safety: every text node is escaped before it is emitted and every URL goes
 * through `safeUrl`, so a report can never inject markup or a `javascript:`
 * URL. The module has no import-time side effects and never touches the DOM,
 * so Node tests import it directly.
 */

import { escapeHtml, safeUrl } from './ui.mjs';

const FENCE = /^(`{3,}|~{3,})\s*([^\s`~]*)\s*$/;
const HEADING = /^(#{1,6})\s+(.+?)\s*#*\s*$/;
const RULE = /^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/;
const QUOTE = /^\s{0,3}>\s?/;
const LIST_ITEM = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/;
const COMMENT = /^\s*<!--.*?-->\s*$/;
//: One pass over the inline grammar: code spans, images, then links. Anything
//: between tokens is plain text and gets escaped.
const INLINE_TOKEN = /(`[^`\n]+`)|(!\[[^\]\n]*\]\([^)\s]+\))|(\[[^\]\n]+\]\([^)\s]+\))/g;

function emphasize(escaped) {
  return escaped
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/__([^_]+)__/g, '<strong>$1</strong>')
    .replace(/(^|[^*\w])\*([^*\s][^*]*)\*(?=[^*\w]|$)/g, '$1<em>$2</em>')
    .replace(/(^|[^_\w])_([^_\s][^_]*)_(?=[^_\w]|$)/g, '$1<em>$2</em>')
    .replace(/~~([^~]+)~~/g, '<del>$1</del>');
}

function inline(value) {
  const source = String(value ?? '');
  let out = '';
  let last = 0;
  for (const match of source.matchAll(INLINE_TOKEN)) {
    out += emphasize(escapeHtml(source.slice(last, match.index)));
    const token = match[0];
    if (token.startsWith('`')) {
      out += `<code>${escapeHtml(token.slice(1, -1))}</code>`;
    } else if (token.startsWith('![')) {
      const image = token.match(/^!\[([^\]]*)\]\(([^)\s]+)\)$/);
      const href = safeUrl(image[2]);
      out += href
        ? `<img src="${escapeHtml(href)}" alt="${escapeHtml(image[1])}" loading="lazy">`
        : escapeHtml(image[1]);
    } else {
      const link = token.match(/^\[([^\]]+)\]\(([^)\s]+)\)$/);
      const href = safeUrl(link[2]);
      out += href
        ? `<a href="${escapeHtml(href)}" target="_blank" rel="noopener noreferrer">${emphasize(escapeHtml(link[1]))}</a>`
        : emphasize(escapeHtml(link[1]));
    }
    last = match.index + token.length;
  }
  out += emphasize(escapeHtml(source.slice(last)));
  return out;
}

function splitRow(line) {
  const source = String(line).trim().replace(/^\|/, '').replace(/\|\s*$/, '');
  const cells = [];
  let cell = '';
  for (let index = 0; index < source.length; index += 1) {
    if (source[index] === '\\' && source[index + 1] === '|') {
      cell += '|';
      index += 1;
      continue;
    }
    if (source[index] === '|') {
      cells.push(cell.trim());
      cell = '';
      continue;
    }
    cell += source[index];
  }
  cells.push(cell.trim());
  return cells;
}

function isDelimiterRow(line) {
  if (!line || !line.includes('-')) return false;
  return splitRow(line).every((cell) => /^:?-{1,}:?$/.test(cell));
}

/** Render a pipe table starting at ``start``; null when it is not one. */
function table(lines, start) {
  const header = lines[start];
  const delimiter = lines[start + 1];
  if (!header?.includes('|') || !delimiter || !isDelimiterRow(delimiter)) return null;
  const head = splitRow(header);
  const dividers = splitRow(delimiter);
  if (head.length < 2 || head.length !== dividers.length) return null;
  const align = dividers.map((cell) =>
    cell.startsWith(':') && cell.endsWith(':') ? 'center' : cell.endsWith(':') ? 'right' : cell.startsWith(':') ? 'left' : '',
  );
  const cell = (tag, value, index) =>
    `<${tag}${align[index] ? ` style="text-align:${align[index]}"` : ''}>${inline(value)}</${tag}>`;
  const rows = [];
  let index = start + 2;
  while (index < lines.length && lines[index].trim() && lines[index].includes('|')) {
    rows.push(splitRow(lines[index]));
    index += 1;
  }
  const html =
    `<div class="md-table-wrap"><table><thead><tr>${head.map((value, column) => cell('th', value, column)).join('')}</tr></thead>` +
    `<tbody>${rows.map((row) => `<tr>${head.map((_, column) => cell('td', row[column] ?? '', column)).join('')}</tr>`).join('')}</tbody></table></div>`;
  return { html, next: index };
}

/** Render a list block starting at ``start`` (nested items recurse). */
function list(lines, start) {
  const base = lines[start].match(/^\s*/)[0].length;
  const ordered = /^\s*\d/.test(lines[start]);
  const items = [];
  let current = null;
  let index = start;
  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) {
      const next = lines.slice(index + 1).find((candidate) => candidate.trim());
      if (next === undefined) {
        index = lines.length;
        break;
      }
      const nextIndent = next.match(/^\s*/)[0].length;
      const nextItem = next.match(LIST_ITEM);
      if (nextIndent <= base && (nextItem || !next.startsWith(' '))) break;
      if (current) current.lines.push('');
      index += 1;
      continue;
    }
    const marker = line.match(LIST_ITEM);
    if (marker) {
      const indent = marker[1].length;
      if (indent < base) break;
      if (indent === base) {
        if (/^\d/.test(marker[2]) !== ordered) break;
        current = { lines: [marker[3]] };
        items.push(current);
        index += 1;
        continue;
      }
    }
    if (!current) break;
    // Continuation or nested block: dedent two spaces so a nested list lands
    // at this recursion's base indent and is parsed by the recursive call.
    const indent = line.match(/^\s*/)[0].length;
    current.lines.push(line.slice(Math.min(indent, base + 2)));
    index += 1;
  }
  const tag = ordered ? 'ol' : 'ul';
  const body = items
    .map((item) => `<li>${renderBlocks(item.lines)}</li>`)
    .join('');
  return { html: `<${tag}>${body}</${tag}>`, next: index };
}

function isBlockStart(line, next) {
  const trimmed = line.trim();
  if (!trimmed) return true;
  if (FENCE.test(trimmed) || HEADING.test(trimmed) || RULE.test(line) || QUOTE.test(line)) return true;
  if (LIST_ITEM.test(line)) return true;
  if (line.includes('|') && next !== undefined && isDelimiterRow(next)) return true;
  return false;
}

function renderBlocks(lines) {
  const out = [];
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim() || COMMENT.test(line)) {
      index += 1;
      continue;
    }

    const fence = line.trim().match(FENCE);
    if (fence) {
      const marker = fence[1];
      const closing = new RegExp(`^\\s*${marker[0]}{${marker.length},}\\s*$`);
      const body = [];
      index += 1;
      while (index < lines.length && !closing.test(lines[index])) {
        body.push(lines[index]);
        index += 1;
      }
      index += 1; // consume the closing fence (or run off the end)
      const language = fence[2] ? ` class="language-${escapeHtml(fence[2].toLowerCase())}"` : '';
      out.push(`<pre><code${language}>${escapeHtml(body.join('\n'))}</code></pre>`);
      continue;
    }

    if (RULE.test(line)) {
      out.push('<hr>');
      index += 1;
      continue;
    }

    const heading = line.match(HEADING);
    if (heading) {
      const level = heading[1].length;
      out.push(`<h${level}>${inline(heading[2])}</h${level}>`);
      index += 1;
      continue;
    }

    if (QUOTE.test(line)) {
      const quoted = [];
      while (index < lines.length && QUOTE.test(lines[index])) {
        quoted.push(lines[index].replace(QUOTE, ''));
        index += 1;
      }
      out.push(`<blockquote>${renderBlocks(quoted)}</blockquote>`);
      continue;
    }

    const rendered = table(lines, index);
    if (rendered) {
      out.push(rendered.html);
      index = rendered.next;
      continue;
    }

    if (LIST_ITEM.test(line)) {
      const rendered = list(lines, index);
      out.push(rendered.html);
      index = rendered.next;
      continue;
    }

    const paragraph = [];
    while (index < lines.length && !isBlockStart(lines[index], lines[index + 1])) {
      paragraph.push(lines[index].trim());
      index += 1;
    }
    if (!paragraph.length) {
      // A block starter we did not claim above (defensive): emit it as text
      // rather than looping forever on the same line.
      paragraph.push(line.trim());
      index += 1;
    }
    out.push(`<p>${inline(paragraph.join('\n'))}</p>`);
  }
  return out.join('');
}

/** Convert Markdown to HTML. */
export function renderMarkdown(markdown) {
  const lines = String(markdown ?? '').replace(/\r\n?/g, '\n').split('\n');
  return renderBlocks(lines);
}
