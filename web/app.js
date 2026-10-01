// Entry point. Implementation modules have no import-time DOM effects, and the
// bootstrap below is the only code that touches `document` before the workbench
// exists — so a failure here can always show the recovery face.

// Shown only when the module graph itself fails to load. The copy stays
// hard-coded because no module (and no translator) is available on this path.
const BOOT_FAILURE = Object.freeze({
  title: '页面没有加载完整',
  detail: '页面的核心部分没能加载。请确认服务正在运行，然后重试。',
  retry: '重试',
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
    const [{ createWorkspace }, { createAuthView }, { createApi }] = await Promise.all([
      import('./modules/workspace.mjs'),
      import('./modules/auth.mjs'),
      import('./modules/api.mjs'),
    ]);
    const api = createApi();
    const auth = createAuthView({ api });
    const workspace = createWorkspace({ api, auth });
    // A successful login hands the session back to the workspace, which reveals
    // the shell only after `GET /api/me` has answered for that identity.
    auth.onSession((me) => workspace.enterSession(me));
    await workspace.init();
    // Exposed for the browser regression (tests/browser/*.mjs) and for manual
    // inspection; nothing in production code reads it back.
    globalThis.__glinkHarness = workspace;
  } catch (error) {
    console.error('[workbench] bootstrap failed', error);
    showBootFailure();
  } finally {
    starting = false;
  }
}

start();
