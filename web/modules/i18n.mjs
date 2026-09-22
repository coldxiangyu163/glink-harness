/**
 * i18n: product-owned browser runtime over vendored i18next; no startup side effects.
 *
 * 默认实例只在显式 setLocale 后离开源语言；本模块不读取 localStorage/navigator，
 * locale 协商函数只处理调用方传入的值。切换是原子操作：先备齐目标语言目录，
 * 成功后一次提交并通知订阅者，失败或已被更新的切换取代时保留原语言。
 */
import { createInstance as createI18nextInstance } from './vendor/i18next-26.4.2.mjs';
import sourceCatalog from './locales/zh-CN.mjs';

export const SOURCE_LOCALE = 'zh-CN';
export const SUPPORTED_LOCALES = Object.freeze(['zh-CN', 'en']);
export const LOCALE_STORAGE_KEY = 'glink.uiLocale';
export const NAMESPACES = Object.freeze(Object.keys(sourceCatalog));

const LOCALE_LOADERS = Object.freeze({
  [SOURCE_LOCALE]: async () => sourceCatalog,
  en: async () => (await import('./locales/en.mjs')).default,
});

const LOCALE_TAG_PATTERNS = Object.freeze([
  [/^zh(?:-|$)/, 'zh-CN'],
  [/^en(?:-|$)/, 'en'],
]);

const STATIC_TEXT_SELECTOR = '[data-i18n]';
// 动态槽：内容由当前视图渲染函数按真实状态投影，静态标注不得覆盖。
// （chat 标题、页面标题、对话框标题、抽屉标题、预览标题都会在打开/导航/切语言时由视图设置；
// 预览标题是产物名（内容），永远不跟随语言。）
const VIEW_MANAGED_TEXT_SLOTS = Object.freeze(['#page-kicker', '#page-title', '#dialog-title', '#drawer-title', '#preview-title']);
const STATIC_ATTRIBUTE_TARGETS = Object.freeze({
  'data-i18n-aria-label': 'aria-label',
  'data-i18n-placeholder': 'placeholder',
  'data-i18n-title': 'title',
  'data-i18n-tip': 'data-tip',
});

function cloneCatalog(catalog) {
  return JSON.parse(JSON.stringify(catalog ?? {}));
}

/**
 * 失败提示取自当前已提交语言的资源（源语言静态导入总可用；其他语言只有成功加载后才可能成为当前语言）。
 * 兜底分支不依赖任何资源，是有意设计：目录或注入的 translator 不可用时仍要给出一条可读提示，
 * 不允许 raw key 或静默失败进入 UI。
 */
const LOCALE_SWITCH_MESSAGE_KEY = 'common:localeSwitchFailed';
const LOCALE_SWITCH_FALLBACK = 'Interface language could not be switched.';

export function localeSwitchFailure(locale = null, translator = t) {
  const code = matchSupportedLocale(locale) ?? SOURCE_LOCALE;
  let message = null;
  try {
    message = translator(LOCALE_SWITCH_MESSAGE_KEY, { lng: code });
  } catch {
    message = null;
  }
  // 资源缺失时 i18next 返回去掉了 namespace 的 key；这类返回值不得进入 UI。
  if (typeof message === 'string' && message && !message.includes('localeSwitchFailed')) return message;
  return LOCALE_SWITCH_FALLBACK;
}

/** 把 BCP 47 标签映射到受支持 locale；未支持的文字按语言族收口（zh-TW 显示简体）。 */
export function matchSupportedLocale(tag) {
  const value = String(tag ?? '').trim().toLowerCase();
  for (const [pattern, locale] of LOCALE_TAG_PATTERNS) {
    if (pattern.test(value)) return locale;
  }
  return null;
}

/** 启动解析：显式保存的受支持值优先，其次按 navigator 语言顺序协商，最后回到源语言。 */
export function resolveInitialLocale({ stored = null, languages = [] } = {}) {
  const storedLocale = matchSupportedLocale(stored);
  if (storedLocale) return storedLocale;
  for (const tag of Array.isArray(languages) ? languages : []) {
    const locale = matchSupportedLocale(tag);
    if (locale) return locale;
  }
  return SOURCE_LOCALE;
}

