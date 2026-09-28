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

/** Allow only http(s), mailto and same-origin relative URLs; everything else is dropped. */
export function safeUrl(value) {
  const text = String(value ?? '').trim();
  if (!text) return '';
  // Browsers strip tab/CR/LF anywhere in a URL and treat `\` as `/`, so decide
  // on the normalised form. `//host` (however it is spelled) is a
  // protocol-relative reference to another origin, not a site-relative path;
  // without this it would slip through the `/` prefix branch below.
  const reference = text.replace(/[\t\n\r]/g, '').replaceAll('\\', '/');
  if (reference.startsWith('//')) return '';
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
