/**
 * Tiny text helpers for the Markdown renderer and the workbench shell.
 *
 * The workbench renders report Markdown itself; there is no locale negotiation
 * here, so the only helpers that survive are the two the renderer needs:
 * HTML escaping and URL allow-listing. Keeping the module this small is
 * deliberate — nothing in the front-end may depend on a product-specific
 * presentation stack.
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
