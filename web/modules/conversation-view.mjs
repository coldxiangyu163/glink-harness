/** Conversation DOM and its resources. Never owns a run, network request or SSE. */
import {escapeHtml, uiIcon} from './ui.mjs';
import {t} from './i18n.mjs';
import {renderMarkdown, reportCitationSources} from './markdown.mjs';
import {artifactResolverFor, conversationArtifactLayout, renderArtifactCard, bindDownloadMenus} from './artifacts.mjs';
import {collectDraft, collectReportsWaitCopy, formatRunElapsed, recoveredFromErrorForRun, renderRun,
  renderRunOutline, renderTaskRows, runOutlineItems, selectTaskBoardRun, subagentSnapshotForRun,
  taskBoardCurrentCopy, taskBoardPresentation, taskBoardProgressCopy, tasksForRun, verificationPresentationForRun} from './run-state.mjs';

/** 流式草稿的渲染预算：delta 帧独立节流，超长草稿只渲染尾部窗口。 */
const DRAFT_RENDER_INTERVAL_MS = 250;
export const DRAFT_RENDER_TAIL_CHARS = 40_000;

export function createConversationView({environment = globalThis, actions, reports}) {
  const {document, window = environment, navigator, CSS, setTimeout, clearTimeout, setInterval,
    clearInterval, requestAnimationFrame, cancelAnimationFrame} = environment;
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  let bundle = null, activeRun = null, visible = false, disposed = false, generation = 0, binding = null;
  const state = {
    runDetailChatId: null, runDetailState: new Map(), runTimer: null,
    runOutlineCleanup: null, runOutlineFrame: null, runOutlineJumpRunId: null,
    runOutlineJumpUntil: 0, runOutlineJumpTimer: null, renderTimer: null, endScrollFrame: null,
    draftTimer: null, draftText: "", structureKey: "", runEventStructures: new Map(),
  };
  function listen(target, type, handler) {
    const revision = generation;
    target.addEventListener(type, event => {
      if (visible && !disposed && revision === generation) return handler(event);
    }, {signal: binding.signal});
  }

  function sourcesForMessage(message) {
    const sources = bundle?.sources || [];
    return reportCitationSources(message.content, sources.filter((source) => source.run_id === message.run_id));
  }

  /** 正在流式输出的草稿：仅当该 run 仍活跃且还没有正式回答时显示。 */
  function liveDraftFor(run) {
    if (!run || !["queued", "running"].includes(run.status) || !bundle) return null;
    if (bundle.messages.some((message) => message.run_id === run.id && message.role === "assistant")) return null;
    const draft = run.liveDraft || collectDraft(run);
    return String(draft || "").trim() ? draft : null;
  }

  /** 超长草稿只渲染尾部窗口，避免每一帧都重建整份报告的 DOM。 */
  function windowedDraft(text) {
    if (!(DRAFT_RENDER_TAIL_CHARS > 0) || text.length <= DRAFT_RENDER_TAIL_CHARS) return {text, omitted: 0};
    let start = text.length - DRAFT_RENDER_TAIL_CHARS;
    const paragraph = text.indexOf("\n\n", start);
    const line = text.indexOf("\n", start);
    if (paragraph !== -1 && paragraph < start + 2000) start = paragraph + 2;
    else if (line !== -1 && line < start + 400) start = line + 1;
    return {text: text.slice(start), omitted: start};
  }

  function draftReportHtml(text, run) {
    const artifacts = (bundle?.artifacts || []).filter((item) => item.run_id === run.id);
    const artifactResolver = artifactResolverFor(artifacts, (item) => (
      `/api/artifacts/${encodeURIComponent(item.id)}`
    ));
    const windowed = windowedDraft(text);
    const note = windowed.omitted
      ? `<p class="live-draft-note">${escapeHtml(t("run:chat.draftOmitted", {chars: windowed.omitted}))}</p>`
      : "";
    return note + renderMarkdown(windowed.text, {artifactResolver});
  }

  /** 非 delta 事件的结构签名：delta 帧不改变它，草稿因此可以原地更新。 */
  function eventStructure(run) {
    const events = run.events || [];
    const cached = state.runEventStructures.get(run.id);
    const incremental = Boolean(cached && cached.events === events && cached.length <= events.length);
    let signature = incremental ? cached.signature : "";
    for (let index = incremental ? cached.length : 0; index < events.length; index += 1) {
      const event = events[index];
      if (!event || event.type === "assistant_delta") continue;
      signature += `${event.seq || 0}:${event.type}|`;
    }
    state.runEventStructures.set(run.id, {events, length: events.length, signature});
    return signature;
  }

  function structureKey() {
    const parts = [`chat:${bundle?.chat?.id || ""}`, `active:${activeRun?.id || ""}:${activeRun?.status || ""}`];
    for (const message of bundle?.messages || []) {
      const content = String(message.content || "");
      parts.push(`m:${message.id}:${message.role}:${message.run_id || ""}:${content.length}:${content.slice(0, 64)}:${content.slice(-64)}`);
    }
    for (const run of bundle?.runs || []) {
      // 覆盖 renderRun/verificationPresentationForRun 真正消费的 run 字段；
      // 非 delta 事件签名捕获 run.events 的一切变化。
      const validation = run.validation || {};
      parts.push(`r:${run.id}:${run.status}:${run.mode || ""}:${run.answer_status || ""}:${run.error || ""}:${run.started_at || 0}:${run.ended_at || 0}`);
      parts.push(`v:${validation.status || ""}:${validation.evidence_status || ""}:${validation.verification_expected ?? ""}:${validation.artifact_status || ""}:${validation.degraded ?? ""}:${eventStructure(run)}`);
    }
    for (const artifact of bundle?.artifacts || []) {
      parts.push(`a:${artifact.id}:${artifact.name}:${artifact.mime || ""}:${artifact.size}:${artifact.run_id || ""}`);
    }
    for (const source of bundle?.sources || []) {
      parts.push(`s:${source.run_id || ""}:${source.url}:${source.title || ""}`);
    }
    return parts.join("|");
  }

  /** 结构与上次整段渲染一致时，草稿可以只更新它自己的节点。 */
  function draftInPlaceReady() {
    if (!bundle || !activeRun || state.structureKey !== structureKey()) return false;
    const body = $("#conversation").querySelector("[data-live-draft-body]");
    if (!body || body.dataset.liveDraftBody !== String(activeRun.id)) return false;
    return Boolean(liveDraftFor(activeRun));
  }

  function scheduleDraftFlush(delay = DRAFT_RENDER_INTERVAL_MS) {
    if (state.draftTimer !== null) return;
    const revision = generation;
    state.draftTimer = setTimeout(() => {
      // 先释放槽位：过期回调也不能拦住后续草稿帧。
      state.draftTimer = null;
      if (disposed || !visible || revision !== generation) return;
      flushDraft();
    }, delay);
  }

  function flushDraft() {
    state.draftTimer = null;
    if (disposed || !visible) return;
    if (state.runOutlineJumpRunId && Date.now() < state.runOutlineJumpUntil) {
      scheduleDraftFlush(90);
      return;
    }
    if (!draftInPlaceReady()) {
      scheduleFullRender();
      return;
    }
    const draft = liveDraftFor(activeRun);
    if (draft === state.draftText) return;
    $("#conversation").querySelector("[data-live-draft-body]").innerHTML = draftReportHtml(draft, activeRun);
    state.draftText = draft;
  }

  function renderConversation() {
    if (disposed || !visible || !bundle) return;
    // 整段渲染会带上最新草稿，排队的草稿更新不再单独执行。
    if (state.draftTimer !== null) { clearTimeout(state.draftTimer); state.draftTimer = null; }
    generation += 1;
    binding?.abort();
    binding = new (environment.AbortController || globalThis.AbortController)();
    if (state.runDetailChatId !== bundle.chat.id) {
      state.runDetailState.clear();
      state.runDetailChatId = bundle.chat.id;
    } else {
      $$("#conversation details[data-detail-key]").forEach((node) => {
        state.runDetailState.set(node.dataset.detailKey, node.open);
      });
    }
    const detailState = state.runDetailState;
    const focusedDetail = document.activeElement?.closest?.("details[data-detail-key]")?.dataset.detailKey;
    const { messages, runs, artifacts } = bundle;
    const runsById = new Map(runs.map((run) => [run.id, run]));
    const outlineItems = runOutlineItems(runs, messages);
    const outlineRunsByMessageId = new Map(outlineItems
      .filter((item) => item.anchorMessageId)
      .map((item) => [item.anchorMessageId, item.runId]));
    const artifactLayout = conversationArtifactLayout(messages, artifacts, runs);
    const showArtifactGroupHeaders = artifactLayout.groupCount > 1;
    const chunks = [];
    for (const [messageIndex, message] of messages.entries()) {
      const outlineRunId = outlineRunsByMessageId.get(String(message.id));
      const outlineAnchor = outlineRunId
        ? ` data-run-outline-anchor="${escapeHtml(outlineRunId)}"`
        : "";
      if (message.role === "user") {
        chunks.push(`<article class="message user" data-message-id="${message.id}"${outlineAnchor}><div class="user-bubble">${escapeHtml(message.content).replace(/\n/g, "<br>")}</div></article>`);
        const run = runsById.get(message.run_id);
        if (run) chunks.push(renderRun(run, detailState, artifacts));
      } else if (message.role === "assistant") {
        const messageSources = sourcesForMessage(message);
        const citationPrefix = `message-${message.id}`;
        const messageRun = runsById.get(message.run_id);
        const verification = verificationPresentationForRun(messageRun);
        const verificationBadge = verification.category === "failed"
          ? `<span class="verification-warning-badge" role="status">⚠ ${escapeHtml(t("run:verification.failedTitle"))}</span>`
          : verification.category === "error"
            ? `<span class="verification-warning-badge" role="status">⚠ ${escapeHtml(t("run:verification.errorStatus"))}</span>`
            : verification.category === "not_run" && verification.expected === true
              ? `<span class="verification-warning-badge" role="status">⚠ ${escapeHtml(t("run:verification.notRunTitle"))}</span>`
              : verification.category === "not_run" && verification.expected === false
                ? `<span class="verification-advisory-badge" role="status">${escapeHtml(t("run:verification.notApplicableTitle"))}</span>`
                : verification.category === "advisory"
                  ? `<span class="verification-advisory-badge" role="status">${escapeHtml(t("run:verification.advisoryTitle", { count: verification.issueCount ?? 0 }))}</span>`
                  : "";
        const recoveredFromError = recoveredFromErrorForRun(messageRun);
        const messageArtifacts = message.run_id
          ? artifacts.filter((item) => item.run_id === message.run_id)
          : artifacts;
        const artifactResolver = artifactResolverFor(messageArtifacts, (item) => (
          `/api/artifacts/${encodeURIComponent(item.id)}`
        ));
        const reportArtifact = messageArtifacts.find((item) => item.name === "Final Report.md");
        chunks.push(`<article class="message assistant-message" data-message-id="${message.id}"${outlineAnchor}>
          <div class="agent-label"><img class="brand-mark" src="/glink-mark.svg" alt=""><span>${escapeHtml(messageRun?.mode === "agent_team" ? "Glink Agent Team" : "Glink Research")}</span></div>
          <div class="report">${renderMarkdown(message.content, { citationPrefix, citationSources: messageSources, artifactResolver })}</div>
          <div class="message-actions"><button class="message-action" data-copy-message="${message.id}" aria-label="${escapeHtml(t("run:message.copy"))}" data-tip="${escapeHtml(t("run:message.copy"))}">${uiIcon("copy")}</button>${verificationBadge}${recoveredFromError ? `<span class="verification-warning-badge" role="status">⚠ ${escapeHtml(t("run:overview.degradedLabel"))}</span>` : ""}${reportArtifact ? `<button class="message-action" data-preview-artifact="${reportArtifact.id}" aria-label="${escapeHtml(t("run:message.viewReport"))}" data-tip="${escapeHtml(t("run:message.viewReport"))}">${uiIcon("report")}</button>` : ""}${messageSources.length ? `<button class="message-action source-count-badge" data-scroll-sources="source-${citationPrefix}-${messageSources[0].number}" aria-label="${escapeHtml(t("workspace:topbar.sources", { count: messageSources.length }))}" data-tip="${escapeHtml(t("workspace:topbar.sources", { count: messageSources.length }))}">${uiIcon("source")}<span>${messageSources.length}</span></button>` : ""}<button class="message-action" data-branch-message="${message.id}" aria-label="${escapeHtml(t("run:message.branch"))}" data-tip="${escapeHtml(t("run:message.branch"))}">${uiIcon("branch")}</button></div>
        </article>`);
      }
      const inlineArtifactGroup = artifactLayout.groupsAfterMessage.get(messageIndex);
      if (inlineArtifactGroup) {
        chunks.push(renderArtifactCard(inlineArtifactGroup, {runs, endpointFor: reports.endpoint, showGroupHeader: showArtifactGroupHeaders}));
      }
    }
    const draft = liveDraftFor(activeRun);
    if (draft) {
      // 独立节点 + data 标记：后续纯 delta 更新只重写这个节点，不重建整段会话。
      chunks.push(`<article class="message assistant-message live-draft" data-live-draft="${escapeHtml(String(activeRun.id))}"><div class="agent-label"><span class="pulse-dot"></span><span>${escapeHtml(t("run:chat.drafting"))}</span></div><div class="report" data-live-draft-body="${escapeHtml(String(activeRun.id))}">${draftReportHtml(draft, activeRun)}</div></article>`);
    }
    if (artifactLayout.unassignedGroup) {
      chunks.push(renderArtifactCard(artifactLayout.unassignedGroup, {runs, endpointFor: reports.endpoint, showGroupHeader: true}));
    }
    const conversationContent = chunks.join("") || `<p class="empty-note">${escapeHtml(t("run:chat.empty"))}</p>`;
    $("#conversation").innerHTML = `${renderRunOutline(outlineItems)}${conversationContent}`;
    $$("#conversation details[data-lazy-detail]").forEach((node) => {
      const renderedOpen = node.open;
      listen(node, "toggle", () => {
        if (node.isConnected && node.open && !renderedOpen) scheduleConversation();
      });
    });
    if (focusedDetail) {
      $$("#conversation details[data-detail-key]").find((node) => node.dataset.detailKey === focusedDetail)
        ?.querySelector("summary")?.focus({ preventScroll: true });
    }
    renderTaskRail();
    actions.rendered();
    bindConversationActions();
    reports.bindAnchors($("#conversation"));
    setupRunOutlineNavigation();
    syncRunTimer();
    // 记录本次整段渲染的结构与草稿，供后续纯 delta 帧原地更新。
    state.structureKey = structureKey();
    state.draftText = draft || "";
  }

  function clearRunOutlineNavigation(resetJump = true) {
    state.runOutlineCleanup?.();
    state.runOutlineCleanup = null;
    if (state.runOutlineFrame !== null) cancelAnimationFrame(state.runOutlineFrame);
    state.runOutlineFrame = null;
    clearTimeout(state.runOutlineJumpTimer);
    state.runOutlineJumpTimer = null;
    if (resetJump) {
      state.runOutlineJumpRunId = null;
      state.runOutlineJumpUntil = 0;
    }
  }

  function setupRunOutlineNavigation() {
    clearRunOutlineNavigation(false);
    const nav = $("#run-outline", $("#conversation"));
    if (!nav) {
      state.runOutlineJumpRunId = null;
      state.runOutlineJumpUntil = 0;
      return;
    }
    const scroller = $(".run-outline-scroller", nav);
    const buttons = $$('[data-run-outline-run]', nav);
    const messageNodes = $$('[data-message-id]', $("#conversation"));
    const anchors = buttons.map((button) => ({
      button,
      runId: button.dataset.runOutlineRun,
      target: messageNodes.find((node) => (
        node.dataset.messageId === button.dataset.runOutlineTarget
      )) || null,
    })).filter((item) => item.target);
    let activeRunId = null;
    const revision = generation;

    const revealChip = (button) => {
      if (!button || !scroller) return;
      const scrollerRect = scroller.getBoundingClientRect();
      const buttonRect = button.getBoundingClientRect();
      if (buttonRect.left < scrollerRect.left) {
        scroller.scrollBy({ left: buttonRect.left - scrollerRect.left - 6 });
      } else if (buttonRect.right > scrollerRect.right) {
        scroller.scrollBy({ left: buttonRect.right - scrollerRect.right + 6 });
      }
    };
    const setActiveRun = (runId) => {
      const activeButton = buttons.find((button) => button.dataset.runOutlineRun === runId)
        || buttons[0];
      if (!activeButton || activeRunId === activeButton.dataset.runOutlineRun) return;
      activeRunId = activeButton.dataset.runOutlineRun;
      buttons.forEach((button) => {
        if (button === activeButton) button.setAttribute("aria-current", "step");
        else button.removeAttribute("aria-current");
      });
      revealChip(activeButton);
    };
    const runAtScrollPosition = () => {
      const threshold = Math.max(nav.getBoundingClientRect().bottom + 12, window.innerHeight * 0.35);
      let current = anchors[0]?.runId || buttons[0]?.dataset.runOutlineRun || "";
      for (const anchor of anchors) {
        if (anchor.target.getBoundingClientRect().top > threshold) break;
        current = anchor.runId;
      }
      return current;
    };
    const refresh = () => {
      if (!visible || disposed || revision !== generation) return;
      state.runOutlineFrame = null;
      const stickyTop = Number.parseFloat(window.getComputedStyle(nav).top) || 0;
      const hasScrollableContentBelow = (
        window.scrollY + window.innerHeight < document.documentElement.scrollHeight - 1
      );
      const isStuck = (
        window.scrollY > 0 && nav.getBoundingClientRect().top <= stickyTop + 1
      );
      nav.classList.toggle("has-scroll-content", isStuck && hasScrollableContentBelow);
      const jumpIsActive = state.runOutlineJumpRunId
        && Date.now() < state.runOutlineJumpUntil
        && buttons.some((button) => button.dataset.runOutlineRun === state.runOutlineJumpRunId);
      if (jumpIsActive) setActiveRun(state.runOutlineJumpRunId);
      else {
        state.runOutlineJumpRunId = null;
        state.runOutlineJumpUntil = 0;
        setActiveRun(runAtScrollPosition());
      }
    };
    const scheduleRefresh = () => {
      if (!visible || disposed || revision !== generation) return;
      if (state.runOutlineFrame !== null) return;
      state.runOutlineFrame = requestAnimationFrame(refresh);
    };
    const finishJump = () => {
      if (!state.runOutlineJumpRunId) return;
      clearTimeout(state.runOutlineJumpTimer);
      state.runOutlineJumpRunId = null;
      state.runOutlineJumpUntil = 0;
      state.runOutlineJumpTimer = null;
      scheduleRefresh();
    };
    const releaseJumpLock = () => {
      const remaining = state.runOutlineJumpUntil - Date.now();
      if (remaining > 0) {
        state.runOutlineJumpTimer = setTimeout(releaseJumpLock, remaining);
        return;
      }
      finishJump();
    };

    buttons.forEach((button) => listen(button, "click", (event) => {
      const anchor = anchors.find((item) => item.runId === button.dataset.runOutlineRun);
      if (!anchor) return;
      const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      const smooth = event.detail !== 0 && !reducedMotion;
      const lockDuration = smooth ? 2500 : 120;
      state.runOutlineJumpRunId = anchor.runId;
      state.runOutlineJumpUntil = Date.now() + lockDuration;
      clearTimeout(state.runOutlineJumpTimer);
      setActiveRun(anchor.runId);
      anchor.target.scrollIntoView({ block: "start", behavior: smooth ? "smooth" : "auto" });
      state.runOutlineJumpTimer = setTimeout(releaseJumpLock, lockDuration);
    }));
    window.addEventListener("scroll", scheduleRefresh, { passive: true });
    window.addEventListener("scrollend", finishJump, { passive: true });
    window.addEventListener("resize", scheduleRefresh, { passive: true });
    state.runOutlineCleanup = () => {
      window.removeEventListener("scroll", scheduleRefresh);
      window.removeEventListener("scrollend", finishJump);
      window.removeEventListener("resize", scheduleRefresh);
    };

    const remaining = state.runOutlineJumpUntil - Date.now();
    if (state.runOutlineJumpRunId && remaining > 0) {
      setActiveRun(state.runOutlineJumpRunId);
      state.runOutlineJumpTimer = setTimeout(releaseJumpLock, remaining);
    } else {
      refresh();
    }
  }

  function syncTaskRailEntries(runId) {
    $$('[data-open-tasks]', $("#conversation")).forEach((button) => {
      if (runId && button.dataset.openTasks === runId) button.setAttribute("data-task-entry-railed", "");
      else button.removeAttribute("data-task-entry-railed");
    });
  }

  function renderTaskRail() {
    const rail = $("#task-rail");
    const layout = $("#chat-layout");
    const run = selectTaskBoardRun(bundle?.runs, activeRun);
    const tasks = tasksForRun(run);
    if (!run || !tasks.length) {
      rail.hidden = true;
      rail.innerHTML = "";
      layout.classList.remove("has-task-rail");
      syncTaskRailEntries(null);
      return;
    }
    const board = taskBoardPresentation(tasks, run.status, subagentSnapshotForRun(run));
    const currentCopy = taskBoardCurrentCopy(board);
    const progressCopy = taskBoardProgressCopy(board);
    rail.innerHTML = `<header class="task-board-head"><div><p class="eyebrow">RUN PLAN</p><h2>${escapeHtml(t("run:task.boardTitle"))}</h2></div><strong>${escapeHtml(progressCopy)}</strong></header><p class="task-board-current">${escapeHtml(currentCopy)}</p>${renderTaskRows(board)}`;
    rail.hidden = false;
    layout.classList.add("has-task-rail");
    syncTaskRailEntries(run.id);
  }

  function syncRunTimer() {
    clearInterval(state.runTimer);
    state.runTimer = null;
    const update = () => {
      if (!visible || disposed) return;
      $$('[data-run-elapsed]', $("#conversation")).forEach((node) => {
        const run = bundle?.runs.find((item) => item.id === node.dataset.runElapsed);
        if (run) node.textContent = formatRunElapsed(run);
      });
      // Reuse the existing elapsed-time refresh; status copy needs no new
      // polling request or timer while collect_reports is waiting.
      $$('[data-run-activity]', $("#conversation")).forEach((node) => {
        const total = node.dataset.subagentTotal === ""
          ? null : Number(node.dataset.subagentTotal);
        node.textContent = collectReportsWaitCopy(
          Number(node.dataset.collectStartedAt) || 0,
          total,
          Number(node.dataset.subagentRemaining) || 0,
        );
      });
    };
    update();
    const hasActiveRun = bundle?.runs.some((run) => ["queued", "running"].includes(run.status));
    if (hasActiveRun) state.runTimer = setInterval(update, 1000);
  }

  function bindConversationActions() {
    const root = document.querySelector("#conversation");
    const $ = selector => root.querySelector(selector);
    const $$ = selector => [...root.querySelectorAll(selector)];
    $$('[data-copy-message]').forEach((button) => listen(button, "click", () => {
      const message = bundle.messages.find((x) => x.id === button.dataset.copyMessage);
      navigator.clipboard.writeText(message?.content || "").then(() => actions.notify(t("run:chat.reportCopied")));
    }));
    $$('[data-branch-message]').forEach((button) => listen(button, "click", () => actions.branchMessage(button.dataset.branchMessage)));
    $$('[data-open-tasks]').forEach((button) => listen(button, "click", () => actions.openTasks(button.dataset.openTasks)));
    $$('[data-preview-artifact]').forEach((button) => listen(button, "click", () => {
      button.closest(".artifact-menu")?.setAttribute("hidden", "");
      reports.open(button.dataset.previewArtifact);
    }));
    $$('[data-artifact-menu]').forEach((button) => listen(button, "click", () => {
      const menu = $(`[data-artifact-menu-popover="${button.dataset.artifactMenu}"]`);
      $$(".artifact-menu").forEach((item) => { if (item !== menu) item.hidden = true; });
      menu.hidden = !menu.hidden;
    }));
    $$('[data-store-artifact]').forEach((button) => listen(button, "click", async () => {
      button.closest(".artifact-menu").hidden = true;
      await actions.storeArtifact(button.dataset.storeArtifact);
    }));
    $$('[data-scroll-sources]').forEach((button) => listen(button, "click", () => {
      const target = root.querySelector(`#${CSS.escape(button.dataset.scrollSources)}`);
      target?.scrollIntoView({ block: "center", behavior: "smooth" });
      reports.highlightSource(target);
    }));
    bindDownloadMenus(root, {signal: binding.signal});
  }

  function scheduleConversation() { schedule(bundle, activeRun); }

  function cancelRender() {
    clearTimeout(state.renderTimer);
    state.renderTimer = null;
    if (state.draftTimer !== null) { clearTimeout(state.draftTimer); state.draftTimer = null; }
    if (state.endScrollFrame !== null) cancelAnimationFrame(state.endScrollFrame);
    state.endScrollFrame = null;
  }

  function show(nextBundle, nextActiveRun, {scrollToEnd = false} = {}) {
    if (disposed) return;
    if (!nextBundle) { hide(); return; }
    cancelRender();
    if (bundle?.chat.id !== nextBundle.chat.id) clearRunOutlineNavigation();
    bundle = nextBundle;
    activeRun = nextActiveRun;
    visible = true;
    renderConversation();
    if (scrollToEnd) {
      const revision = generation;
      state.endScrollFrame = requestAnimationFrame(() => {
        if (!visible || disposed || revision !== generation) return;
        state.endScrollFrame = null;
        $("#conversation").lastElementChild?.scrollIntoView({block: 'end'});
      });
    }
  }

  function scheduleFullRender() {
    if (state.renderTimer !== null) return;
    if (state.draftTimer !== null) { clearTimeout(state.draftTimer); state.draftTimer = null; }
    const revision = generation;
    const flush = () => {
      if (disposed || !visible || revision !== generation) return;
      if (state.runOutlineJumpRunId && Date.now() < state.runOutlineJumpUntil) {
        state.renderTimer = setTimeout(flush, 90);
        return;
      }
      state.renderTimer = null;
      renderConversation();
    };
    state.renderTimer = setTimeout(flush, 90);
  }

  function schedule(nextBundle, nextActiveRun, options = {}) {
    if (disposed || !visible || nextBundle?.chat.id !== bundle?.chat.id) return;
    bundle = nextBundle;
    activeRun = nextActiveRun;
    if (!bundle.runs.some(run => ['queued', 'running'].includes(run.status))) {
      clearInterval(state.runTimer);
      state.runTimer = null;
    }
    // 纯 delta 增长：结构与其余消息未变，只重渲染流式草稿节点。
    if (options.draftOnly && state.renderTimer === null && draftInPlaceReady()) {
      scheduleDraftFlush();
      return;
    }
    scheduleFullRender();
  }

  function hide() {
    if (visible) {
      $$('#conversation details[data-detail-key]').forEach(node => state.runDetailState.set(node.dataset.detailKey, node.open));
    }
    visible = false;
    generation += 1;
    binding?.abort();
    binding = null;
    cancelRender();
    clearInterval(state.runTimer);
    state.runTimer = null;
    clearRunOutlineNavigation();
    state.runEventStructures.clear();
    state.structureKey = "";
    state.draftText = "";
    bundle = null;
    activeRun = null;
  }

  function dispose() {
    if (disposed) return;
    hide();
    disposed = true;
    state.runDetailState.clear();
    state.runDetailChatId = null;
  }

  return {show, schedule, hide, dispose};
}
