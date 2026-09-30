/**
 * Workbench shell: research records, composer, live run view, report slot,
 * progress & context panel.
 *
 * The shell owns navigation and layout and nothing else. Run semantics live in
 * `run-reducer.mjs` (pure event → state), `run-stream.mjs` (SSE subscription
 * folded through the reducer) and `run-view.mjs` (state → DOM); every word
 * the reader sees about a run comes from `present.mjs`. Nothing here names the
 * engine or the execution surface — which process ran the research is an
 * operator concern (see `/api/health`), not a reader concern.
 *
 * Layout contract (百鉴 规范 §2.2 / §2.3):
 *   · 8px gutter + left nav card + right work surface, 52px pane headers;
 *   · the main area has at most two columns — the conversation and one slot;
 *     the report takes the slot, 进度与上下文 is a floating panel governed by the
 *     same slot (opening one closes the other);
 *   · one draggable 1px line between the two columns (double-click resets),
 *     each column collapsible from its own outer edge (S-04).
 */

import { createApi, ApiError, setCsrfToken } from './api.mjs';
import { createAccountArea } from './account.mjs';
import { createNavGuard } from './nav-guard.mjs';
import { createRunState } from './run-reducer.mjs';
import { createRunFeed } from './run-stream.mjs';
import { renderRunLists } from './run-list-view.mjs';
import { emptyRunList, groupRunChains, mergeRunPage } from './run-list-state.mjs';
import { createRunView } from './run-view.mjs';
import { createProgressPanel } from './progress-panel.mjs';
import { installTooltips } from './tooltip.mjs';
import { renderMarkdown } from './markdown.mjs';
import { reportFileName, researchTitle } from './present.mjs';

const MAX_PROMPT = 32000;
/** The counter is noise while typing; it appears only near the limit. */
const COUNT_FROM = 30000;
const SIDEBAR_KEY = 'pi-glink-sidebar';
const SPLIT_KEY = 'pi-glink-report-split';
/** A dropped stream gets this long to come back before the reader is told. */
const RECONNECT_GRACE_MS = 4000;
const CONVERSATION_MIN = 560;
const REPORT_MIN = 360;
const SPLIT_STEP = 24;
const APP_NAME = '研究工作台';

const PLACEHOLDERS = {
  home: '描述研究对象、范围与交付要求…',
  idle: '继续追问或提出修改要求…',
  busy: '研究进行中，完成或停止后可以继续追问',
};

const TOAST_ICONS = { success: 'check-circle', info: 'check-circle', warning: 'alert-circle', error: 'alert-circle' };

