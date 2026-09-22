/**
 * Minimal message lookup for a single-language build.
 *
 * The workbench ships Chinese copy inline; `t()` therefore only performs
 * parameter interpolation and returns its input verbatim for anything it does
 * not have a translation for (including HTML fragments handed in by the report
 * renderer). Keeping this tiny is deliberate: the previous i18n stack — locale
 * negotiation, vendored runtime, per-key catalogs — belongs to a product with
 * a second language, which this host does not have yet.
 */

const MESSAGES = Object.freeze({
  'common:empty': '无',
  'report:table.more': '展开其余行',
  'report:formula.summary': '公式',
});

function interpolate(template, params) {
  if (!params) return template;
  return template.replace(/\{(\w+)\}/g, (match, key) =>
    Object.prototype.hasOwnProperty.call(params, key) ? String(params[key]) : match,
  );
}

export function t(value, params) {
  if (typeof value !== 'string') return String(value ?? '');
  const template = Object.prototype.hasOwnProperty.call(MESSAGES, value) ? MESSAGES[value] : value;
  return interpolate(template, params);
}

export function getLocale() {
  return 'zh-CN';
}

export const LOCALE_STORAGE_KEY = 'pi-glink.uiLocale';
