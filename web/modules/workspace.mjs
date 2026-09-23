/**
 * Workbench shell: research records, composer, live run view, report slot,
 * progress & context panel.
 *
 * The shell owns navigation and layout and nothing else. Run semantics live in
 * `run-stream.mjs` (event → model) and `run-view.mjs` (model → DOM); every word
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

import { createApi } from './api.mjs';
import { applyEvent, createRunModel, createRunSubscription, TERMINAL_EVENT_TYPES } from './run-stream.mjs';
import { createRunView } from './run-view.mjs';
import { createProgressPanel } from './progress-panel.mjs';
import { installTooltips } from './tooltip.mjs';
import { renderMarkdown } from './markdown.mjs';
import { formatDuration, relativeTime, reportFileName, researchTitle, statusLabel } from './present.mjs';

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
  idle: '发起一项新的研究…',
  busy: '研究进行中，完成或停止后可以发起新的研究',
};

/* Only the two states that need the reader get a word in the sidebar. */
const NAV_META = { running: '进行中', failed: '未完成' };

const TOAST_ICONS = { success: 'check-circle', info: 'check-circle', warning: 'alert-circle', error: 'alert-circle' };

export function createWorkspace({ api = createApi(), eventSourceFactory } = {}) {
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

  let current = null; // { runId, model, subscription, view, prompt, events }
  let latestRuns = [];
  let slot = null; // null | 'report' | 'progress'
  let conversationCollapsed = false;
  let modelName = '';
  let toastTimer = null;
  let reconnectTimer = null;
  let progressFrame = null;
  let disposed = false;

  const progress = createProgressPanel({ root: el('progress-panel'), onOpenReport: (runId) => openReport(runId) });
  installTooltips({ isRail: () => shell.dataset.sidebar === 'collapsed' || rail.matches });

  /* Bottom anchoring (标准库用 use-stick-to-bottom 做同一件事)：the conversation
   * follows new frames only while the reader is already at the tail. */
  const STICK_THRESHOLD = 80;
  const ANCHOR_SETTLE_MS = 800;
  let stick = true;
  let settleUntil = 0;
  const anchored = el('conversation');
  const anchorFrame = () => {
    if (!stick || !anchored) return;
    settleUntil = Math.max(settleUntil, performance.now() + ANCHOR_SETTLE_MS);
    const step = () => {
      if (!stick) return;
      conversationScroll.scrollTop = conversationScroll.scrollHeight;
      if (performance.now() < settleUntil) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  };
  if (anchored && typeof MutationObserver === 'function') {
    new MutationObserver(anchorFrame).observe(anchored, { childList: true, subtree: true, characterData: true });
    anchored.addEventListener('toggle', anchorFrame, true);
    if (typeof ResizeObserver === 'function') new ResizeObserver(anchorFrame).observe(anchored);
  }

  /* The composer's real height drives the conversation's bottom padding and the
   * progress panel's lower edge (标准库 useComposerDockHeight): a textarea that
   * grows to 240px must never cover the last line of the answer. */
  if (typeof ResizeObserver === 'function' && composerDockBody) {
    new ResizeObserver(() => {
      const height = composerDockBody.offsetHeight;
      if (height) pane.style.setProperty('--composer-dock', `${height}px`);
    }).observe(composerDockBody);
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
    let message = '';
    try {
      const [health, config] = await Promise.all([api.health(), api.config()]);
      modelName = config?.model ?? '';
      if (!health?.pi?.available) message = '研究引擎暂时不可用，暂时无法发起研究。请联系管理员。';
      else if (!health?.configured?.model) message = '模型服务还没有配置，暂时无法发起研究。请联系管理员完成配置。';
    } catch {
      message = '服务暂时连不上。请确认服务在运行，然后刷新页面。';
    }
    serviceAlertText.textContent = message;
    serviceAlert.dataset.tip = message;
    sidebarFoot.hidden = !message;
  }

  // -- research records ------------------------------------------------------

  function emptyState({ title, description, size = 'sm' }) {
    const wrap = document.createElement('div');
    wrap.className = 'empty-state';
    wrap.dataset.size = size;
    wrap.dataset.variant = 'no-data';
    wrap.setAttribute('role', 'status');
    const image = document.createElement('img');
    image.src = '/empty-state/no-data.svg';
    image.alt = '';
    const heading = document.createElement('p');
    heading.className = 'empty-state-title';
    heading.textContent = title;
    wrap.append(image, heading);
    if (description) {
      const text = document.createElement('p');
      text.className = 'empty-state-desc';
      text.textContent = description;
      wrap.append(text);
    }
    return wrap;
  }

  function renderRunList(runs) {
    latestRuns = runs;
    runList.replaceChildren();
    homeRunList.replaceChildren();
    homeReportList.replaceChildren();
    if (!runs.length) {
      runList.append(emptyState({ title: '还没有研究记录' }));
      homeRunList.append(emptyState({ title: '还没有研究', description: '在上方描述问题，开始第一项研究' }));
      homeReportList.append(emptyState({ title: '还没有报告', description: '研究完成后生成的报告会出现在这里' }));
      return;
    }
    for (const run of runs) {
      runList.append(navRow(run));
      homeRunList.append(homeRow(run));
    }
    const reports = runs.filter((run) => run.hasReport);
    if (!reports.length) {
      homeReportList.append(emptyState({ title: '还没有报告', description: '研究完成后生成的报告会出现在这里' }));
    }
    for (const run of reports) homeReportList.append(reportRow(run));
  }

  function iconNode(name) {
    const node = document.createElement('span');
    node.className = 'icon';
    node.dataset.icon = name;
    node.setAttribute('aria-hidden', 'true');
    return node;
  }

  function span(className, text) {
    const node = document.createElement('span');
    node.className = className;
    node.textContent = text;
    return node;
  }

  function navRow(run) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'nav-item';
    button.dataset.runId = run.runId;
    button.dataset.status = run.status;
    button.dataset.tip = researchTitle(run.prompt, 40);
    button.dataset.tipSide = 'right';
    button.dataset.tipWhen = 'collapsed';
    if (current?.runId === run.runId) button.setAttribute('aria-current', 'page');
    button.append(iconNode('chat'), span('nav-title', researchTitle(run.prompt)));
    if (NAV_META[run.status]) button.append(span('nav-meta', NAV_META[run.status]));
    button.addEventListener('click', () => openRun(run.runId));
    return button;
  }

  function homeRow(run) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'home-row';
    button.dataset.runId = run.runId;
    button.dataset.status = run.status;
    const body = span('home-row-body', '');
    const duration = formatDuration(run.durationSeconds);
    body.append(
      span('home-row-title', researchTitle(run.prompt, 40)),
      span('home-row-meta', duration ? `${statusLabel(run.status)} · ${duration}` : statusLabel(run.status)),
    );
    button.append(iconNode('history'), body, span('home-row-time', relativeTime(run.createdAt)));
    button.addEventListener('click', () => openRun(run.runId));
    return button;
  }

  function reportRow(run) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'home-row';
    button.dataset.reportRunId = run.runId;
    const tile = document.createElement('img');
    tile.className = 'home-row-tile';
    tile.src = '/icons/markdown-32.png';
    tile.alt = '';
    const body = span('home-row-body', '');
    body.append(span('home-row-title', researchTitle(run.prompt, 40)), span('home-row-meta', 'report.md · 待核验草稿'));
    button.append(tile, body, span('home-row-time', relativeTime(run.finishedAt || run.createdAt)));
    button.addEventListener('click', () => openRun(run.runId, { withReport: true }));
    return button;
  }

  async function refreshRuns() {
    try {
      const { runs } = await api.listRuns();
      renderRunList(runs);
      return runs;
    } catch {
      showToast('研究记录暂时读取失败，请稍后刷新。', 'error');
      return [];
    }
  }

  // -- run lifecycle ---------------------------------------------------------

  async function startRun(promptOverride) {
    const prompt = String(promptOverride ?? input.value).trim();
    if (!prompt) return;
    if (current?.model?.status === 'running') {
      showToast('这项研究还在进行，请等它完成或先停止。', 'warning');
      return;
    }
    setSending(true);
    try {
      const { run } = await api.startRun(prompt);
      if (promptOverride === undefined) input.value = '';
      updatePromptCount();
      await openRun(run.runId);
      await refreshRuns();
    } catch (error) {
      showToast(startErrorMessage(error), error?.code === 'run_busy' ? 'warning' : 'error');
    } finally {
      setSending(false);
    }
  }

  function setSending(sending) {
    sendButton.disabled = sending || input.value.trim().length === 0;
    const label = sending ? '正在提交' : '开始研究';
    sendButton.setAttribute('aria-label', label);
    sendButton.dataset.tip = label;
  }

  async function openRun(runId, { withReport = false } = {}) {
    closeCurrent();
    let run;
    try {
      ({ run } = await api.getRun(runId));
    } catch {
      showToast('这项研究暂时打不开，请稍后重试。', 'error');
      return;
    }
    const model = createRunModel(run);
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
    });
    current = { runId, model, subscription: null, view, prompt: run.prompt, events: 0 };
    question.textContent = run.prompt;
    showView('chat');
    view.render(model);
    syncSelection();
    afterModelChange();
    if (withReport && model.hasReport) openReport(runId);

    current.subscription = createRunSubscription({
      runId,
      eventsUrl: api.eventsUrl,
      eventSourceFactory,
      onEvent: (event) => {
        if (disposed || current?.runId !== runId) return; // late frames from a replaced run
        current.events += 1;
        current.model = applyEvent(current.model, event);
        current.view.render(current.model);
        afterModelChange();
        // Closing on the terminal *frame* (not on the model's status) is what
        // makes history replay work: a finished run starts at status=finished.
        if (TERMINAL_EVENT_TYPES.has(event.type)) {
          current.subscription?.close();
          setConnection(null);
          refreshRuns();
        }
      },
      onStatus: (status) => {
        if (disposed || current?.runId !== runId) return;
        onStreamStatus(status);
      },
      onError: () => showToast('有一条过程记录无法显示，已跳过。', 'warning'),
    });
  }

  function onStreamStatus(status) {
    clearTimeout(reconnectTimer);
    if (status === 'live') {
      setConnection(null);
    } else if (status === 'reconnecting') {
      reconnectTimer = setTimeout(() => {
        if (current?.subscription?.status === 'reconnecting') setConnection('reconnecting');
      }, RECONNECT_GRACE_MS);
    } else if (status === 'failed') {
      current?.subscription?.close();
      setConnection('unavailable');
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
    clearTimeout(reconnectTimer);
    current?.subscription?.close();
    current?.view?.dispose();
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
    stopButton.disabled = true;
    try {
      await api.stopRun(current.runId);
      showToast('正在停止，已完成的内容会保留。', 'info');
    } catch {
      showToast('停止没有成功，请重试。', 'error');
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
    try {
      const response = await fetch(api.reportUrl(runId));
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const text = await response.text();
      if (current?.runId !== runId) return;
      previewTitle.textContent = 'report.md';
      previewTitle.title = 'report.md';
      previewDownload.href = api.reportUrl(runId);
      previewDownload.setAttribute('download', reportFileName(current.prompt));
      previewArticle.innerHTML = renderMarkdown(text);
      previewBody.scrollTop = 0;
      setSlot('report');
    } catch {
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
    const overlay = surface.getBoundingClientRect().width < CONVERSATION_MIN + 1 + REPORT_MIN;
    if (overlay !== isOverlay()) {
      shell.classList.toggle('slot-overlay', overlay);
      if (overlay && conversationCollapsed) setConversationCollapsed(false);
    }
    if (slot === 'report') applySplit(readSplit());
    syncHeader();
  };
  if (typeof ResizeObserver === 'function' && surface) new ResizeObserver(syncSlotMode).observe(surface);
  else window.addEventListener('resize', syncSlotMode);

  return {
    async init() {
      if (!shell) throw new Error('workbench shell is missing from the document');
      input.setAttribute('data-focus-ring-managed', '');
      let collapsed = false;
      try {
        collapsed = localStorage.getItem(SIDEBAR_KEY) === 'collapsed';
      } catch {
        /* private mode — fall back to expanded */
      }
      setSidebar(collapsed, { persist: false });
      await refreshService();
      await refreshRuns();
      showView('home');
      shell.hidden = false;
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

function startErrorMessage(error) {
  if (error?.code === 'run_busy') return '已有一项研究正在进行，请等它完成或先停止。';
  if (error?.code === 'not_configured') return '模型服务还没有配置，暂时无法发起研究。请联系管理员完成配置。';
  if (error?.code === 'invalid_request') return '这个问题没能提交，请检查内容后重试。';
  return '研究没能开始，请稍后重试。';
}