export function createWorkspace({ api = createApi(), eventSourceFactory, auth = null } = {}) {
  const el = (id) => document.getElementById(id);

  const shell = el('app-shell');
  const surface = shell?.querySelector('.surface');
  const pane = el('conversation-pane');
  const homeView = el('home-view');
  const chatView = el('chat-view');
  const composer = el('composer');
  const input = el('prompt-input');
  const sendButton = el('send-button');
  const stopButton = el('stop-run');
  const promptCount = el('prompt-count');
  const runList = el('run-list');
  const homeRunList = el('home-run-list');
  const homeReportList = el('home-report-list');
  const sidebarFoot = el('sidebar-foot');
  const serviceAlert = el('service-alert');
  const serviceAlertText = el('service-alert-text');
  const conversationScroll = el('conversation-scroll');
  const runHost = el('run-view');
  const question = el('run-question');
  const earlierTurns = el('earlier-turns');
  const previewDrawer = el('preview-drawer');
  const previewBody = el('preview-body');
  const previewArticle = el('preview-article');
  const previewTitle = el('preview-title');
  const previewDownload = el('preview-download');
  const previewClose = el('preview-close');
  const previewToggle = el('pane-preview-toggle');
  const progressToggle = el('progress-toggle');
  const progressLayer = el('progress-layer');
  const headerNew = el('header-new');
  const conversationCollapse = el('conversation-collapse');
  const conversationExpand = el('conversation-expand');
  const splitHandle = el('split-handle');
  const paneTitle = el('pane-title');
  const composerDock = el('composer-dock');
  const composerDockBody = el('composer-dock-body');
  const composerHost = el('composer-host');
  const homeComposerSlot = el('home-composer-slot');
  const paneHome = el('pane-home');
  const sidebarToggle = el('sidebar-toggle');
  const toast = el('toast');
  const toastText = el('toast-text');
  const toastIcon = el('toast-icon');

  const rail = window.matchMedia('(width < 721px)');
  /** Two columns need 560 + 1 + 360; below that the report floats over the conversation. */
  const isOverlay = () => shell.classList.contains('slot-overlay');

  let current = null; // { runId, model, feed, view, prompt (root), history }
  /** The accumulated research history: `/api/runs` is a page, not the whole list. */
  let runListState = emptyRunList();
  let loadingMoreRuns = false;
  let latestRuns = [];
  let slot = null; // null | 'report' | 'progress'
  let conversationCollapsed = false;
  let modelName = '';
  let toastTimer = null;
  let reconnectTimer = null;
  let progressFrame = null;
  let disposed = false;
  /** The identity behind the workbench (null before login / after expiry). */
  let identity = null;
  /* Identity generation: login, re-authentication, expiry and logout each bump
   * it, so a response that started under an older identity is dropped instead
   * of rendering the previous account's data. The navigation token only covers
   * navigations, not a session change while a request is in flight. */
  let identityGeneration = 0;
  const identityStale = (generation) => disposed || generation !== identityGeneration;
  /** Which unauthenticated face is on screen (gateway/accounts/login). */
  let authFaceMode = null;
  /** True once the workbench shell has been revealed for a session. */
  let entered = false;
  /* A navigation may finish after a newer one started (or after dispose); the
   * token decides whether its response still owns the screen. */
  const nav = createNavGuard();
  /* Observers outlive a render pass; dispose must not leave them watching. */
  const disposers = [];

  const progress = createProgressPanel({
    root: el('progress-panel'),
    onOpenReport: (runId) => openReport(runId),
    onClose: () => setSlot(null, { returnFocus: progressToggle }),
  });
  const tooltips = installTooltips({ isRail: () => shell.dataset.sidebar === 'collapsed' || rail.matches });
  /* The account area owns the sidebar identity menu and the admin invitation
   * dialog; its logout button calls back into this module because only here can
   * the session be revoked and the screen cleared. */
  const account = createAccountArea({
    api,
    onLogout: () => logout(),
    onNotice: (message, level) => showToast(message, level),
  });

  /* Bottom anchoring (标准库用 use-stick-to-bottom 做同一件事)：the conversation
   * follows new frames only while the reader is already at the tail. */
  const STICK_THRESHOLD = 80;
  const ANCHOR_SETTLE_MS = 800;
  let stick = true;
  let settleUntil = 0;
  const anchored = el('conversation');
  const anchorFrame = () => {
    if (!stick || !anchored || disposed) return;
    settleUntil = Math.max(settleUntil, performance.now() + ANCHOR_SETTLE_MS);
    const step = () => {
      if (!stick || disposed) return;
      conversationScroll.scrollTop = conversationScroll.scrollHeight;
      if (performance.now() < settleUntil) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  };
  if (anchored && typeof MutationObserver === 'function') {
    const observer = new MutationObserver(anchorFrame);
    observer.observe(anchored, { childList: true, subtree: true, characterData: true });
    disposers.push(() => observer.disconnect());
    anchored.addEventListener('toggle', anchorFrame, true);
    if (typeof ResizeObserver === 'function') {
      const resize = new ResizeObserver(anchorFrame);
      resize.observe(anchored);
      disposers.push(() => resize.disconnect());
    }
  }

  /* The composer's real height drives the conversation's bottom padding and the
   * progress panel's lower edge (标准库 useComposerDockHeight): a textarea that
   * grows to 240px must never cover the last line of the answer. */
  if (typeof ResizeObserver === 'function' && composerDockBody) {
    const observer = new ResizeObserver(() => {
      const height = composerDockBody.offsetHeight;
      if (height) pane.style.setProperty('--composer-dock', `${height}px`);
    });
    observer.observe(composerDockBody);
    disposers.push(() => observer.disconnect());
  }

  // -- navigation ------------------------------------------------------------

  function showView(name) {
    homeView.hidden = name !== 'home';
    chatView.hidden = name !== 'chat';
    shell.dataset.view = name;
    /* The prompt input lives in the page flow on the landing view (title → input →
     * cards) and docks above the conversation otherwise. */
    if (name === 'home') {
      homeComposerSlot.append(composerHost);
      composerDock.hidden = true;
      setSlot(null);
      paneTitle.textContent = '新研究';
      document.title = APP_NAME;
      conversationScroll.scrollTop = 0;
      runList.querySelectorAll('[aria-current]').forEach((node) => node.removeAttribute('aria-current'));
    } else {
      composerDockBody.append(composerHost);
      composerDock.hidden = false;
      const title = researchTitle(current?.prompt);
      paneTitle.textContent = title;
      paneTitle.title = current?.prompt ?? '';
      document.title = `${title} · ${APP_NAME}`;
      stick = true;
      requestAnimationFrame(anchorFrame);
    }
    syncComposer();
    syncHeader();
  }

  function goHome() {
    closeCurrent();
    showView('home');
    input.focus();
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
      /* private mode — the state still applies for this session */
    }
  }

  // -- header (PH-05 right cluster, S-04 collapse keys) ------------------------

  function setToggle(button, { open, openLabel, closedLabel }) {
    const label = open ? openLabel : closedLabel;
    button.setAttribute('aria-expanded', String(open));
    button.setAttribute('aria-label', label);
    button.dataset.tip = label;
    button.classList.toggle('is-on', open);
  }

  function syncHeader() {
    const chat = shell.dataset.view === 'chat' && Boolean(current);
    headerNew.hidden = !chat;
    progressToggle.hidden = !chat;
    setToggle(progressToggle, { open: slot === 'progress', openLabel: '收起进度与上下文', closedLabel: '展开进度与上下文' });
    // The slot's own selector disappears while its content is in the slot; the
    // slot then carries its collapse key at the screen's right edge (S-04).
    previewToggle.hidden = !chat || !current?.model?.hasReport || slot === 'report';
    conversationCollapse.hidden = slot !== 'report' || conversationCollapsed || isOverlay();
    conversationExpand.hidden = !(slot === 'report' && conversationCollapsed);
  }

  function setSlot(next, { returnFocus = null } = {}) {
    if (next === slot) return;
    slot = next;
    const report = slot === 'report';
    shell.classList.toggle('preview-open', report);
    previewDrawer.hidden = !report;
    splitHandle.hidden = !report;
    if (!report) setConversationCollapsed(false);
    const open = slot === 'progress';
    pane.classList.toggle('progress-open', open);
    progressLayer.dataset.open = String(open);
    progressLayer.inert = !open;
    progressLayer.setAttribute('aria-hidden', String(!open));
    if (open) renderProgressNow();
    if (report) applySplit(readSplit());
    syncHeader();
    if (returnFocus && !returnFocus.hidden) returnFocus.focus();
  }

  function setConversationCollapsed(collapsed) {
    conversationCollapsed = collapsed;
    shell.classList.toggle('conversation-collapsed', collapsed);
    syncHeader();
  }

  // -- split line (S-03：可拖、双击复位、按栏型记住) ----------------------------

  function splitBounds() {
    const width = surface.getBoundingClientRect().width;
    return { min: CONVERSATION_MIN, max: width - 1 - REPORT_MIN, width };
  }

  function readSplit() {
    try {
      const value = Number(localStorage.getItem(SPLIT_KEY));
      return Number.isFinite(value) && value > 0 ? value : null;
    } catch {
      return null;
    }
  }

  function applySplit(px) {
    const { min, max, width } = splitBounds();
    if (px === null || max < min) {
      surface.style.removeProperty('--split-conversation');
    } else {
      surface.style.setProperty('--split-conversation', `${Math.round(Math.min(max, Math.max(min, px)))}px`);
    }
    const conversation = pane.getBoundingClientRect().width || width / 2;
    splitHandle.setAttribute('aria-valuemin', '0');
    splitHandle.setAttribute('aria-valuemax', '100');
    splitHandle.setAttribute('aria-valuenow', String(width ? Math.round((conversation / width) * 100) : 50));
  }

  function saveSplit(px) {
    try {
      if (px === null) localStorage.removeItem(SPLIT_KEY);
      else localStorage.setItem(SPLIT_KEY, String(Math.round(px)));
    } catch {
      /* private mode — the width still applies for this session */
    }
  }

  let dragging = false;
  splitHandle.addEventListener('pointerdown', (event) => {
    if (event.button !== 0) return;
    event.preventDefault();
    dragging = true;
    splitHandle.setPointerCapture(event.pointerId);
    splitHandle.dataset.dragging = 'true';
    shell.dataset.resizing = 'true';
  });
  splitHandle.addEventListener('pointermove', (event) => {
    if (!dragging) return;
    applySplit(event.clientX - surface.getBoundingClientRect().left);
  });
  const endDrag = () => {
    if (!dragging) return;
    dragging = false;
    delete splitHandle.dataset.dragging;
    delete shell.dataset.resizing;
    saveSplit(pane.getBoundingClientRect().width);
  };
  splitHandle.addEventListener('pointerup', endDrag);
  splitHandle.addEventListener('pointercancel', endDrag);
  splitHandle.addEventListener('dblclick', () => {
    saveSplit(null);
    applySplit(null);
  });
  splitHandle.addEventListener('keydown', (event) => {
    const width = pane.getBoundingClientRect().width;
    const { min, max } = splitBounds();
    let next = null;
    if (event.key === 'ArrowLeft') next = width - SPLIT_STEP;
    if (event.key === 'ArrowRight') next = width + SPLIT_STEP;
    if (event.key === 'Home') next = min;
    if (event.key === 'End') next = max;
    if (event.key === 'Enter') {
      saveSplit(null);
      applySplit(null);
      event.preventDefault();
      return;
    }
    if (next === null) return;
    event.preventDefault();
    applySplit(next);
    saveSplit(pane.getBoundingClientRect().width);
  });

  // -- toast -----------------------------------------------------------------

  function hideToast() {
    toast.hidden = true;
    clearTimeout(toastTimer);
  }

  function armToast() {
    clearTimeout(toastTimer);
    toastTimer = setTimeout(hideToast, 5000);
  }

  function showToast(message, level = 'info') {
    toastText.textContent = message;
    toast.dataset.level = level;
    toastIcon.dataset.icon = TOAST_ICONS[level] ?? 'check-circle';
    toast.hidden = false;
    armToast();
  }
  toast.addEventListener('pointerenter', () => clearTimeout(toastTimer));
  toast.addEventListener('pointerleave', armToast);
  el('toast-close').addEventListener('click', hideToast);

  // -- service readiness -------------------------------------------------------

  /* The reader hears about the service only when something stops them from
   * working; a healthy service is silent. Engine names and execution surfaces
   * are operator facts and never appear here. */
  async function refreshService() {
    const generation = identityGeneration;
    let message = '';
    try {
      const [health, config] = await Promise.all([api.health(), api.config()]);
      if (identityStale(generation)) return;
      modelName = config?.model ?? '';
      // ``config`` is the authenticated view: in multi-tenant mode it answers
      // for this caller's own tenant credential. The unauthenticated health
      // probe only knows the host-level key, so using it here would tell a
      // tenant with a configured key that no model is configured.
      if (!health?.pi?.available) message = '研究引擎暂时不可用，暂时无法发起研究。请联系管理员。';
      else if (!config?.configured?.model) message = '模型服务还没有配置，暂时无法发起研究。请联系管理员完成配置。';
    } catch (error) {
      if (identityStale(generation)) return;
      if (handleSessionFailure(error)) return;
      message = '服务暂时连不上。请确认服务在运行，然后刷新页面。';
    }
    if (identityStale(generation)) return;
    serviceAlertText.textContent = message;
    serviceAlert.dataset.tip = message;
    sidebarFoot.hidden = !message;
  }

  // -- research records ------------------------------------------------------

  /** One request's worth of history; the sidebar's «显示更多» asks for the next. */
  const RUN_PAGE_LIMIT = 50;
  /** How many missing ancestors one refresh may resolve (a page cuts chains). */
  const MAX_CHAIN_ANCESTORS = 25;

  function renderRunList(state = runListState) {
    runListState = state;
    latestRuns = state.runs;
    renderRunLists({ runList, homeRunList, homeReportList }, state.runs, {
      currentRunId: current?.runId ?? null,
      onOpen: (runId, options) => openRun(runId, options),
      hasMore: state.hasMore,
      loadingMore: loadingMoreRuns,
      onLoadMore: () => {
        loadMoreRuns();
      },
    });
  }

  /**
   * Resolve the chain roots a page boundary left out.
   *
   * The list is newest-first, so a page can cut a follow-up chain before its
   * first turn. Fetching the missing parents (bounded) lets the row keep the
   * root's title instead of surfacing an intermediate turn as a research of its
   * own; a parent that is gone (404, another tenant) simply ends the walk and
   * the newest loaded turn stays visible.
   */
  async function completeFetchedChains(generation) {
    const known = new Set(runListState.runs.map((run) => run.runId));
    const asked = new Set();
    const queue = groupRunChains(runListState.runs).unresolvedParents.filter((id) => !known.has(id));
    let hops = 0;
    while (queue.length && hops < MAX_CHAIN_ANCESTORS) {
      const parentId = queue.shift();
      if (asked.has(parentId)) continue;
      asked.add(parentId);
      hops += 1;
      let run = null;
      try {
        ({ run } = await api.getRun(parentId));
      } catch {
        run = null; // unknown, other tenant or unavailable: keep the fallback title
      }
      if (identityStale(generation)) return;
      if (!run?.runId) continue;
      renderRunList(mergeRunPage(runListState, { runs: [run] }));
      known.add(run.runId);
      for (const id of groupRunChains(runListState.runs).unresolvedParents) {
        if (!known.has(id) && !asked.has(id) && !queue.includes(id)) queue.push(id);
      }
    }
  }

  /** Re-read the newest page, replacing whatever this session had loaded. */
  async function refreshRuns() {
    const generation = identityGeneration;
    try {
      const page = await api.listRuns({ limit: RUN_PAGE_LIMIT });
      if (identityStale(generation)) return [];
      renderRunList(mergeRunPage(emptyRunList(), page));
      await completeFetchedChains(generation);
      return runListState.runs;
    } catch (error) {
      if (identityStale(generation)) return [];
      if (handleSessionFailure(error)) return [];
      showToast('研究记录暂时读取失败，请稍后刷新。', 'error');
      return [];
    }
  }

  /** Append the next page the host named; the already-loaded history is kept. */
  async function loadMoreRuns() {
    if (loadingMoreRuns || !runListState.hasMore) return;
    const generation = identityGeneration;
    loadingMoreRuns = true;
    renderRunList(runListState);
    let failed = false;
    try {
      const page = await api.listRuns({ limit: RUN_PAGE_LIMIT, cursor: runListState.cursor });
      if (identityStale(generation)) return;
      runListState = mergeRunPage(runListState, page);
    } catch (error) {
      if (identityStale(generation)) return;
      if (handleSessionFailure(error)) return;
      failed = true;
    } finally {
      loadingMoreRuns = false;
    }
    renderRunList(runListState);
    if (failed) {
      showToast('研究记录暂时读取失败，请稍后刷新。', 'error');
      return;
    }
    await completeFetchedChains(generation);
  }

  // -- run lifecycle ---------------------------------------------------------

  async function startRun(promptOverride) {
    const prompt = String(promptOverride ?? input.value).trim();
    if (!prompt) return;
    if (current?.model?.status === 'running') {
      showToast('这项研究还在进行，请等它完成或先停止。', 'warning');
      return;
    }
    const generation = identityGeneration;
    setSending(true);
    try {
      const { run } = await api.startRun(prompt, promptOverride === undefined ? current?.runId : undefined);
      if (identityStale(generation)) return;
      if (promptOverride === undefined) input.value = '';
      updatePromptCount();
      await openRun(run.runId);
      await refreshRuns();
    } catch (error) {
      if (identityStale(generation)) return;
      if (handleSessionFailure(error)) return;
      const refusal = error?.code === 'run_busy' || error?.code === 'resources_unreleased';
      showToast(startErrorMessage(error), refusal ? 'warning' : 'error');
    } finally {
      setSending(false);
    }
  }

  function setSending(sending) {
    sendButton.disabled = sending || input.value.trim().length === 0;
    const label = sending ? '正在提交' : current ? '发送追问' : '开始研究';
    sendButton.setAttribute('aria-label', label);
    sendButton.dataset.tip = label;
  }

  async function openRun(runId, { withReport = false } = {}) {
    closeCurrent();
    // This navigation owns the screen until a newer one starts or the workspace
    // is disposed; a slow response must not overwrite what came after it.
    const token = nav.begin();
    let run;
    try {
      ({ run } = await api.getRun(runId));
    } catch (error) {
      if (handleSessionFailure(error)) return;
      if (disposed || !nav.isCurrent(token)) return; // stale failure: stay quiet
      showToast('这项研究暂时打不开，请稍后重试。', 'error');
      return;
    }
    if (disposed || !nav.isCurrent(token)) return;
    const ancestors = [];
    const seen = new Set([runId]);
    let parentId = run.parentRunId;
    while (parentId && !seen.has(parentId) && ancestors.length < 100) {
      seen.add(parentId);
      try {
        const { run: ancestor } = await api.getRun(parentId);
        if (disposed || !nav.isCurrent(token)) return;
        ancestors.unshift(ancestor);
        parentId = ancestor.parentRunId;
      } catch (error) {
        if (handleSessionFailure(error)) return;
        if (disposed || !nav.isCurrent(token)) return;
        showToast('之前的对话暂时无法加载，请稍后刷新。', 'warning');
        break;
      }
    }
    if (disposed || !nav.isCurrent(token)) return;
    const model = createRunState(run);
    // A view belongs to one run: `dispose()` is permanent.
    const view = createRunView({
      root: runHost,
      renderMarkdown: (text) => renderMarkdown(text),
      onOpenReport: (openId) => openReport(openId),
      onRerun: (prompt) => startRun(prompt),
      onRetry: (retryId) => openRun(retryId ?? runId),
      onCopy: (text) => copyText(text),
      reportHref: (id) => api.reportUrl(id),
      reportDownloadName: (value) => reportFileName(value.prompt),
      childFileHref: (id, n, name) => api.childFileUrl(id, n, name),
      deliverableHref: (id, index) => api.deliverableUrl(id, index),
    });
    current = { runId, model, feed: null, view,
      prompt: ancestors[0]?.prompt ?? run.prompt, history: [] };
    earlierTurns.replaceChildren();
    const owner = current;
    /* A 20-turn chain must not open 21 `EventSource`s: the host bounds
     * concurrent streams and would refuse the live run's own stream once the
     * ceiling is hit, so the history would never appear. Ancestors therefore
     * replay one at a time — each from the durable log, closing on its terminal
     * frame before the next connects. */
    const historyQueue = [];
    let historyCursor = 0;
    function openNextHistory() {
      if (disposed || current !== owner) return;
      historyQueue[historyCursor]?.feed.open();
    }
    function advanceHistory() {
      if (disposed || current !== owner) return;
      historyCursor += 1;
      openNextHistory();
    }
    for (const ancestor of ancestors) {
      const exchange = document.createElement('section');
      exchange.className = 'pi-exchange';
      const pastQuestion = document.createElement('article');
      pastQuestion.className = 'pi-question message user-message';
      pastQuestion.textContent = ancestor.prompt;
      const pastRoot = document.createElement('div');
      pastRoot.className = 'pi-run';
      exchange.append(pastQuestion, pastRoot);
      earlierTurns.append(exchange);
      const past = { runId: ancestor.runId, model: createRunState(ancestor), feed: null,
        view: createRunView({ root: pastRoot, renderMarkdown, onOpenReport: openReport,
          onCopy: copyText, reportHref: api.reportUrl,
          deliverableHref: api.deliverableUrl,
          reportDownloadName: (value) => reportFileName(value.prompt) }) };
      current.history.push(past);
      past.view.render(past.model);
      past.feed = createRunFeed({ run: ancestor, initialState: past.model, eventsUrl: api.eventsUrl,
        eventSourceFactory, lazy: true,
        isActive: () => !disposed && current === owner,
        onState: (state) => {
          past.model = state;
          past.view.render(state);
        },
        onTerminal: () => advanceHistory(),
        onStatus: (status) => {
          if (status === 'failed') {
            past.feed?.close();
            advanceHistory();
          }
        } });
      historyQueue.push(past);
    }
    question.textContent = run.prompt;
    showView('chat');
    view.render(model);
    syncSelection();
    afterModelChange();
    if (withReport) {
      const reportRunId = withReport === true ? runId : withReport;
      if (reportRunId === runId ? model.hasReport : ancestors.some((past) => past.runId === reportRunId && past.hasReport)) {
        openReport(reportRunId);
      }
    }

    // History first, then the live run: the ancestor connection is the only one
    // open beside the current run, and the current run connects synchronously
    // right after it.
    openNextHistory();
    current.feed = createRunFeed({
      run,
      initialState: model,
      eventsUrl: api.eventsUrl,
      eventSourceFactory,
      // Late frames from a replaced run are dropped.
      isActive: () => !disposed && current === owner,
      onState: (state) => {
        current.model = state;
        current.view.render(state);
        afterModelChange();
      },
      // The feed closes the stream on the terminal *frame* (not on the state's
      // status): that is what makes history replay work, since a finished run
      // starts at status=finished.
      onTerminal: () => {
        setConnection(null);
        refreshRuns();
      },
      onStatus: (status) => onStreamStatus(status),
      onError: () => showToast('有一条过程记录无法显示，已跳过。', 'warning'),
    });
  }

  function onStreamStatus(status) {
    clearTimeout(reconnectTimer);
    if (status === 'live') {
      setConnection(null);
    } else if (status === 'reconnecting') {
      reconnectTimer = setTimeout(() => {
        if (current?.feed?.status === 'reconnecting') setConnection('reconnecting');
      }, RECONNECT_GRACE_MS);
    } else if (status === 'failed') {
      current?.feed?.close();
      setConnection('unavailable');
      // In accounts mode a stream that dies can mean the server ended it because
      // the session was revoked, disabled or expired; verify before trusting the
      // screen with the old identity's data.
      if (identity?.mode === 'accounts') verifySession();
    }
  }

  function setConnection(state) {
    current?.view?.setConnection(state);
  }

  let lastStatus = null;
  let lastHasReport = null;
  function afterModelChange() {
    const model = current?.model;
    if (!model) return;
    if (model.status !== lastStatus || model.hasReport !== lastHasReport) {
      lastStatus = model.status;
      lastHasReport = model.hasReport;
      syncComposer();
      syncHeader();
    }
    renderProgressSoon();
  }

  function closeCurrent() {
    nav.invalidate();
    clearTimeout(reconnectTimer);
    current?.feed?.close();
    current?.view?.dispose();
    for (const past of current?.history ?? []) {
      past.feed?.close();
      past.view.dispose();
    }
    earlierTurns?.replaceChildren();
    current = null;
    lastStatus = null;
    lastHasReport = null;
    setSlot(null);
    syncComposer();
  }

  /** Move the `aria-current` marker to the run being viewed. */
  function syncSelection() {
    runList.querySelectorAll('[data-run-id]').forEach((node) => {
      if (node.dataset.runId === current?.runId) node.setAttribute('aria-current', 'page');
      else node.removeAttribute('aria-current');
    });
  }

  async function stopRun() {
    if (!current) return;
    const { runId } = current;
    const generation = identityGeneration;
    stopButton.disabled = true;
    try {
      await api.stopRun(runId);
      if (identityStale(generation)) return;
      if (current?.runId === runId) showToast('正在停止，已完成的内容会保留。', 'info');
    } catch (error) {
      if (identityStale(generation)) return;
      if (handleSessionFailure(error)) return;
      // The host decides a stop at the run's commit point: past it, the run's
      // committed outcome stands and arrives on the stream as its terminal frame.
      if (current?.runId === runId) {
        showToast(stopErrorMessage(error), error?.code === 'run_not_cancellable' ? 'info' : 'error');
      }
    } finally {
      stopButton.disabled = false;
    }
  }

  async function copyText(text) {
    try {
      if (!navigator.clipboard) throw new Error('clipboard unavailable');
      await navigator.clipboard.writeText(text);
      showToast('已复制回答', 'success');
    } catch {
      showToast('复制没有成功，请手动选择文字复制。', 'error');
    }
  }

  // -- progress panel --------------------------------------------------------

  function renderProgressNow() {
    if (progressFrame !== null) cancelAnimationFrame(progressFrame);
    progressFrame = null;
    if (current?.model) progress.render(current.model, { modelName });
  }

  function renderProgressSoon() {
    if (slot !== 'progress' || progressFrame !== null) return;
    progressFrame = requestAnimationFrame(() => {
      progressFrame = null;
      if (slot === 'progress' && current?.model) progress.render(current.model, { modelName });
    });
  }

  // -- report ----------------------------------------------------------------

  async function openReport(runId) {
    const token = nav.token;
    const generation = identityGeneration;
    try {
      const response = await fetch(api.reportUrl(runId));
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const text = await response.text();
      if (identityStale(generation) || !nav.isCurrent(token) ||
        (current?.runId !== runId && !current?.history?.some((past) => past.runId === runId))) return;
      previewTitle.textContent = 'report.md';
      previewTitle.title = 'report.md';
      previewDownload.href = api.reportUrl(runId);
      const source = current.history.find((past) => past.runId === runId);
      previewDownload.setAttribute('download', reportFileName(source?.model.prompt ?? current.model.prompt));
      previewArticle.innerHTML = renderMarkdown(text);
      previewBody.scrollTop = 0;
      setSlot('report');
    } catch (error) {
      if (identityStale(generation)) return;
      if (handleSessionFailure(error)) return;
      showToast('报告暂时打不开，请稍后重试。', 'error');
    }
  }

  // -- composer --------------------------------------------------------------

  /** The editor grows with its content between 68 and 240px, like the library's. */
  function autoGrow() {
    input.style.height = 'auto';
    input.style.height = `${Math.min(240, Math.max(68, input.scrollHeight))}px`;
  }

  function updatePromptCount() {
    const length = input.value.length;
    promptCount.hidden = length < COUNT_FROM;
    promptCount.textContent = `${length} / ${MAX_PROMPT}`;
    promptCount.dataset.level = length >= MAX_PROMPT ? 'limit' : '';
    autoGrow();
    setSending(false);
  }

  function syncComposer() {
    const busy = current?.model?.status === 'running';
    composer.dataset.busy = String(busy);
    sendButton.hidden = busy;
    stopButton.hidden = !busy;
    input.readOnly = busy;
    input.setAttribute('aria-readonly', String(busy));
    const view = shell.dataset.view === 'home' ? 'home' : busy ? 'busy' : 'idle';
    input.placeholder = PLACEHOLDERS[view];
  }

  // -- wiring ----------------------------------------------------------------

  /* Scroll-activity scrollbars (标准库 .sidebar-scroll-on-activity). */
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
  trackScrollActivity(el('progress-panel'));

  input.addEventListener('input', updatePromptCount);
  input.addEventListener('keydown', (event) => {
    if (event.key !== 'Enter') return;
    // 输入法组字期间的回车是在选候选字，不是提交（标准库 PromptInputTextarea 同一条保护）。
    if (event.isComposing || event.keyCode === 229) return;
    if (event.shiftKey) return; // Shift+Enter 换行
    event.preventDefault();
    if (input.readOnly) return;
    startRun();
  });
  sendButton.addEventListener('click', () => startRun());
  stopButton.addEventListener('click', stopRun);
  paneHome.addEventListener('click', goHome);
  el('new-research').addEventListener('click', goHome);
  headerNew.addEventListener('click', goHome);
  el('brand-home').addEventListener('click', (event) => {
    event.preventDefault();
    goHome();
  });
  previewClose.addEventListener('click', () => setSlot(null, { returnFocus: previewToggle }));
  previewToggle.addEventListener('click', () => {
    if (current?.runId) openReport(current.runId);
  });
  progressToggle.addEventListener('click', () => setSlot(slot === 'progress' ? null : 'progress'));
  conversationCollapse.addEventListener('click', () => {
    setConversationCollapsed(true);
    conversationExpand.focus();
  });
  conversationExpand.addEventListener('click', () => {
    setConversationCollapsed(false);
    conversationCollapse.focus();
  });
  sidebarToggle.addEventListener('click', () => {
    setSidebar(shell.dataset.sidebar !== 'collapsed');
  });
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape' || event.defaultPrevented) return;
    if (slot === 'progress') setSlot(null, { returnFocus: progressToggle });
    else if (slot === 'report' && isOverlay()) setSlot(null, { returnFocus: previewToggle });
  });
  /* The slot mode follows the main surface's real width (the sidebar's three
   * widths change it as much as the viewport does). */
  const syncSlotMode = () => {
    if (disposed) return;
    const overlay = surface.getBoundingClientRect().width < CONVERSATION_MIN + 1 + REPORT_MIN;
    if (overlay !== isOverlay()) {
      shell.classList.toggle('slot-overlay', overlay);
      if (overlay && conversationCollapsed) setConversationCollapsed(false);
    }
    if (slot === 'report') applySplit(readSplit());
    syncHeader();
  };
  if (typeof ResizeObserver === 'function' && surface) {
    const observer = new ResizeObserver(syncSlotMode);
    observer.observe(surface);
    disposers.push(() => observer.disconnect());
  } else window.addEventListener('resize', syncSlotMode);

  // -- session lifecycle -----------------------------------------------------

  /** Handle a 401 from any API call: the session is over, clear the screen. */
  function handleSessionFailure(error) {
    if (!(error instanceof ApiError) || error.status !== 401) return false;
    if (identity || entered) sessionExpired('登录状态已过期，请重新登录。');
    else showAuthFace(error.payload?.mode);
    return true;
  }

  /** Show the right unauthenticated face for the identity mode the host uses. */
  function showAuthFace(mode, { notice = '', invite = '' } = {}) {
    if (!auth) return;
    authFaceMode = mode;
    if (mode === 'gateway') {
      auth.show({ mode: 'gateway', notice });
      return;
    }
    if (mode === 'accounts') {
      const wantsRegister =
        typeof location !== 'undefined' && String(location.pathname || '').startsWith('/register');
      auth.show({
        mode: wantsRegister ? 'register' : 'login',
        invite: invite || inviteFromUrl(),
        notice,
      });
      return;
    }
    auth.show({ mode: 'login', notice });
  }

  function inviteFromUrl() {
    try {
      // The invitation lives in the URL *fragment*: browsers never send it to
      // the server and strip it from Referer, and `auth.mjs` clears it as soon
      // as the client has it.
      if (typeof location === 'undefined' || !location.hash) return '';
      return new URLSearchParams(String(location.hash).replace(/^#/, '')).get('invite') || '';
    } catch {
      return '';
    }
  }

  /** Ask the server who we are before revealing anything about the workbench. */
  async function refreshSession() {
    let me;
    try {
      me = await api.me();
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        showAuthFace(error.payload?.mode);
      } else if (auth) {
        auth.show({ mode: 'unavailable', notice: '服务暂时连不上，无法确认登录状态。请稍后重试。' });
      }
      return;
    }
    await enterSession(me);
  }

  /** Reveal the workbench for an authenticated (or local) identity. */
  async function enterSession(me) {
    if (disposed) return;
    identityGeneration += 1;
    const generation = identityGeneration;
    identity = me ?? null;
    setCsrfToken(me?.csrfToken ?? null);
    account.setIdentity(identity);
    if (auth) auth.hide();
    setSkipTarget(false);
    if (
      typeof history !== 'undefined' &&
      typeof history.replaceState === 'function' &&
      typeof location !== 'undefined' &&
      location.pathname !== '/'
    ) {
      history.replaceState({}, '', '/');
    }
    await refreshService();
    await refreshRuns();
    // A session change while those were in flight must not reveal this shell.
    if (identityStale(generation)) return;
    showView('home');
    shell.hidden = false;
    entered = true;
    updatePromptCount();
  }

  /** Drop the identity and everything derived from it, then show login again. */
  function sessionExpired(notice) {
    identityGeneration += 1;
    closeCurrent();
    loadingMoreRuns = false;
    renderRunList(emptyRunList());
    setSlot(null);
    earlierTurns.replaceChildren();
    previewArticle.replaceChildren();
    account.setIdentity(null);
    setCsrfToken(null);
    identity = null;
    entered = false;
    authFaceMode = 'accounts';
    shell.hidden = true;
    setSkipTarget(true);
    if (typeof history !== 'undefined' && typeof history.replaceState === 'function') {
      history.replaceState({}, '', '/login');
    }
    if (auth) {
      auth.show({
        mode: 'login',
        notice: notice ?? '登录状态已过期，请重新登录。',
      });
    }
  }

  /** Re-check the session (visibility change, dead SSE stream, CSRF rejection). */
  async function verifySession() {
    if (!identity || disposed) return false;
    const generation = identityGeneration;
    try {
      const me = await api.me();
      if (identityStale(generation)) return false;
      setCsrfToken(me?.csrfToken ?? null);
      identity = me;
      account.setIdentity(me);
      return Boolean(me?.csrfToken);
    } catch (error) {
      if (!disposed && error instanceof ApiError && error.status === 401) {
        sessionExpired('登录状态已过期，请重新登录。');
      }
      return false;
    }
  }

  /** Revoke the server session, then clear the screen. Never hides UI only. */
  async function logout() {
    if (!identity) return;
    try {
      await api.logout();
    } catch (error) {
      if (!(error instanceof ApiError && error.status === 401)) {
        // A refused or unreachable logout must not pretend the session ended.
        showToast(
          error instanceof ApiError && error.status === 403
            ? '退出请求被拒绝，请刷新页面后重试。'
            : '退出没有成功，请稍后重试。',
          'error'
        );
        return;
      }
    }
    sessionExpired('已退出登录。');
  }

  function setSkipTarget(toAuth) {
    const skip = el('skip-link');
    if (skip && typeof skip.setAttribute === 'function') {
      skip.setAttribute('href', toAuth ? '#auth-view' : '#main-content');
    }
  }

  if (typeof window !== 'undefined' && window.addEventListener) {
    // A link like /register#invite=… pasted while the register face is already
    // open is a same-document navigation: no reload, only a hashchange. Without
    // this the new code would sit unused in the address bar (and stay there);
    // with it the fragment is read and cleared like any other arrival.
    window.addEventListener('hashchange', () => {
      if (disposed || entered || authFaceMode !== 'accounts') return;
      const invite = inviteFromUrl();
      if (!invite) return;
      showAuthFace('accounts', { invite });
    });
  }

  if (typeof document !== 'undefined' && document.addEventListener) {
    document.addEventListener('visibilitychange', () => {
      if (disposed || !entered || identity?.mode !== 'accounts') return;
      if (document.hidden === true) return;
      verifySession();
    });
  }

  // Keep 401/session handling in this workspace; the shared HTTP client only
  // retries the original POST if verification actually refreshed this session.
  api.setCsrfRecovery?.(() => identity?.mode === 'accounts' && verifySession());

  return {
    async init() {
      if (!shell) throw new Error('workbench shell is missing from the document');
      // Hide the shell before the identity probe, whatever the markup said: the
      // previous identity's runs must never flash while /api/me is in flight.
      shell.hidden = true;
      input.setAttribute('data-focus-ring-managed', '');
      let collapsed = false;
      try {
        collapsed = localStorage.getItem(SIDEBAR_KEY) === 'collapsed';
      } catch {
        /* private mode — fall back to expanded */
      }
      setSidebar(collapsed, { persist: false });
      await refreshSession();
    },
    enterSession,
    sessionExpired,
    logout,
    startRun,
    stopRun,
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
    get identity() {
      return identity;
    },
    dispose() {
      disposed = true;
      nav.invalidate();
      closeCurrent();
      clearTimeout(toastTimer);
      clearTimeout(reconnectTimer);
      if (progressFrame !== null) cancelAnimationFrame(progressFrame);
      progressFrame = null;
      for (const dispose of disposers.splice(0)) {
        try {
          dispose();
        } catch {
          /* an observer that cannot be detached must not break disposal */
        }
      }
      tooltips.dispose();
      account.dispose?.();
    },
  };
}

function startErrorMessage(error) {
  if (error?.code === 'run_busy') return '已有一项研究正在进行，请等它完成或先停止。';
  if (error?.code === 'resources_unreleased') {
    return '上一项研究的执行资源还没有确认释放，暂时不能开始新研究。请联系管理员核对。';
  }
  if (error?.code === 'not_configured') return '模型服务还没有配置，暂时无法发起研究。请联系管理员完成配置。';
  if (error?.code === 'session_unavailable') return '此前的会话记录不可续接；请从“新建研究”重新开始。';
  if (error?.code === 'stale_run') return '这项研究已有更新的追问，请刷新后在最新一轮继续。';
  if (error?.code === 'invalid_request') return '这个问题没能提交，请检查内容后重试。';
  return '研究没能开始，请稍后重试。';
}

function stopErrorMessage(error) {
  if (error?.code === 'run_not_cancellable') {
    return error.payload?.reason === 'finalizing'
      ? '这项研究正在收尾，已无法停止。'
      : '这项研究已经结束，无需停止。';
  }
  if (error?.code === 'unknown_run') return '找不到这项研究，请刷新后重试。';
  return '停止没有成功，请重试。';
}
