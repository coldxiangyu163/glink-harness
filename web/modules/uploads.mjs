/** uploads: product-owned browser module; no startup side effects.

Pure decisions and markup for attachment uploads, so the composer, the drawer and
the library view agree on limits, progress and scanned-document state.
*/

import { escapeHtml, formatBytes } from './ui.mjs';
import { t } from './i18n.mjs';

export const DEFAULT_UPLOAD_LIMIT = 50 * 1024 * 1024;

export function uploadLimitBytes(config) {
  const configured = Number(config?.max_upload_bytes);
  return Number.isFinite(configured) && configured > 0 ? configured : DEFAULT_UPLOAD_LIMIT;
}

/** Files over the server limit are rejected in the browser, before any bytes are sent. */
export function oversizedFiles(files, limit) {
  if (!limit) return [];
  return [...files].filter((file) => Number(file.size) > limit);
}

/** Only a drag that actually carries files becomes an upload; text drags stay native. */
export function filesFromDrag(event) {
  const types = [...(event?.dataTransfer?.types || [])];
  if (types.length && !types.includes('Files')) return [];
  return [...(event?.dataTransfer?.files || [])];
}

/** Scanned uploads parse on the host; this badge is the only place a user sees it. */
export function parseBadgeMarkup(item) {
  const parse = item?.parse || {};
  if (parse.status === 'pending') {
    const done = Number(parse.pages_done) || 0;
    const total = Number(parse.page_count) || 0;
    const progress = total
      ? t('workspace:upload.parsePages', { done, total })
      : t('workspace:upload.parsePreparing');
    return `<span class="parse-badge pending">${t('workspace:upload.parsePending', { progress })}</span>`;
  }
  if (parse.status === 'done') {
    return `<span class="parse-badge done">${t('workspace:upload.parseDone', { pages: Number(parse.pages) || 0 })}</span>`;
  }
  if (parse.status === 'failed') {
    const reason = parse.error || t('workspace:upload.parseFailedTitle');
    return `<span class="parse-badge failed" title="${escapeHtml(reason)}">${t('workspace:upload.parseFailed')}</span>`;
  }
  return '';
}

export function uploadRowDetail(row) {
  if (row.phase === 'done') return t('workspace:upload.rowDone');
  if (row.phase === 'failed') return row.error || t('errors:rowFailed');
  if (row.phase === 'parsing') return t('workspace:upload.rowParsing');
  const total = Number(row.total) || 0;
  if (!total) return t('workspace:upload.rowUploaded', { size: formatBytes(Number(row.loaded) || 0) });
  return `${Math.round((Number(row.loaded) / total) * 100)}% · ${formatBytes(Number(row.loaded) || 0)} / ${formatBytes(total)}`;
}

export function uploadRowsMarkup(rows) {
  return rows.filter((row) => row.visible).map((row) => {
    const total = Number(row.total) || 0;
    const settled = row.phase === 'done' || row.phase === 'failed';
    const bar = settled ? `<span class="upload-mark">${row.phase === 'done' ? '✓' : '✗'}</span>`
      : total && row.phase !== 'parsing'
        ? `<progress max="${total}" value="${Number(row.loaded) || 0}"></progress>`
        : '<progress></progress>';
    return `<div class="upload-row ${row.phase}"><span class="upload-name">${escapeHtml(`${row.label || ''}${row.name || ''}`)}</span>${bar}<span class="upload-detail">${escapeHtml(uploadRowDetail(row))}</span></div>`;
  }).join('');
}

// Materials have two lives: a deliberate library save versus a file that only ever
// fed one research run. The attachment picker offers both; the library page separates them.
export const LIBRARY_SCOPES = [
  { id: 'library', labelKey: 'workspace:library.scope.library', match: (item) => item.origin !== 'attachment' },
  { id: 'attachment', labelKey: 'workspace:library.scope.attachment', match: (item) => item.origin === 'attachment' },
  { id: 'all', labelKey: 'workspace:library.scope.all', match: () => true },
];

export function libraryScopeItems(library, scopeId) {
  const scope = LIBRARY_SCOPES.find((entry) => entry.id === scopeId) || LIBRARY_SCOPES[0];
  return [...(library || [])].filter(scope.match);
}

export function libraryCount(library) {
  return libraryScopeItems(library, 'library').length;
}

export function isRunAttachment(item) {
  return item?.origin === 'attachment';
}
