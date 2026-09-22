// Entry point. Implementation modules have no import-time DOM effects, and the
// bootstrap below is the only code that touches `document` before the workbench
// exists — so a failure here can always show the recovery face.

// Shown only when the module graph itself fails to load. The copy stays
// hard-coded because no module (and no translator) is available on this path.
const BOOT_FAILURE = Object.freeze({
  title: '页面未能完整加载 / Page failed to load',
  detail:
    '核心脚本未能加载。请确认本地服务在运行，然后重试。 / Core scripts did not load. Check that the local service is running, then retry.',
  retry: '重试 / Retry',
});

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
  retry.className = 'btn primary';
  retry.textContent = BOOT_FAILURE.retry;
  retry.addEventListener('click', () => {
    location.reload();
  });
  card.append(title, detail, retry);
  overlay.append(card);
  document.body.append(overlay);
}

let starting = false;

async function start() {
  if (starting) return;
  starting = true;
  try {
    const { createWorkspace } = await import('./modules/workspace.mjs');
    const workspace = createWorkspace();
    await workspace.init();
    // Exposed for the browser regression (tests/browser/*.mjs) and for manual
    // inspection; nothing in production code reads it back.
    globalThis.__piGlink = workspace;
  } catch (error) {
    console.error('[pi-glink] bootstrap failed', error);
    showBootFailure();
  } finally {
    starting = false;
  }
}

start();
