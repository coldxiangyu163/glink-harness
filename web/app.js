// Keep the historical entry URL; implementation modules have no import-time DOM effects.
// Bootstrap is the only place that reads the device locale preference; consumers read the
// committed locale through ./modules/i18n.mjs.

// Shown only when the runtime or its core language resources fail to load, so the copy
// stays bilingual and hard-coded: t() is not available on this path. Retry reloads the page:
// browsers cache a failed module graph for the current document, so re-importing cannot recover it.
const BOOT_FAILURE = Object.freeze({
  title: '页面未能完整加载 / Page failed to load',
  detail: '核心脚本或语言资源未能加载。请确认本地服务正常运行后重试。 / Core scripts or language resources did not load. Check that the local service is running, then retry.',
  retry: '重试 / Retry',
});

function readStoredLocale(storageKey) {
  try {
    return globalThis.localStorage?.getItem(storageKey) ?? null;
  } catch {
    return null; // Storage unavailable: the session still works, the choice is just not remembered.
  }
}

function preferredLanguages() {
  const navigator = globalThis.navigator;
  if (!navigator) return [];
  if (Array.isArray(navigator.languages) && navigator.languages.length) return navigator.languages;
  return navigator.language ? [navigator.language] : [];
}

function showBootFailure() {
  if (document.getElementById('boot-failure')) return;
  const overlay = document.createElement('div');
  overlay.id = 'boot-failure';
  overlay.className = 'boot-failure';
  overlay.setAttribute('role', 'alert');
  const card = document.createElement('div');
  card.className = 'boot-failure-card';
  const title = document.createElement('h2');
  title.textContent = BOOT_FAILURE.title;
  const detail = document.createElement('p');
  detail.textContent = BOOT_FAILURE.detail;
  const retry = document.createElement('button');
  retry.type = 'button';
  retry.className = 'button primary';
  retry.textContent = BOOT_FAILURE.retry;
  retry.addEventListener('click', () => { location.reload(); });
  card.append(title, detail, retry);
  overlay.append(card);
  document.body.append(overlay);
}

async function bootstrap() {
  const i18n = await import('./modules/i18n.mjs');
  const locale = i18n.resolveInitialLocale({
    stored: readStoredLocale(i18n.LOCALE_STORAGE_KEY),
    languages: preferredLanguages(),
  });
  await i18n.setLocale(locale);
  i18n.applyStatic(document);
  // skip-link 的 reveal 属于视图层：它可见时的文案必须与 <html lang> 一致，因此由 workspace 在
  // syncDocumentLanguage() 与静态标注投影完成后揭示；module 加载或 init 失败时保持 hidden，
  // 交由既有 boot-failure 面表现。
  const { createWorkspace } = await import('./modules/workspace.mjs');
  await createWorkspace().init();
}

let starting = false;
async function start() {
  if (starting) return;
  starting = true;
  try {
    await bootstrap();
  } catch (error) {
    console.error('[glink] bootstrap failed', error);
    showBootFailure();
  } finally {
    starting = false;
  }
}

start();
