/**
 * Workbench shell: run history, composer, live run view, report reader.
 *
 * The shell owns navigation and nothing else. All run semantics live in
 * `run-stream.mjs` (event → model) and `run-view.mjs` (model → DOM), so the
 * interesting behaviour is testable without a browser.
 *
 * Visual language comes from the 百鉴 UI standard library: 8px gutter + left
 * nav card + right work surface, 52px pane headers, 36px nav rows, the
 * library's 120px prompt input dock. See `web/styles.css` for the token layer.
 */

import { createApi } from './api.mjs';
import { applyEvent, createRunModel, createRunSubscription, TERMINAL_EVENT_TYPES, TERMINAL_STATUSES } from './run-stream.mjs';
import { createRunView } from './run-view.mjs';
import { renderMarkdown } from './markdown.mjs';

const MAX_PROMPT = 32000;
const SIDEBAR_KEY = 'pi-glink-sidebar';
const RECONNECT_LABELS = {
  idle: '',
  connecting: '连接事件流…',
  live: '事件流已连接',
  reconnecting: '事件流重连中…',
  closed: '事件流已关闭',
};

/** The icon the run-history row wears: every run is a conversation with pi. */
const RUN_ICON = 'chat';

export function createWorkspace({ api = createApi(), eventSourceFactory } = {}) {
  const el = (id) => document.getElementById(id);

  const shell = el('app-shell');
  const homeView = el('home-view');
  const chatView = el('chat-view');
  const composer = el('composer');
  const input = el('prompt-input');
  const sendButton = el('send-button');
  const promptCount = el('prompt-count');
  const runList = el('run-list');
  const homeRunList = el('home-run-list');
  const hostStatus = el('host-status');
  const hostStatusCard = el('host-status-card');
  const conversationScroll = el('conversation-scroll');
  const runHost = el('run-view');
  const question = el('run-question');
  const banner = el('run-banner');
  const bannerTitle = el('run-banner-title');
  const stopButton = el('stop-run');
  const streamStatus = el('stream-status');
  const previewDrawer = el('preview-drawer');
  const previewBody = el('preview-body');
  const previewTitle = el('preview-title');
  const previewDownload = el('preview-download');
  const previewClose = el('preview-close');
  const previewToggle = el('pane-preview-toggle');
  const paneTitle = el('pane-title');
  const composerDock = el('composer-dock');
  const composerHost = el('composer-host');
  const homeComposerSlot = el('home-composer-slot');
  const paneHome = el('pane-home');
  const sidebarToggle = el('sidebar-toggle');
  const toast = el('toast');

  let current = null; // { runId, model, subscription, view }
  let latestRuns = [];
  let toastTimer = null;
  let disposed = false;

  /* Bottom anchoring (标准库用 use-stick-to-bottom 做同一件事)：the conversation
   * follows new frames only while the reader is already at the tail. Scrolling up
   * is a deliberate act — one frame later the view must not yank them back. */
  const STICK_THRESHOLD = 80;
  const ANCHOR_SETTLE_MS = 800;
  let stick = true;
  let settleUntil = 0;
  const anchored = document.getElementById('conversation');
  const anchorFrame = () => {
    if (!stick || !anchored) return;
    // Folds and tool details grow with a transition, so one pin is not enough:
    // keep following for a moment, and stop the moment the reader scrolls away.
    settleUntil = Math.max(settleUntil, performance.now() + ANCHOR_SETTLE_MS);
    const step = () => {
      if (!stick) return;
      conversationScroll.scrollTop = conversationScroll.scrollHeight;
      if (performance.now() < settleUntil) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  };
  if (anchored && typeof MutationObserver === 'function') {
    new MutationObserver(anchorFrame).observe(anchored, {
      childList: true,
      subtree: true,
      characterData: true,
    });
    // `<details>` open/close is an attribute change, not a mutation, and Chrome's
    // scroll anchoring fights a height change above the viewport — a size observer
    // on the column is what actually catches "the tail moved" in every case.
    anchored.addEventListener('toggle', anchorFrame, true);
    if (typeof ResizeObserver === 'function') new ResizeObserver(anchorFrame).observe(anchored);
  }

  // -- navigation ------------------------------------------------------------

  function showView(name) {
    homeView.hidden = name !== 'home';
    chatView.hidden = name !== 'chat';
    composer.hidden = false;
    shell.dataset.view = name;
    /* The prompt input lives in the page flow on the landing view (title → input →
     * cards, 标准库助理首页的顺序) and docks above the conversation otherwise.
     * Move the whole host, not just the composer: the host owns the 680px measure. */
    if (name === 'home' && homeComposerSlot) {
      homeComposerSlot.append(composerHost);
      composerDock.hidden = true;
    } else {
      composerDock.append(composerHost);
      composerDock.hidden = false;
    }
    if (name === 'chat') stick = true;
    if (name === 'home') {
      paneTitle.textContent = '新研究';
      conversationScroll.scrollTop = 0;
      // On the landing view no run is "current": the history marker must go too,
      // or the sidebar claims a page the reader is not on.
      runList.querySelectorAll('[aria-current]').forEach((node) => node.removeAttribute('aria-current'));
    } else {
      paneTitle.textContent = current ? shortTitle(current.title) : '研究会话';
      // A reopened run starts at its tail; the stream keeps it anchored.
      stick = true;
      requestAnimationFrame(anchorFrame);
    }
  }

  function setSidebar(collapsed, { persist = true } = {}) {
    shell.dataset.sidebar = collapsed ? 'collapsed' : 'expanded';
    sidebarToggle.setAttribute('aria-expanded', String(!collapsed));
    const label = collapsed ? '展开侧栏' : '收起侧栏';
    sidebarToggle.setAttribute('aria-label', label);
    sidebarToggle.dataset.tip = label;
    if (!persist) return;
    try {
      localStorage.setItem(SIDEBAR_KEY, collapsed ? 'collapsed' : 'expanded');
    } catch {
      /* private mode / file: — the state still applies for this session */
    }
  }

  function showToast(message, level = 'info') {
    toast.textContent = message;
    toast.dataset.level = level;
    toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => {
      toast.hidden = true;
    }, 6000);
  }

  // -- run list --------------------------------------------------------------

  async function refreshHostStatus() {
    try {
      const health = await api.health();
      const config = await api.config();
      const parts = [`执行面 ${health.executor}`, `pi ${health.pi.available ? '可用' : '未找到'}`];
      if (config.model) parts.push(config.model);
      hostStatus.textContent = parts.join(' · ');
      const ready = health.configured.model ? 'true' : 'false';
      hostStatus.dataset.ready = ready;
      hostStatusCard.dataset.ready = ready;
    } catch (error) {
      hostStatus.textContent = `宿主不可达：${error.message}`;
      hostStatus.dataset.ready = 'false';
      hostStatusCard.dataset.ready = 'false';
    }
  }

  function renderRunList(runs) {
    latestRuns = runs;
    runList.replaceChildren();
    homeRunList.replaceChildren();
    if (!runs.length) {
      const empty = document.createElement('p');
      empty.className = 'empty-note';
      empty.textContent = '还没有运行记录。';
      runList.append(empty);
      return;
    }
    for (const run of runs) {
      runList.append(navRow(run));
      homeRunList.append(homeRow(run));
    }
  }

  function navRow(run) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'nav-item';
    button.dataset.runId = run.runId;
    button.dataset.status = run.status;
    button.title = run.prompt;
    if (current?.runId === run.runId) button.setAttribute('aria-current', 'page');

    const icon = document.createElement('span');
    icon.className = 'icon';
    icon.dataset.icon = RUN_ICON;
    icon.setAttribute('aria-hidden', 'true');

    const title = document.createElement('span');
    title.textContent = run.prompt.length > 34 ? `${run.prompt.slice(0, 34)}…` : run.prompt;

    const meta = document.createElement('em');
    meta.className = 'nav-meta';
    meta.textContent = statusLabel(run.status);

    button.append(icon, title, meta);
    button.addEventListener('click', () => openRun(run.runId));
    return button;
  }

  function homeRow(run) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'home-row';
    button.dataset.runId = run.runId;
    button.dataset.status = run.status;

    const icon = document.createElement('span');
    icon.className = 'icon';
    icon.dataset.icon = RUN_ICON;
    icon.setAttribute('aria-hidden', 'true');

    const body = document.createElement('span');
    body.className = 'home-row-body';
    const title = document.createElement('span');
    title.className = 'home-row-title';
    title.textContent = run.prompt;
    const meta = document.createElement('span');
    meta.className = 'home-row-meta';
    const duration = run.durationSeconds ? ` · ${Math.round(run.durationSeconds)}s` : '';
    meta.textContent = `${statusLabel(run.status)}${duration}`;
    body.append(title, meta);

    // 右侧写相对时间（标准库历史行的右簇），状态留在第二行。
    const time = document.createElement('span');
    time.className = 'home-row-time';
    time.textContent = relativeTime(run.createdAt);

    button.append(icon, body, time);
    button.addEventListener('click', () => openRun(run.runId));
    return button;
  }

  async function refreshRuns() {
    try {
      const { runs } = await api.listRuns();
      renderRunList(runs);
      return runs;
    } catch (error) {
      showToast(`读取运行列表失败：${error.message}`, 'error');
      return [];
    }
  }

  // -- run lifecycle ---------------------------------------------------------

  async function startRun() {
    const prompt = input.value.trim();
    if (!prompt) return;
    setSending(true);
    try {
      const { run } = await api.startRun(prompt);
      input.value = '';
      updatePromptCount();
      showView('chat');
      await openRun(run.runId);
      await refreshRuns();
    } catch (error) {
      showToast(startErrorMessage(error), 'error');
      return;
    } finally {
      setSending(false);
    }
  }

  function setSending(sending) {
    sendButton.disabled = sending || input.value.trim().length === 0;
    sendButton.setAttribute('aria-label', sending ? '提交中…' : '开始研究');
    sendButton.dataset.busy = sending ? 'true' : 'false';
  }

  async function openRun(runId) {
    closeCurrent();
    const { run } = await api.getRun(runId);
    const model = createRunModel(run);
    // A view belongs to one run: `dispose()` is permanent, so reusing an
    // instance across runs would silently freeze the timeline.
    const view = createRunView({
      root: runHost,
      renderMarkdown: (text) => renderMarkdown(text),
      onOpenReport: (openId) => openReport(openId),
    });
    current = { runId, model, subscription: null, view, title: run.prompt };
    showView('chat');
    question.textContent = run.prompt;
    view.render(model);
    updateBanner(model);
    syncSelection();
    syncPreviewToggle(model);

    current.subscription = createRunSubscription({
      runId,
      eventsUrl: api.eventsUrl,
      eventSourceFactory,
      onEvent: (event) => {
        if (disposed || current?.runId !== runId) return; // late frames from a replaced run
        current.model = applyEvent(current.model, event);
        current.view.render(current.model);
        updateBanner(current.model);
        syncPreviewToggle(current.model);
        // Closing on the terminal *frame* (not on the model's status) is what
        // makes history replay work: a finished run starts at status=finished,
        // so a status check would abort the subscription after the first
        // replayed frame and leave the timeline half-drawn.
        if (TERMINAL_EVENT_TYPES.has(event.type)) {
          current.subscription?.close();
          streamStatus.textContent = '运行结束';
          refreshRuns();
        }
      },
      onStatus: (status) => {
        if (disposed || current?.runId !== runId) return;
        streamStatus.textContent = RECONNECT_LABELS[status] ?? status;
      },
      onError: () => showToast('事件解析失败，已跳过该帧', 'warning'),
    });
  }

  function closeCurrent() {
    current?.subscription?.close();
    current?.view?.dispose();
    current = null;
    closeReport();
    syncPreviewToggle(null);
  }

  /** Move the `aria-current` marker to the run being viewed. */
  function syncSelection() {
    runList.querySelectorAll('[data-run-id]').forEach((node) => {
      if (node.dataset.runId === current?.runId) node.setAttribute('aria-current', 'page');
      else node.removeAttribute('aria-current');
    });
  }

  function updateBanner(model) {
    const terminal = TERMINAL_STATUSES.has(model.status);
    banner.hidden = terminal;
    stopButton.hidden = terminal;
    banner.dataset.status = model.status;
    if (terminal) {
      bannerTitle.textContent = '运行已结束';
      return;
    }
    bannerTitle.textContent = 'pi 正在本机运行';
  }

  async function stopRun() {
    if (!current) return;
    stopButton.disabled = true;
    try {
      await api.stopRun(current.runId);
      showToast('已请求停止，等待 pi 收尾', 'info');
    } catch (error) {
      showToast(`停止失败：${error.message}`, 'error');
    } finally {
      stopButton.disabled = false;
    }
  }

  // -- report ----------------------------------------------------------------

  function syncPreviewToggle(model) {
    const hasReport = Boolean(model?.hasReport);
    previewToggle.hidden = !hasReport;
    const open = shell.classList.contains('preview-open');
    const label = open ? '收起报告栏' : '展开报告栏';
    previewToggle.setAttribute('aria-label', label);
    previewToggle.dataset.tip = label;
    previewToggle.setAttribute('aria-expanded', String(open));
    previewToggle.firstElementChild.dataset.icon = open ? 'panel-collapse-right' : 'panel-expand-right';
  }

  async function openReport(runId) {
    try {
      const response = await fetch(api.reportUrl(runId));
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const text = await response.text();
      previewTitle.textContent = `报告 · ${runId}`;
      previewDownload.href = api.reportUrl(runId);
      previewDownload.setAttribute('download', `${runId}-report.md`);
      previewBody.innerHTML = renderMarkdown(text);
      previewDrawer.hidden = false;
      // The shell grid reserves the third column only in this state; without the
      // class the pane renders at 0px width and overflows the layout.
      shell.classList.add('preview-open');
      syncPreviewToggle(current?.model ?? null);
    } catch (error) {
      showToast(`打开报告失败：${error.message}`, 'error');
    }
  }

  function closeReport() {
    previewDrawer.hidden = true;
    shell.classList.remove('preview-open');
  }

  function toggleReport() {
    if (shell.classList.contains('preview-open')) {
      closeReport();
      syncPreviewToggle(current?.model ?? null);
      return;
    }
    if (current?.runId) openReport(current.runId);
  }

  // -- composer --------------------------------------------------------------

  /** The editor grows with its content between 68 and 240px, like the library's. */
  function autoGrow() {
    input.style.height = 'auto';
    input.style.height = `${Math.min(240, Math.max(68, input.scrollHeight))}px`;
  }

  function updatePromptCount() {
    const length = input.value.length;
    promptCount.hidden = length === 0;
    promptCount.textContent = `${length} / ${MAX_PROMPT}`;
    autoGrow();
    setSending(false);
  }

  // -- wiring ----------------------------------------------------------------

  /* Scroll-activity scrollbars (标准库 .sidebar-scroll-on-activity)：the bar is
   * invisible at rest and shows while the reader is actually moving the list. */
  function trackScrollActivity(node) {
    if (!node) return;
    let timer = null;
    node.addEventListener('scroll', () => {
      node.dataset.scrollActive = 'true';
      clearTimeout(timer);
      timer = setTimeout(() => {
        delete node.dataset.scrollActive;
      }, 800);
    }, { passive: true });
  }
  conversationScroll.addEventListener('scroll', () => {
    const distance = conversationScroll.scrollHeight - conversationScroll.scrollTop - conversationScroll.clientHeight;
    stick = distance < STICK_THRESHOLD;
  }, { passive: true });
  trackScrollActivity(el('nav-scroll'));
  trackScrollActivity(conversationScroll);
  trackScrollActivity(previewBody);

  input?.addEventListener('input', updatePromptCount);
  input?.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
      event.preventDefault();
      startRun();
    }
  });
  sendButton?.addEventListener('click', startRun);
  stopButton?.addEventListener('click', stopRun);
  paneHome?.addEventListener('click', () => {
    closeCurrent();
    showView('home');
    input.focus();
  });
  el('new-research')?.addEventListener('click', () => {
    closeCurrent();
    showView('home');
    input.focus();
  });
  previewClose?.addEventListener('click', () => {
    closeReport();
    syncPreviewToggle(current?.model ?? null);
  });
  previewToggle?.addEventListener('click', toggleReport);
  sidebarToggle?.addEventListener('click', () => {
    setSidebar(shell.dataset.sidebar !== 'collapsed');
  });

  return {
    async init() {
      if (!shell) throw new Error('workbench shell is missing from the document');
      // The shortcut hint must name the key the reader actually has: ⌘ on macOS,
      // Ctrl elsewhere (the handler accepts either).
      const modifier = el('prompt-modifier');
      if (modifier && !/Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent)) {
        modifier.textContent = 'Ctrl';
      }
      let collapsed = false;
      try {
        collapsed = localStorage.getItem(SIDEBAR_KEY) === 'collapsed';
      } catch {
        /* private mode / file: — fall back to expanded */
      }
      setSidebar(collapsed, { persist: false });
      await refreshHostStatus();
      await refreshRuns();
      showView('home');
      shell.hidden = false;
      composer.hidden = false;
      updatePromptCount();
    },
    startRun,
    openRun,
    openReport,
    refreshRuns,
    get model() {
      return current?.model ?? null;
    },
    get runId() {
      return current?.runId ?? null;
    },
    get runs() {
      return latestRuns;
    },
    dispose() {
      disposed = true;
      closeCurrent();
      clearTimeout(toastTimer);
    },
  };
}

