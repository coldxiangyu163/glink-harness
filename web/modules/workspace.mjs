/**
 * Workbench shell: run history, composer, live run view, report preview.
 *
 * The shell owns navigation and nothing else. All run semantics live in
 * `run-stream.mjs` (event → model) and `run-view.mjs` (model → DOM), so the
 * interesting behaviour is testable without a browser.
 */

import { createApi } from './api.mjs';
import { applyEvent, createRunModel, createRunSubscription, runSummary, TERMINAL_EVENT_TYPES, TERMINAL_STATUSES } from './run-stream.mjs';
import { createRunView } from './run-view.mjs';
import { renderMarkdown } from './markdown.mjs';

const MAX_PROMPT = 32000;
const RECONNECT_LABELS = {
  idle: '',
  connecting: '连接事件流…',
  live: '事件流已连接',
  reconnecting: '事件流重连中…',
  closed: '事件流已关闭',
};

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
  const hostStatus = el('host-status');
  const conversation = el('conversation');
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
  const toast = el('toast');

  let current = null; // { runId, model, subscription, view }
  let toastTimer = null;
  let disposed = false;

  // -- navigation ------------------------------------------------------------

  function showView(name) {
    homeView.hidden = name !== 'home';
    chatView.hidden = name !== 'chat';
    composer.hidden = false;
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
      hostStatus.dataset.ready = health.configured.model ? 'true' : 'false';
    } catch (error) {
      hostStatus.textContent = `宿主不可达：${error.message}`;
      hostStatus.dataset.ready = 'false';
    }
  }

  function renderRunList(runs) {
    runList.replaceChildren();
    if (!runs.length) {
      const empty = document.createElement('p');
      empty.className = 'empty-note';
      empty.textContent = '还没有运行记录。';
      runList.append(empty);
      return;
    }
    for (const run of runs) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'nav-row nav-chat-row';
      button.dataset.runId = run.runId;
      button.dataset.active = current?.runId === run.runId ? 'true' : 'false';
      const title = document.createElement('span');
      title.className = 'nav-chat';
      title.textContent = run.prompt.length > 34 ? `${run.prompt.slice(0, 34)}…` : run.prompt;
      const badge = document.createElement('em');
      badge.dataset.status = run.status;
      badge.textContent = statusLabel(run.status);
      button.append(title, badge);
      button.addEventListener('click', () => openRun(run.runId));
      runList.append(button);
    }
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
    sendButton.disabled = true;
    sendButton.textContent = '提交中…';
    try {
      const { run } = await api.startRun(prompt);
      input.value = '';
      updatePromptCount();
      showView('chat');
      await openRun(run.runId);
      await refreshRuns();
    } catch (error) {
      sendButton.disabled = false;
      sendButton.textContent = '开始研究';
      showToast(startErrorMessage(error), 'error');
      return;
    }
    sendButton.disabled = false;
    sendButton.textContent = '开始研究';
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
    current = { runId, model, subscription: null, view };
    showView('chat');
    question.textContent = run.prompt;
    view.render(model);
    updateBanner(model);
    runList.querySelectorAll('[data-run-id]').forEach((node) => {
      node.dataset.active = node.dataset.runId === runId ? 'true' : 'false';
    });

    current.subscription = createRunSubscription({
      runId,
      eventsUrl: api.eventsUrl,
      eventSourceFactory,
      onEvent: (event) => {
        if (disposed || current?.runId !== runId) return; // late frames from a replaced run
        current.model = applyEvent(current.model, event);
        current.view.render(current.model);
        updateBanner(current.model);
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
  }

  function updateBanner(model) {
    const terminal = TERMINAL_STATUSES.has(model.status);
    banner.hidden = terminal;
    stopButton.hidden = terminal;
    if (terminal) {
      const summary = runSummary(model);
      bannerTitle.textContent = '运行已结束';
      banner.dataset.status = model.status;
      return;
    }
    bannerTitle.textContent = 'pi 正在本机运行';
    banner.dataset.status = model.status;
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
      // class the drawer renders at 0px width and overflows the layout.
      shell.classList.add('preview-open');
    } catch (error) {
      showToast(`打开报告失败：${error.message}`, 'error');
    }
  }

  function closeReport() {
    previewDrawer.hidden = true;
    shell.classList.remove('preview-open');
  }

  // -- composer --------------------------------------------------------------

  function updatePromptCount() {
    const length = input.value.length;
    promptCount.hidden = length === 0;
    promptCount.textContent = `${length} / ${MAX_PROMPT}`;
    sendButton.disabled = length === 0;
  }

  // -- wiring ----------------------------------------------------------------

  input?.addEventListener('input', updatePromptCount);
  input?.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
      event.preventDefault();
      startRun();
    }
  });
  sendButton?.addEventListener('click', startRun);
  stopButton?.addEventListener('click', stopRun);
  el('new-research')?.addEventListener('click', () => {
    closeCurrent();
    showView('home');
    input.focus();
  });
  previewClose?.addEventListener('click', () => {
    closeReport();
  });

  return {
    async init() {
      if (!shell) throw new Error('workbench shell is missing from the document');
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
    dispose() {
      disposed = true;
      closeCurrent();
      clearTimeout(toastTimer);
    },
  };
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

function startErrorMessage(error) {
  if (error?.code === 'run_busy') return '已有 run 在运行，先停止或等它结束。';
  if (error?.code === 'not_configured') return '未配置模型凭据：请在 .env 填 OPENAI_API_KEY。';
  return `启动失败：${error.message}`;
}