export function createI18n(options = {}) {
  const {
    locale = SOURCE_LOCALE,
    resources = { [SOURCE_LOCALE]: sourceCatalog },
    loaders = LOCALE_LOADERS,
  } = options;
  const host = createI18nextInstance();
  const listeners = new Set();
  const loaded = new Set(Object.keys(resources).filter((code) => matchSupportedLocale(code)));
  const warnedKeys = new Set();
  let current = matchSupportedLocale(locale) ?? SOURCE_LOCALE;
  let revision = 0;
  let disposed = false;

  host.init({
    lng: current,
    fallbackLng: SOURCE_LOCALE,
    supportedLngs: [...SUPPORTED_LOCALES],
    defaultNS: NAMESPACES[0],
    ns: [...NAMESPACES],
    resources: cloneCatalog(resources),
    interpolation: { escapeValue: false },
    returnNull: false,
    parseMissingKeyHandler(key) {
      if (!warnedKeys.has(key)) {
        warnedKeys.add(key);
        console.warn(`[i18n] missing key: ${key}`);
      }
      return key;
    },
  });

  function t(key, tOptions) {
    return host.t(key, tOptions);
  }

  function getLocale() {
    return current;
  }

  function subscribe(listener) {
    if (typeof listener !== 'function') throw new TypeError('i18n listener must be a function');
    listeners.add(listener);
    return () => listeners.delete(listener);
  }

  function installCatalog(locale, catalog) {
    for (const [namespace, entries] of Object.entries(catalog ?? {})) {
      host.addResourceBundle(locale, namespace, cloneCatalog(entries), true, true);
    }
  }

  async function setLocale(nextLocale) {
    if (disposed) throw new Error('i18n instance has been disposed');
    const locale = matchSupportedLocale(nextLocale);
    if (!locale) throw new RangeError(`unsupported locale: ${nextLocale}`);
    const ticket = ++revision;
    // 同值短路：locale 未变也返回当前值，由调用方（视图层）决定是否重申文档语言。
    if (locale === current && loaded.has(locale)) return current;
    if (!loaded.has(locale)) {
      const loadCatalog = loaders[locale];
      if (typeof loadCatalog !== 'function') throw new RangeError(`no catalog loader for locale: ${locale}`);
      const catalog = await loadCatalog();
      if (disposed || ticket !== revision) return current;
      installCatalog(locale, catalog);
      loaded.add(locale);
    }
    if (ticket !== revision) return current;
    current = locale;
    void host.changeLanguage(locale);
    // 实例只负责消息与 locale 状态：<html lang> 是文档级属性，由视图层唯一写入。
    for (const listener of [...listeners]) listener(locale);
    return current;
  }

  function dispose() {
    if (disposed) return;
    disposed = true;
    listeners.clear();
  }

  return Object.freeze({ t, getLocale, setLocale, subscribe, dispose });
}

const defaultI18n = createI18n();

export function t(key, tOptions) {
  return defaultI18n.t(key, tOptions);
}

export function getLocale() {
  return defaultI18n.getLocale();
}

export function setLocale(locale) {
  return defaultI18n.setLocale(locale);
}

export function subscribe(listener) {
  return defaultI18n.subscribe(listener);
}

export function dispose() {
  defaultI18n.dispose();
}

/** 应用 index.html 的 data-i18n* 标注：只改文本节点与属性，不重建 DOM。 */
export function applyStatic(root = null, translator = t) {
  const scope = root ?? (typeof document !== 'undefined' ? document : null);
  if (!scope || typeof scope.querySelectorAll !== 'function') return 0;
  const managed = typeof scope.querySelector === 'function'
    ? new Set(VIEW_MANAGED_TEXT_SLOTS.map((selector) => scope.querySelector(selector)).filter(Boolean))
    : new Set();
  let applied = 0;
  for (const element of scope.querySelectorAll(STATIC_TEXT_SELECTOR)) {
    const key = element.getAttribute('data-i18n');
    if (!key || managed.has(element)) continue;
    const text = translator(key);
    // Rewriting an unchanged text node would blur a focused control (the locale entry itself).
    if (element.textContent !== text) element.textContent = text;
    applied += 1;
  }
  for (const [attribute, target] of Object.entries(STATIC_ATTRIBUTE_TARGETS)) {
    for (const element of scope.querySelectorAll(`[${attribute}]`)) {
      const key = element.getAttribute(attribute);
      if (!key) continue;
      element.setAttribute(target, translator(key));
      applied += 1;
    }
  }
  return applied;
}