/** Pane-header path keeps one line: a long prompt is cut, never wrapped. */
function shortTitle(prompt) {
  const text = String(prompt ?? '').trim().replace(/\s+/g, ' ');
  if (!text) return '研究会话';
  return text.length > 42 ? `${text.slice(0, 42)}…` : text;
}

function statusLabel(status) {
  switch (status) {
    case 'running':
      return '运行中';
    case 'finished':
      return '完成';
    case 'cancelled':
      return '已取消';
    case 'failed':
      return '失败';
    default:
      return status;
  }
}

/** Relative time for list rows, same register as the standard library's 历史行. */
function relativeTime(seconds) {
  const stamp = Number(seconds);
  if (!Number.isFinite(stamp) || stamp <= 0) return '';
  const delta = Date.now() / 1000 - stamp;
  if (delta < 60) return '刚刚';
  if (delta < 3600) return `${Math.floor(delta / 60)} 分钟前`;
  if (delta < 86400) return `${Math.floor(delta / 3600)} 小时前`;
  if (delta < 172800) return '昨天';
  if (delta < 2592000) return `${Math.floor(delta / 86400)} 天前`;
  const date = new Date(stamp * 1000);
  return `${date.getMonth() + 1} 月 ${date.getDate()} 日`;
}

function startErrorMessage(error) {
  if (error?.code === 'run_busy') return '已有 run 在运行，先停止或等它结束。';
  if (error?.code === 'not_configured') return '未配置模型凭据：请在 .env 填 OPENAI_API_KEY。';
  return `启动失败：${error.message}`;
}
