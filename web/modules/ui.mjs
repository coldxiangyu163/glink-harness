/**
 * Small DOM/text helpers used by the report renderer.
 *
 * The full workbench helpers were intentionally left behind with the previous
 * front-end; this module carries only what a renderer needs, so the report
 * pipeline can be imported by Node tests without a document.
 */

export function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

/** Allow only http(s), mailto and relative URLs; everything else is dropped. */
export function safeUrl(value) {
  const text = String(value ?? '').trim();
  if (!text) return '';
  if (text.startsWith('/') || text.startsWith('#') || text.startsWith('./') || text.startsWith('../')) {
    return text;
  }
  try {
    const url = new URL(text);
    if (url.protocol === 'http:' || url.protocol === 'https:' || url.protocol === 'mailto:') {
      return text;
    }
  } catch {
    return '';
  }
  return '';
}

export function formatBytes(bytes) {
  const value = Number(bytes);
  if (!Number.isFinite(value) || value < 0) return '';
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

export function formatDate(timestamp, locale = 'zh-CN') {
  const value = Number(timestamp);
  if (!Number.isFinite(value) || value <= 0) return '';
  const date = new Date(value * 1000);
  return date.toLocaleString(locale, {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  });
}
