import {reviewThreadStatus as threadStatus} from './review-anchors.mjs';
import { escapeHtml, formatDate, uiIcon } from './ui.mjs';
import { reviewThreadRequest as reviewThreadRequestRequest } from './requests.mjs';
import { renderMarkdown } from './markdown.mjs';
import { getLocale, t } from './i18n.mjs';

import { bindReviewHighlight, markReviewAnchor, reviewThreadReadOnly, reviewThreadsForVersion, reviewVersionForArtifact, selectionReviewAnchor } from './review-anchors.mjs';

/** Owns review requests and unsent input; receives only the visible report/run context. */
export function createReviewWorkspace({environment, request: api, notify: toast, context: getContext, reports, runs}) {
  const {document, window = environment, CSS, setTimeout, clearTimeout, confirm} = environment;
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const {open: openArtifactPreview, render: renderPreviewReport} = reports;
  const {refresh: refreshAll, loadChat, stop: stopRun} = runs;
  const previewArtifacts = () => getContext().current?.artifacts || [];
  const reviewThreadRequest = (chat, thread, action, body) => reviewThreadRequestRequest(chat, thread, action, body, api);
  const state = {
   reviewData: null, reviewDrafts: new Map(), reviewIssueDrafts: new Map(), reviewThreadDrafts: new Map(),
   reviewPending: new Set(), reviewSelection: null, reviewNavigation: 0, reviewLoadSequence: 0,
   reviewLocations: new Map(), reviewTimer: null, draftSaving: '', reviewBusy: false,
  };
  function carryReviewConversation(from, to) {
    const previous = state.reviewDrafts.get(from);
    if (previous?.activeThread) state.reviewDrafts.set(to, {
      ...previous, history: false, knowledge: false,
      input: { message: "", anchor: null, role: "", action: "question" },
    });
  }

  function reviewDraft() {
    const key = reports.snapshot().artifactId;
    if (!state.reviewDrafts.has(key)) state.reviewDrafts.set(key, {
      activeThread: null, creating: true, history: false, knowledge: false,
      input: { message: "", anchor: null, role: "", action: "question" },
    });
    return state.reviewDrafts.get(key);
  }

  function hideReviewSelection() {
    state.reviewSelection = null;
    const toolbar = $("#review-selection-toolbar");
    if (toolbar) toolbar.hidden = true;
  }

  function captureReviewAnchor() {
    if (getContext().shareToken || !reports.snapshot().artifactId || !reviewVersionForArtifact(state.reviewData, reports.snapshot().artifactId)) return hideReviewSelection();
    const selection = window.getSelection();
    const anchor = selectionReviewAnchor($("#preview-body > .report"), selection);
    if (!anchor || anchor.error) {
      if (anchor?.error) toast(t("review:selection.tooLong"));
      return hideReviewSelection();
    }
    state.reviewSelection = anchor;
    const toolbar = $("#review-selection-toolbar");
    const rect = selection.getRangeAt(0).getBoundingClientRect();
    toolbar.hidden = false;
    toolbar.style.left = `${Math.max(8, Math.min(rect.left, window.innerWidth - toolbar.offsetWidth - 8))}px`;
    toolbar.style.top = `${Math.max(8, Math.min(rect.bottom + 8, window.innerHeight - toolbar.offsetHeight - 8))}px`;
    $$("[data-selection-action]", toolbar).forEach((button) => {
      let activated = false;
      const begin = () => {
        if (activated) return;
        activated = true;
        beginReviewThread(button.dataset.selectionAction, anchor);
      };
      button.onmousedown = (event) => {
        if (event.button !== 0) return;
        event.preventDefault();
        // Act before selectionchange can hide the toolbar; retain this selection's anchor.
        begin();
      };
      button.onclick = (event) => {
        // Native Enter/Space activation has detail=0; a mouse click must not run twice.
        if (event.detail === 0) begin();
      };
    });
  }

  // 保存态存协议码，渲染时才映射成当前语言的文案。
  const DRAFT_SAVING_LABEL_KEYS = Object.freeze({
    finalizing: "review:version.generating",
    saving: "review:version.saving",
    failed: "review:version.saveFailed",
  });

  // 动作文案只存 key，渲染时再取当前语言：实例创建语言不得冻结在闭包里。
  const REVIEW_SELECTION_ACTIONS = {
    modify: {
      label: "review:selection.modify", button: "review:selection.modifyButton",
      placeholder: "review:selection.modifyPlaceholder", prompt: "review:selection.modifyPrompt",
      description: "review:selection.modifyDescription", notice: "review:selection.modifyNotice",
    },
    evidence: {
      label: "review:selection.evidence", button: "review:selection.evidenceButton",
      placeholder: "review:selection.evidencePlaceholder", prompt: "review:selection.evidencePrompt",
      description: "review:selection.evidenceDescription", notice: "review:selection.evidenceNotice",
    },
  };

  function reviewSelectionAction(input) {
    const spec = input?.anchor && Object.hasOwn(REVIEW_SELECTION_ACTIONS, input.action)
      ? REVIEW_SELECTION_ACTIONS[input.action] : null;
    if (!spec) return null;
    return Object.fromEntries(Object.entries(spec).map(([key, value]) => [key, t(value)]));
  }

  function beginReviewThread(action, anchor = state.reviewSelection) {
    if (getContext().shareToken || !reports.snapshot().artifactId || (action !== "global" && !anchor)) return;
    const draft = reviewDraft();
    draft.creating = true;
    draft.activeThread = null;
    draft.history = false;
    draft.input.anchor = action === "global" ? null : { ...anchor };
    draft.input.role = action === "evidence" ? "evidence_reviewer" : "";
    draft.input.action = action;
    draft.input.error = "";
    draft.expandedQuotes?.delete("new");
    state.reviewNavigation += 1;
    hideReviewSelection();
    renderReviewWorkspace();
    $("#review-thread-input")?.focus();
  }

  function clearReviewSelectionQuote() {
    if (getContext().shareToken || !reports.snapshot().artifactId) return;
    const draft = reviewDraft(), version = reviewVersionForArtifact(state.reviewData, reports.snapshot().artifactId);
    if (!version || !draft.creating || !draft.input.anchor || state.reviewPending.has(`${getContext().current.chat.id}:${version.id}:new`)) return;
    beginReviewThread("global");
    window.getSelection?.()?.removeAllRanges?.();
  }

  function applyReviewHighlights() {
    const root = $("#preview-body > .report");
    const version = reviewVersionForArtifact(state.reviewData, reports.snapshot().artifactId);
    if (!root || !version || getContext().shareToken) return;
    $$("mark[data-thread-id]", root).reverse().forEach((mark) => mark.replaceWith(...mark.childNodes));
    root.normalize();
    state.reviewLocations.clear();
    for (const thread of reviewThreadsForVersion(state.reviewData, version)) {
      if (thread.scope !== "selection" || !["direct", "mapped"].includes(thread.projection_status)) continue;
      state.reviewLocations.set(thread.id, markReviewAnchor(root, thread.anchor, thread.id, thread.title) ? thread.projection_status : "relink_required");
    }
    $$("mark[data-thread-id]", root).forEach((mark) => bindReviewHighlight(mark, openReviewThread, () => window.getSelection()));
  }

  function openReviewThread(id) {
    const thread = state.reviewData?.threads?.find((item) => item.id === id);
    if (!thread || getContext().shareToken || !reports.snapshot().artifactId) return;
    const version = reviewVersionForArtifact(state.reviewData, reports.snapshot().artifactId);
    if (version && !reviewThreadsForVersion(state.reviewData, version).some((item) => item.id === id)) {
      const origin = state.reviewData.versions.find((item) => item.id === thread.version_id);
      if (!origin || !previewArtifacts().some((item) => item.id === origin.artifact_id)) return toast(t("review:toast.historicalVersion"));
      const opening = openArtifactPreview(origin.artifact_id);
      const generation = reports.snapshot().generation, navigation = state.reviewNavigation;
      return opening.then(async () => {
        if (generation !== reports.snapshot().generation || navigation !== state.reviewNavigation) return;
        await loadReviewWorkspace(generation);
        if (generation === reports.snapshot().generation && navigation === state.reviewNavigation) openReviewThread(id);
      });
    }
    const draft = reviewDraft();
    draft.activeThread = id;
    draft.creating = false;
    draft.history = false;
    state.reviewNavigation += 1;
    renderReviewWorkspace();
    const scroll = $("#review-scroll");
    if (scroll) scroll.scrollTop = scroll.scrollHeight;
  }

  function locateReviewThread(id) {
    let mark = $$("mark[data-thread-id]", $("#preview-body")).find((node) => node.dataset.threadId === id);
    const thread = state.reviewData?.threads?.find((item) => item.id === id);
    if (!mark && thread?.scope === "section") {
      const headings = $$("h1,h2,h3,h4,h5,h6", $("#preview-body")).filter((node) => node.textContent === thread.anchor?.heading);
      if (headings.length === 1) mark = headings[0];
    }
    if (mark) {
      mark.scrollIntoView({ block: "center", behavior: "instant" });
      mark.classList.add("review-highlight-active");
      setTimeout(() => mark.classList.remove("review-highlight-active"), 1500);
    } else toast(t("review:toast.locateFailed"));
  }

  function reviewThreadDraft(threadId) {
    const key = `${getContext().current.chat.id}:${reports.snapshot().artifactId}:${threadId}`;
    if (!state.reviewThreadDrafts.has(key)) state.reviewThreadDrafts.set(key, { message: "" });
    return state.reviewThreadDrafts.get(key);
  }

  function draftContext(version) {
    const draft = state.reviewData?.draft;
    return { version_id: version.id, ...(draft?.base_version_id === version.id ? { draft_id: draft.id, draft_revision: draft.revision } : {}) };
  }

  async function submitReviewThread(action) {
    if (getContext().shareToken || !reports.snapshot().artifactId) return;
    const draft = reviewDraft();
    if (draft.history) return;
    const version = reviewVersionForArtifact(state.reviewData, reports.snapshot().artifactId);
    const thread = state.reviewData?.threads?.find((item) => item.id === draft.activeThread);
    const creating = !thread && action === "messages";
    const input = creating ? draft.input : thread && reviewThreadDraft(thread.id);
    const selectionAction = creating ? reviewSelectionAction(input) : null;
    const text = input?.message.trim() || selectionAction?.prompt || "";
    if (!version || (!creating && (!thread || reviewThreadReadOnly(thread))) || (action === "messages" && !text)) return;
    const chatId = getContext().current.chat.id;
    const key = `${chatId}:${version.id}:${creating ? "new" : thread.id}`;
    if (state.reviewPending.has(key) || (!creating && threadJobBusy(thread.id))) return;
    const generation = reports.snapshot().generation, navigation = state.reviewNavigation;
    const sent = input?.message, sentAction = input?.action, sentAnchor = input?.anchor;
    const message = creating ? `${input.action === "modify" ? t("review:selection.modifyTarget") : input.action === "evidence" ? t("review:selection.evidenceTarget") : ""}${text}`.slice(0, 4000) : text;
    const body = { ...draftContext(version), ...(creating ? { kind: "question", scope: input.anchor ? "selection" : "document", anchor: input.anchor || {}, role: input.role, title: (input.message.trim() || `${selectionAction.label} · ${input.anchor.heading || t("review:selection.fallbackTitle")}`).slice(0, 120), message }
      : action === "messages" ? { message: input.message.trim(), role: thread.role || "" } : {}) };
    state.reviewPending.add(key);
    if (input && action === "messages") input.error = "";
    renderReviewWorkspace();
    try {
      const result = await reviewThreadRequest(chatId, thread?.id, creating ? "create" : action, body);
      if (input && action === "messages" && input.message === sent && (!creating || (input.anchor === sentAnchor && input.action === sentAction))) {
        input.message = ""; if (creating) input.anchor = null;
      }
      if (generation !== reports.snapshot().generation || getContext().current?.chat.id !== chatId) return;
      if (creating && result.thread && navigation === state.reviewNavigation) {
        draft.creating = false;
        draft.activeThread = result.thread.id;
      }
      await loadReviewWorkspace(generation);
      if (!draft.history && navigation === state.reviewNavigation && generation === reports.snapshot().generation && $("#review-scroll")) $("#review-scroll").scrollTop = $("#review-scroll").scrollHeight;
    } catch (err) {
      const retained = input && action === "messages" && input.message === sent && (!creating || (input.anchor === sentAnchor && input.action === sentAction));
      if (retained) input.error = err.message;
      else if (generation === reports.snapshot().generation && navigation === state.reviewNavigation) toast(err.message);
    }
    finally {
      state.reviewPending.delete(key);
      if (generation === reports.snapshot().generation && state.reviewData) renderReviewWorkspace();
    }
  }

  function threadJobBusy(id) {
    return (state.reviewData?.jobs || []).some((job) => job.thread_id === id && ["queued", "running"].includes(job.status));
  }

  async function loadReviewWorkspace(generation = reports.snapshot().generation) {
    if (!getContext().current || getContext().shareToken || !reports.snapshot().artifactId || generation !== reports.snapshot().generation) return;
    const sequence = ++state.reviewLoadSequence;
    clearTimeout(state.reviewTimer);
    const chatId = getContext().current.chat.id;
    try {
      const data = await api(`/api/chats/${chatId}/review?artifact_id=${encodeURIComponent(reports.snapshot().artifactId)}`);
      if (generation !== reports.snapshot().generation || sequence !== state.reviewLoadSequence || getContext().current?.chat.id !== chatId) return;
      const changed = JSON.stringify(data) !== JSON.stringify(state.reviewData);
      state.reviewData = data;
      if (changed || !$("#review-version")) {
        if (reports.snapshot().text !== null) renderPreviewReport(data.draft?.content ?? reports.snapshot().text);
        applyReviewHighlights(); renderReviewWorkspace();
      }
      if (data.jobs.some((job) => ["queued", "running"].includes(job.status))) {
        state.reviewTimer = setTimeout(() => loadReviewWorkspace(generation), 1200);
      }
    } catch (err) {
      if (generation !== reports.snapshot().generation || sequence !== state.reviewLoadSequence) return;
      if (state.reviewData) { toast(err.message); return; }
      $("#review-rail").innerHTML = `<p role="alert">${escapeHtml(err.message)}</p><button class="button" id="review-retry">${escapeHtml(t("review:rail.retryLoad"))}</button>`;
      $("#review-retry").onclick = () => loadReviewWorkspace(generation);
    }
  }

  const reviewThreadStatus = (thread, data) => threadStatus(thread, data, state.reviewLocations);

  function renderReviewSuggestion(item, data) {
    const thread = data.threads.find((entry) => entry.id === item.thread_id);
    const resolved = data.versions.find((entry) => entry.id === thread?.resolved_version_id);
    const research = item.needs_research || item.operation === "research";
    const applied = item.draft_status === "applied";
    const version = reviewVersionForArtifact(data, reports.snapshot().artifactId);
    const latest = version && !data.versions.some((entry) => entry.number > version.number);
    const actionable = latest && !research && !applied && !["applied", "dismissed"].includes(item.status) && !reviewThreadReadOnly(thread);
    const title = escapeHtml(t(research ? "review:suggestion.researchTitle" : "review:suggestion.modifyTitle"));
    const stateLabel = resolved
      ? t("review:status.resolvedAt", { number: resolved.number })
      : applied ? t("review:suggestion.applied")
        : item.status === "dismissed" ? t("review:suggestion.dismissed") : t("review:suggestion.pending");
    const proposedFallback = item.operation === "delete" ? t("review:suggestion.deleted") : t("review:suggestion.researchPlaceholder");
    return `<article class="review-suggestion" data-suggestion-card="${escapeHtml(item.id)}"><header><strong>${title}</strong><small>${escapeHtml(stateLabel)}</small></header><p>${escapeHtml(t("review:suggestion.original"))}</p><blockquote>${escapeHtml(item.original_text || t("review:suggestion.empty"))}</blockquote><p>${escapeHtml(t("review:suggestion.proposed"))}</p><blockquote class="review-proposed">${escapeHtml(item.proposed_text || proposedFallback)}</blockquote><p>${escapeHtml(t("review:suggestion.rationale", { rationale: item.rationale ?? "" }))}</p>${research ? `<p class="review-notice" data-evidence-gap>${escapeHtml(t("review:suggestion.requiresResearch"))}</p>` : ""}${actionable ? `<div class="review-issue-actions"><button class="button primary" data-draft-apply="${escapeHtml(item.id)}" ${state.reviewBusy ? "disabled" : ""}>${escapeHtml(t("review:suggestion.apply"))}</button><button class="button" data-suggestion-dismiss="${escapeHtml(item.id)}">${escapeHtml(t("review:suggestion.dismiss"))}</button></div>` : ""}</article>`;
  }

  // 计划卡：多处修改的完整对照与整批应用；单处计划仍走原建议卡。
  function planErrorText(error) {
    const keys = Object.freeze({
      missing: "review:plan.error.missing", ambiguous: "review:plan.error.ambiguous",
      overlap: "review:plan.error.overlap", unsafe: "review:plan.error.unsafe",
      limit: "review:plan.error.limit", protocol: "review:plan.error.protocol",
      invalid: "review:plan.error.invalid", empty: "review:plan.error.empty",
      truncated: "review:plan.error.truncated", incomplete: "review:plan.error.incomplete",
    });
    const key = keys[error?.code];
    if (key) return t(key);
    return t("review:plan.error.internal");
  }

  function renderReviewPlan(plan, data) {
    const version = reviewVersionForArtifact(data, reports.snapshot().artifactId);
    const latest = version && !data.versions.some((entry) => entry.number > version.number);
    const applied = plan.status === "applied";
    const researchSuggestionIds = new Set(plan.edits.filter((edit) => edit.needs_research).map((edit) => edit.suggestion_id));
    const researchCards = (data.suggestions || []).filter((item) => researchSuggestionIds.has(item.id));
    const actionable = latest && plan.status === "proposed" && !plan.stale && !state.reviewBusy
      && !(getContext().activeRun && ["queued", "running"].includes(getContext().activeRun.status));
    const statusLabel = applied ? t("review:plan.applied")
      : plan.status === "dismissed" ? t("review:plan.dismissed") : t("review:plan.proposed");
    const edits = plan.edits.map((edit) => {
      const research = edit.needs_research || edit.operation === "research";
      const where = typeof edit.line === "number" ? `<small>${escapeHtml(t("review:plan.line", { line: edit.line }))}</small>` : "";
      if (research) {
        return `<li class="review-plan-edit" data-operation="research">${where}<p>${escapeHtml(t("review:plan.research"))}</p><blockquote>${escapeHtml(edit.search || t("review:suggestion.empty"))}</blockquote></li>`;
      }
      const operation = t(`review:plan.${edit.operation}`);
      const after = edit.operation === "insert"
        ? `<p>${escapeHtml(t("review:plan.after"))}</p><blockquote class="review-proposed">${escapeHtml(edit.replace)}</blockquote>`
        : `<p>${escapeHtml(t("review:plan.proposedLabel"))}</p><blockquote class="review-proposed">${escapeHtml(edit.replace || t("review:suggestion.deleted"))}</blockquote>`;
      return `<li class="review-plan-edit" data-operation="${escapeHtml(edit.operation)}">${where}<p>${escapeHtml(operation)} · ${escapeHtml(t("review:plan.original"))}</p><blockquote>${escapeHtml(edit.search)}</blockquote>${after}<small>${escapeHtml(t("review:suggestion.rationale", { rationale: edit.rationale ?? "" }))}</small></li>`;
    }).join("");
    const researchNote = plan.research ? `<p class="review-notice">${escapeHtml(t("review:plan.researchNote", { count: plan.research }))}</p>` : "";
    const staleNote = plan.stale ? `<p class="review-notice">${escapeHtml(t("review:plan.stale"))}</p>` : "";
    const applyButton = plan.edits.some(edit => !edit.needs_research && edit.operation !== "research")
      ? `<button class="button primary" data-plan-apply="${escapeHtml(plan.id)}">${escapeHtml(t("review:plan.apply"))}</button>` : "";
    const actions = actionable
      ? `<div class="review-issue-actions">${applyButton}<button class="button" data-plan-dismiss="${escapeHtml(plan.id)}">${escapeHtml(t("review:plan.dismiss"))}</button></div>`
      : "";
    return `<article class="review-plan" data-plan-card="${escapeHtml(plan.id)}"><header><strong>${escapeHtml(t("review:plan.title", { count: plan.edits.length }))}</strong><small>${escapeHtml(statusLabel)}</small></header><ol class="review-plan-edits">${edits}</ol>${researchNote}${staleNote}<p class="review-plan-atomic">${escapeHtml(t("review:plan.atomicNotice"))}</p>${actions}${researchCards.map((item) => renderReviewSuggestion(item, data)).join("")}</article>`;
  }

  // C2 producer/consumer 闭环：折叠开关的文案由 JS 写入 data-label，
  // styles.css 的 ::after 只读 attr(data-label)，不内联任何文案。
  function reviewQuoteLabel(open) {
    return t(open ? "review:quote.collapse" : "review:quote.expand");
  }

  function renderReviewQuote(anchor, key, draft, label) {
    if (!anchor?.excerpt) return "";
    const excerpt = anchor.excerpt;
    const open = Boolean(draft.expandedQuotes?.has(key));
    return `<details class="review-quote" data-review-quote="${escapeHtml(key)}" ${open ? "open" : ""}><summary><span class="review-quote-label">${escapeHtml(label)}<span class="review-quote-toggle" data-label="${escapeHtml(reviewQuoteLabel(open))}" aria-hidden="true"></span></span><span class="review-quote-preview">${escapeHtml(excerpt.slice(0, 80))}${excerpt.length > 80 ? "…" : ""}</span></summary><blockquote>${escapeHtml(excerpt)}</blockquote></details>`;
  }

  function renderReviewThreadContent(data, version, draft) {
    const threads = reviewThreadsForVersion(data, version);
    const thread = threads.find((item) => item.id === draft.activeThread);
    if (draft.history) return `<section class="review-history" aria-label="${escapeHtml(t("review:rail.historyAria"))}">${threads.map((item) => `<div class="review-thread-row ${item.id === thread?.id ? "active" : ""}"><button data-open-thread="${escapeHtml(item.id)}"><strong>${escapeHtml(item.title || t("review:history.untitled"))}</strong><small>${item.scope === "document" ? escapeHtml(t("review:history.scopeDocument")) : escapeHtml(item.anchor?.heading || t("review:history.scopeLocal"))} · ${escapeHtml(reviewThreadStatus(item, data))} · ${escapeHtml(t("review:history.messageCount", { count: item.messages?.length || 0 }))}</small></button></div>`).join("") || `<p class="empty-note">${escapeHtml(t("review:history.empty"))}</p>`}</section>`;
    if (!thread) {
      const action = reviewSelectionAction(draft.input);
      const welcomeTitle = action?.label || (draft.input.anchor ? t("review:welcome.anchored") : t("review:welcome.generic"));
      const welcomeDetail = action?.description
        || `${draft.input.anchor ? t("review:welcome.anchoredDetail") : t("review:welcome.genericDetail")}${t("review:welcome.continuation")}`;
      const welcomeNotice = action?.notice || t("review:welcome.notice");
      return `<section class="review-welcome"><h3>${escapeHtml(welcomeTitle)}</h3><p>${escapeHtml(welcomeDetail)}</p><small>${escapeHtml(welcomeNotice)}</small></section>`;
    }
    const issue = data.issues.find((item) => item.id === thread.issue_id && item.version_id === version.id);
    const suggestions = (data.suggestions || []).filter((item) => item.thread_id === thread.id);
    const rendered = new Set();
    const planById = new Map((data.plans || []).map((item) => [item.id, item]));
    const latestReply = [...(thread.messages || [])].reverse().find((message) => ["assistant", "reviewer"].includes(message.author_type));
    const busy = threadJobBusy(thread.id) || state.reviewPending.has(`${getContext().current.chat.id}:${version.id}:${thread.id}`);
    const messages = (thread.messages || []).map((message, index) => {
      let value;
      try { value = JSON.parse(message.content); } catch { /* Ordinary Markdown message. */ }
      const turn = message.author_type === "assistant" && value?.type === "review_turn" && Object.keys(value).length === 3 && typeof value.answer === "string" && value.answer.trim() && (value.suggestion_id === null || typeof value.suggestion_id === "string" && suggestions.some((item) => item.id === value.suggestion_id));
      // New turns use only server-owned IDs; text matching is confined to legacy cards.
      const suggestion = message.author_type === "assistant" && value && suggestions.find((item) => !rendered.has(item.id) && (turn ? item.id === value.suggestion_id : !value.type && item.operation === value.operation && item.original_text === value.original_text && item.proposed_text === value.proposed_text));
      if (suggestion) rendered.add(suggestion.id);
      const grouped = message.author_type === "assistant" && message.plan_id ? planById.get(message.plan_id) : null;
      const groupedPlan = grouped && grouped.edits.length > 1 ? grouped : null;
      if (groupedPlan) groupedPlan.edits.forEach((item) => rendered.add(item.suggestion_id));
      // Static JS can update before the long-running Python backend restarts.
      const legacyAction = message === latestReply && !turn && !suggestion && !reviewThreadReadOnly(thread);
      const author = message.author_type === "human" ? t("review:message.human")
        : message.author_type === "assistant" ? t("review:message.assistant")
          : message.author_name || t("review:message.record");
      return `<li class="review-message" data-author="${escapeHtml(message.author_type)}"><header><strong title="${escapeHtml(message.author_name || author)}">${escapeHtml(author)}</strong><small>${message.role && message.author_type !== "human" ? escapeHtml(data.roles[message.role] || message.role) + " · " : ""}${message.created_at ? formatDate(message.created_at) : ""}</small></header>${index === 0 && thread.scope !== "document" ? renderReviewQuote(thread.anchor, thread.id, draft, t("review:selection.quoteLabel")) : ""}${turn ? `<div class="report">${renderMarkdown(value.answer)}</div>` : ""}${groupedPlan ? renderReviewPlan(groupedPlan, data) : suggestion ? renderReviewSuggestion(suggestion, data) : turn ? "" : `<div class="report">${renderMarkdown(message.content || "")}</div>`}${message.references?.length && (turn || !suggestion) ? `<details class="review-references"><summary>${escapeHtml(t("review:message.references"))}</summary>${message.references.map((ref) => `<p>${escapeHtml(typeof ref === "string" ? ref : [ref.title, ref.url, ref.excerpt || ref.content].filter(Boolean).join(" · "))}</p>`).join("")}</details>` : ""}${legacyAction ? `<div class="review-response-actions"><button class="review-text-button" id="review-form-suggestion" ${busy ? "disabled" : ""}>${escapeHtml(t("review:message.legacySuggestionAction"))}</button></div>` : ""}</li>`;
    }).join("");
    const issueStatusKey = {
      pending: "review:issue.statusPending",
      accepted: "review:issue.statusAccepted",
      rejected: "review:issue.statusRejected",
      needs_evidence: "review:issue.statusNeedsEvidence",
    }[issue?.status];
    const issueStatusLabel = issueStatusKey ? t(issueStatusKey) : issue?.status ?? "";
    const issueActions = [
      ["accepted", "review:issue.accept"],
      ["rejected", "review:issue.reject"],
      ["needs_evidence", "review:issue.requestEvidence"],
      ["noted", "review:issue.note"],
    ];
    return `<section class="review-thread-detail"><small>${escapeHtml(reviewThreadStatus(thread, data))}</small>${(state.reviewLocations.get(thread.id) || thread.projection_status) === "relink_required" ? `<p class="review-notice">${escapeHtml(t("review:relink.notice"))}</p><button class="button" data-relink-thread="${escapeHtml(thread.id)}">${escapeHtml(t("review:relink.action"))}</button>` : thread.scope !== "document" ? `<button class="review-text-button" data-locate-thread="${escapeHtml(thread.id)}">${escapeHtml(t("review:relink.locate"))}</button>` : ""}<ol class="review-messages" aria-label="${escapeHtml(t("review:rail.threadMessagesAria"))}">${messages}${suggestions.filter((item) => !rendered.has(item.id)).map((item) => `<li class="review-message">${renderReviewSuggestion(item, data)}</li>`).join("")}</ol>${threadJobBusy(thread.id) ? `<p class="review-reply-status" role="status">${escapeHtml(t("review:rail.replying"))}</p>` : ""}${issue ? `<section class="review-issue-decision"><small>${escapeHtml(t("review:issue.status", { status: issueStatusLabel }))}</small><label for="review-issue-note-${escapeHtml(issue.id)}">${escapeHtml(t("review:issue.noteLabel"))}${issue.severity === "high" ? escapeHtml(t("review:issue.noteRequired")) : ""}</label><textarea id="review-issue-note-${escapeHtml(issue.id)}" data-issue-note="${escapeHtml(issue.id)}" rows="2" maxlength="2000">${escapeHtml(state.reviewIssueDrafts.get(issue.id))}</textarea><div class="review-issue-actions">${issueActions.map(([status, key]) => `<button class="button" data-issue="${escapeHtml(issue.id)}" data-decision="${status}">${escapeHtml(t(key))}</button>`).join("")}</div></section>` : ""}</section>`;
  }

  function renderReviewThreadComposer(data, version, draft) {
    if (draft.history) return "";
    const thread = reviewThreadsForVersion(data, version).find((item) => item.id === draft.activeThread);
    const input = thread ? reviewThreadDraft(thread.id) : draft.input;
    const pending = state.reviewPending.has(`${getContext().current.chat.id}:${version.id}:${thread?.id || "new"}`);
    const busy = pending || (thread && threadJobBusy(thread.id));
    const job = thread && [...data.jobs].reverse().find((item) => item.thread_id === thread.id);
    const locked = reviewThreadReadOnly(thread);
    const selectionAction = thread ? null : reviewSelectionAction(input);
    const sendLabel = input.error ? t("review:composer.retrySend") : selectionAction?.button || t("review:composer.send");
    const busyStatus = busy
      ? `<p role="status">${escapeHtml(pending ? t("review:composer.sending") : t("review:composer.replying"))}</p>`
      : input.error ? `<p class="review-send-error" role="alert">${escapeHtml(t("review:composer.sendError", { detail: input.error }))}</p>`
        : job?.status === "failed" ? `<p class="review-send-error" role="alert">${escapeHtml(t("review:composer.jobFailed", { detail: job.error || t("review:composer.replyFailed") }))}</p>`
          : job?.phase === "plan" && job.meta?.plan_error ? `<p class="review-send-error" role="alert">${escapeHtml(t("review:plan.planError", { detail: planErrorText(job.meta.plan_error) }))}</p>` : "";
    const lockedNotice = locked ? `<p>${escapeHtml(t("review:rail.locked"))}</p>` : "";
    const pendingQuote = !thread && input.anchor
      ? `<div class="review-pending-quote">${renderReviewQuote(input.anchor, "new", draft, selectionAction ? `${selectionAction.label} · ${t("review:selection.selectedExcerpt")}` : t("review:selection.selectedExcerpt"))}<button id="review-clear-selection" class="review-clear-selection" type="button" aria-label="${escapeHtml(t("review:selection.clearAria"))}" title="${escapeHtml(t("review:selection.clearTitle"))}" ${pending ? "disabled" : ""}><span aria-hidden="true">×</span></button></div>` : "";
    const inputPlaceholder = selectionAction?.placeholder || (thread ? t("review:composer.placeholderThread") : t("review:composer.placeholderNew"));
    const sendButton = selectionAction
      ? escapeHtml(input.error ? t("review:composer.retry") : selectionAction.button)
      : '<svg aria-hidden="true" viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5m-6 6 6-6 6 6"/></svg>';
    return `<section class="review-thread-composer">${busyStatus}${lockedNotice}${locked ? "" : `<div class="review-compose-box">${pendingQuote}<div class="review-input-row${selectionAction ? " has-selection-action" : ""}"><label class="sr-only" for="review-thread-input">${escapeHtml(thread ? t("review:composer.inputLabelThread") : t("review:composer.inputLabelNew"))}</label><textarea id="review-thread-input" rows="2" maxlength="4000" placeholder="${escapeHtml(inputPlaceholder)}" aria-describedby="review-send-hint">${escapeHtml(input.message)}</textarea><button id="review-thread-send" class="button primary review-send" aria-label="${escapeHtml(sendLabel)}" title="${escapeHtml(`${sendLabel}${t("review:composer.shortcut")}`)}" ${busy || (!input.message.trim() && !selectionAction) ? "disabled" : ""}>${sendButton}</button></div></div><p id="review-send-hint" class="sr-only">${escapeHtml(t("review:composer.sendHint"))}</p>`}</section>`;
  }

  function resizeReviewInput() {
    const input = $("#review-thread-input");
    if (!input) return;
    input.style.height = "auto";
    input.style.height = `${input.scrollHeight || 0}px`;
  }

  function bindReviewThreads(data, version, draft) {
    $("#review-history").onclick = () => {
      if (!draft.history) draft.historyReturnScroll = $("#review-scroll")?.scrollTop || 0;
      draft.history = !draft.history;
      renderReviewWorkspace();
      $("#review-scroll").scrollTop = draft.history ? 0 : draft.historyReturnScroll || 0;
      if (!draft.history) $("#review-thread-input")?.focus();
    };
    $("#review-new").onclick = () => beginReviewThread("global");
    $("#review-clear-selection")?.addEventListener("click", clearReviewSelectionQuote);
    $$("[data-open-thread]").forEach((button) => button.onclick = () => openReviewThread(button.dataset.openThread));
    $$("[data-locate-thread]").forEach((button) => button.onclick = () => locateReviewThread(button.dataset.locateThread));
    const thread = data.threads?.find((item) => item.id === draft.activeThread);
    const busy = (thread && threadJobBusy(thread.id)) || state.reviewPending.has(`${getContext().current.chat.id}:${version.id}:${thread?.id || "new"}`);
    const selectionAction = thread ? null : reviewSelectionAction(draft.input);
    $("#review-thread-input")?.addEventListener("input", (event) => {
      (thread ? reviewThreadDraft(thread.id) : draft.input).message = event.target.value;
      $("#review-thread-send").disabled = busy || (!event.target.value.trim() && !selectionAction);
      resizeReviewInput();
    });
    resizeReviewInput();
    $("#review-thread-input")?.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && (event.metaKey || event.ctrlKey) && !event.isComposing && event.keyCode !== 229) {
        event.preventDefault();
        submitReviewThread("messages");
      }
    });
    $$('[data-review-quote]').forEach((details) => details.ontoggle = () => {
      draft.expandedQuotes ||= new Set();
      if (details.open) draft.expandedQuotes.add(details.dataset.reviewQuote);
      else draft.expandedQuotes.delete(details.dataset.reviewQuote);
      // C2：折叠开关的可见文案只存 data-label，CSS 用 attr() 消费。
      const toggle = $(".review-quote-toggle", details);
      if (toggle) toggle.dataset.label = reviewQuoteLabel(details.open);
    });
    $("#review-thread-send")?.addEventListener("click", () => submitReviewThread("messages"));
    $("#review-form-suggestion")?.addEventListener("click", () => submitReviewThread("suggestions"));
    $$("[data-relink-thread]").forEach((button) => {
      button.onmousedown = (event) => event.preventDefault();
      button.onclick = () => {
        const anchor = state.reviewSelection;
        if (!anchor) return toast(t("review:toast.selectFirst"));
        hideReviewSelection();
        return reviewAction(`/api/chats/${getContext().current.chat.id}/review/threads/${encodeURIComponent(button.dataset.relinkThread)}`, { method: "PATCH", body: { anchor, ...draftContext(version) } });
      };
    });
    $$("[data-draft-apply]").forEach((button) => button.onclick = () => mutateWorkingDraft("apply", button.dataset.draftApply));
    $$("[data-plan-apply]").forEach((button) => button.onclick = () => mutateWorkingDraft("apply_plan", button.dataset.planApply));
    $$("[data-plan-dismiss]").forEach((button) => button.onclick = () => reviewAction(`/api/chats/${getContext().current.chat.id}/review/plans/${encodeURIComponent(button.dataset.planDismiss)}`, { method: "PATCH", body: { status: "dismissed" } }));
    $$("[data-suggestion-dismiss]").forEach((button) => button.onclick = () => reviewAction(`/api/chats/${getContext().current.chat.id}/review/suggestions/${encodeURIComponent(button.dataset.suggestionDismiss)}`, { method: "PATCH", body: { status: "dismissed" } }));
    $("#draft-undo")?.addEventListener("click", () => mutateWorkingDraft("undo"));
    $("#draft-finalize")?.addEventListener("click", () => mutateWorkingDraft("finalize"));
    $("#draft-stop")?.addEventListener("click", stopRun);
  }

  function clearReportReviewHeader() {
    $("#report-review-actions").innerHTML = "";
    $("#report-version-control").innerHTML = "";
    $("#report-version-control").hidden = true;
    const status = $("#report-version-status");
    status.textContent = "";
    status.title = "";
    status.dataset.state = "";
    status.hidden = true;
  }

  function setPreviewDownload(label = t("report:preview.download"), detail = "") {
    const download = $("#preview-download");
    download.innerHTML = uiIcon("download");
    download.setAttribute("aria-label", label);
    download.dataset.tip = detail || label;
    download.title = detail;
  }

  function renderReportReviewHeader(data, version, draft) {
    const jobs = data.jobs.filter((item) => item.version_id === version.id);
    const jobBusy = jobs.some((job) => ["queued", "running"].includes(job.status));
    const running = getContext().activeRun && ["queued", "running"].includes(getContext().activeRun.status);
    const high = data.issues.some((issue) => issue.version_id === version.id && issue.severity === "high" && (["pending", "accepted", "needs_evidence"].includes(issue.status) || (issue.status === "rejected" && !issue.note?.trim())));
    const blocked = jobBusy || high || running || Boolean(data.draft?.change_count);
    const reason = jobBusy ? t("review:version.blockedJob") : high ? t("review:version.blockedHigh") : running ? t("review:version.blockedRunning") : data.draft?.change_count ? t("review:version.blockedDraft") : t("review:version.internalOnly");
    const candidates = data.candidates.filter((item) => item.version_id === version.id);
    const working = data.draft?.base_version_id === version.id;
    setPreviewDownload(
      working ? t("report:preview.downloadBase") : t("report:preview.download"),
      working ? t("report:preview.downloadBaseDetail", { number: version.number }) : "",
    );
    const versionControl = $("#report-version-control"), status = $("#report-version-status");
    versionControl.hidden = false;
    versionControl.innerHTML = `<select id="review-version" aria-label="${escapeHtml(t("review:version.selectAria"))}" aria-describedby="report-version-status" title="${escapeHtml(t("review:version.switchTitle"))}">${data.versions.map((item) => `<option value="${item.artifact_id}" ${item.id === version.id ? "selected" : ""}>v${item.number}</option>`).join("")}</select>${uiIcon("chevron")}`;
    status.hidden = false;
    status.textContent = working
      ? t("review:version.statusWorking", { number: version.number })
      : version.approval?.at ? t("review:version.statusApproved") : t("review:version.statusDraft");
    status.dataset.state = working ? "working" : version.approval?.at ? "approved" : "draft";
    status.title = working
      ? t("review:version.baseTitle", {
        number: version.number,
        status: version.approval?.at ? t("review:version.baseApproved") : t("review:version.baseDraft"),
      })
      : version.approval?.at
        ? t("review:version.approvalTitle", { name: version.approval.name || t("review:version.currentAccount") })
        : t("review:version.unsignedTitle");
    const knowledgeMenu = `<summary class="report-header-action">${escapeHtml(t("review:knowledge.menu"))}${uiIcon("chevron")}</summary><div class="report-more-menu"><button id="review-extract" class="button" ${jobBusy || candidates.length ? "disabled" : ""}>${escapeHtml(t("review:knowledge.generate"))}</button><button id="review-knowledge-open" class="button">${escapeHtml(t("review:knowledge.reviewOpen", { count: candidates.length }))}</button><small>${escapeHtml(t("review:knowledge.localOnly"))}</small></div>`;
    $("#report-review-actions").innerHTML = working ? "" : version.approval?.at
      ? `<details id="report-more">${knowledgeMenu}</details>`
      : `<button id="review-approve" class="report-header-action" aria-label="${escapeHtml(t("review:version.approveAria"))}" title="${escapeHtml(reason)}" data-tip="${escapeHtml(reason)}" ${blocked || state.reviewBusy ? "disabled" : ""}>${escapeHtml(t("review:version.approve"))}</button>`;
    const base = `/api/chats/${getContext().current.chat.id}/review`;
    $("#review-version").onchange = (event) => openArtifactPreview(event.target.value);
    $("#review-approve")?.addEventListener("click", () => {
      if (confirm(t("review:version.approveConfirm", { number: version.number }))) return reviewAction(`${base}/versions/${version.id}/approve`, { method: "POST" });
    });
    $("#review-extract")?.addEventListener("click", () => { draft.knowledge = true; return reviewAction(`${base}/jobs`, { method: "POST", body: { version_id: version.id, kind: "knowledge" } }); });
    $("#review-knowledge-open")?.addEventListener("click", () => { draft.knowledge = true; renderReviewWorkspace(); });
    const panel = $("#review-knowledge");
    panel.hidden = working || !draft.knowledge || !version.approval?.at;
    const candidateStatus = (item) => {
      const key = { approved: "review:knowledge.statusApproved", rejected: "review:knowledge.statusRejected", pending: "review:knowledge.statusPending" }[item.status];
      return key ? t(key) : t("review:knowledge.statusPending");
    };
    const candidateField = (key, value) => escapeHtml(t(key, { value: value || t("review:knowledge.unlabeled") }));
    panel.innerHTML = panel.hidden ? "" : `<header><h3>${escapeHtml(t("review:knowledge.panelTitle", { number: version.number }))}</h3><button id="review-knowledge-close" class="button">${escapeHtml(t("review:knowledge.close"))}</button></header><p>${escapeHtml(t("review:knowledge.intro"))}</p>${jobBusy ? `<p role="status">${escapeHtml(t("review:knowledge.generating"))}</p>` : ""}${jobs.filter((job) => job.kind === "knowledge" && job.status === "failed").slice(-1).map((job) => `<p role="alert">${escapeHtml(job.error)}</p>`).join("")}${candidates.map((item) => `<article class="review-candidate"><strong>${escapeHtml(item.title)}</strong><p>${escapeHtml(item.body)}</p><small>${escapeHtml(t("review:knowledge.source", { number: version.number }))}<br>${candidateField("review:knowledge.jurisdiction", item.jurisdiction)}<br>${candidateField("review:knowledge.scope", item.scope)}<br>${candidateField("review:knowledge.informationDate", item.information_date)}<br>${candidateField("review:knowledge.reviewDate", item.review_date)}<br>${escapeHtml(candidateStatus(item))}</small>${item.status === "pending" ? `<div class="review-issue-actions"><button class="button" data-candidate="${item.id}" data-decision="approved">${escapeHtml(t("review:knowledge.approve"))}</button><button class="button" data-candidate="${item.id}" data-decision="rejected">${escapeHtml(t("review:knowledge.reject"))}</button></div>` : ""}</article>`).join("")}`;
    $("#review-knowledge-close")?.addEventListener("click", () => { draft.knowledge = false; renderReviewWorkspace(); });
    $$("[data-candidate]").forEach((button) => button.onclick = () => reviewAction(`${base}/candidates/${button.dataset.candidate}`, { method: "PATCH", body: { status: button.dataset.decision } }));
  }

  function renderReviewWorkspace() {
    if (getContext().shareToken) { $("#review-rail").innerHTML = ""; return; }
    const data = state.reviewData;
    const version = reviewVersionForArtifact(data, reports.snapshot().artifactId);
    if (!data || !version) {
      $("#review-rail").innerHTML = `<p class="empty-note">${escapeHtml(t("review:rail.onlyFinalReport"))}</p>`;
      return;
    }
    const draft = reviewDraft(), working = data.draft;
    const thread = reviewThreadsForVersion(data, version).find((item) => item.id === draft.activeThread);
    const anchor = thread ? thread.anchor : draft.input.anchor;
    const context = (thread?.scope === "document" || !anchor)
      ? t("review:history.scopeDocument")
      : anchor.heading || t("review:selection.fallbackTitle");
    const running = getContext().activeRun && ["queued", "running"].includes(getContext().activeRun.status);
    const jobBusy = data.jobs.some((job) => job.version_id === version.id && ["queued", "running"].includes(job.status));
    data.issues.forEach((issue) => { if (!state.reviewIssueDrafts.has(issue.id)) state.reviewIssueDrafts.set(issue.id, issue.note || ""); });
    const rail = $("#review-rail"), focused = document.activeElement;
    const focusId = focused && rail.contains?.(focused) ? focused.id : null;
    const selection = focusId ? [focused.selectionStart, focused.selectionEnd] : null;
    const scrollNode = $("#review-scroll"), scrollTop = scrollNode?.scrollTop || 0;
    const followTail = !draft.history && scrollNode && scrollTop + scrollNode.clientHeight >= scrollNode.scrollHeight - 40;
    const historyControl = draft.history
      ? (thread ? t("review:rail.backToThread") : t("review:rail.backToInput"))
      : t("review:rail.history");
    const contextTitle = draft.history ? t("review:rail.historyAria") : context;
    const contextText = draft.history
      ? t("review:rail.historyContext", { count: reviewThreadsForVersion(data, version).length })
      : `${t("review:rail.scope", { context: context.slice(0, 36) })}${context.length > 36 ? "…" : ""}`;
    // 保存态存协议码，显示时才取当前语言（切语言后不得残留旧语言的保存提示）。
    const savedTime = working
      ? new Date(working.updated_at * 1000).toLocaleTimeString(getLocale(), { hour: "2-digit", minute: "2-digit" })
      : "";
    const saved = state.draftSaving
      ? t(DRAFT_SAVING_LABEL_KEYS[state.draftSaving])
      : working ? t("review:version.savedAt", { time: savedTime }) : "";
    const draftStatusText = running
      ? t("review:version.generatingShort")
      : state.draftSaving ? saved : t("review:version.savedChanges", { count: working?.change_count || 0 });
    rail.innerHTML = `<header class="review-assistant-head"><div><h2>${escapeHtml(t("review:rail.title"))}</h2><button class="review-text-button" id="review-history" aria-expanded="${draft.history}" aria-controls="review-scroll">${escapeHtml(historyControl)}</button><button class="review-text-button" id="review-new">${escapeHtml(t("review:rail.newThread"))}</button></div><p class="review-context" title="${escapeHtml(contextTitle)}">${escapeHtml(contextText)}</p></header><div id="review-scroll" class="review-scroll">${renderReviewThreadContent(data, version, draft)}</div>${renderReviewThreadComposer(data, version, draft)}${working || state.draftSaving ? `<footer class="working-draft-bar"><small class="working-draft-status" role="status" title="${escapeHtml(`${saved} · r${working?.revision || 0}`)}">${escapeHtml(draftStatusText)}</small>${running ? `<button id="draft-stop" class="button">${escapeHtml(t("review:version.stop"))}</button>` : `<button id="draft-undo" class="button" ${!working?.change_count || state.reviewBusy ? "disabled" : ""}>${escapeHtml(t("review:version.undo"))}</button><button id="draft-finalize" class="button primary" ${!working?.change_count || state.reviewBusy || jobBusy ? "disabled" : ""}>${escapeHtml(t("review:version.finalize"))}</button>`}</footer>` : ""}`;
    bindReviewThreads(data, version, draft);
    $("#review-scroll").scrollTop = followTail ? $("#review-scroll").scrollHeight : scrollTop;
    if (focusId && $(`#${CSS.escape(focusId)}`)) {
      const target = $(`#${CSS.escape(focusId)}`); target.focus();
      if (selection && target.setSelectionRange) target.setSelectionRange(...selection);
    }
    renderReportReviewHeader(data, version, draft);
    $$("[data-issue-note]").forEach((input) => input.oninput = (event) => state.reviewIssueDrafts.set(input.dataset.issueNote, event.target.value));
    $$("[data-issue]").forEach((button) => button.onclick = () => {
      const issue = data.issues.find((item) => item.id === button.dataset.issue), status = button.dataset.decision;
      const note = state.reviewIssueDrafts.get(issue.id) || "";
      if ((status === "noted" || (status === "rejected" && issue.severity === "high")) && !note.trim()) return toast(t("review:toast.noteRequired"));
      return reviewAction(`/api/chats/${getContext().current.chat.id}/review/issues/${issue.id}`, { method: "PATCH", body: { status, note } });
    });
  }

  async function mutateWorkingDraft(action, suggestionId) {
    if (state.reviewBusy || getContext().shareToken || !reports.snapshot().artifactId) return;
    const version = reviewVersionForArtifact(state.reviewData, reports.snapshot().artifactId), working = state.reviewData?.draft;
    const applies = action === "apply" || action === "apply_plan";
    if (!version || (!applies && !working)) return;
    if (applies && state.reviewData.versions.some((item) => item.number > version.number)) return toast(t("review:toast.historicalReadOnly"));
    const generation = reports.snapshot().generation, chatId = getContext().current.chat.id, artifactId = reports.snapshot().artifactId;
    const expected_revision = working?.revision || 0;
    const body = action === "apply" ? { version_id: version.id, suggestion_id: suggestionId, expected_revision }
      : action === "apply_plan" ? { version_id: version.id, plan_id: suggestionId, expected_revision }
        : { draft_id: working.id, expected_revision };
    state.reviewBusy = true; state.draftSaving = action === "finalize" ? "finalizing" : "saving";
    renderReviewWorkspace();
    try {
      const result = await api(`/api/chats/${chatId}/review/draft/${action}`, { method: "POST", body });
      if (generation !== reports.snapshot().generation || getContext().current?.chat.id !== chatId) return;
      state.draftSaving = "";
      if (action === "finalize") {
        await refreshAll(); await loadChat(chatId);
        if (generation === reports.snapshot().generation && getContext().current?.chat.id === chatId) {
          const finalized = getContext().current.artifacts.find((item) => item.run_id === result.id && /\.md$/i.test(item.name))?.id;
          if (finalized) carryReviewConversation(artifactId, finalized);
          await openArtifactPreview(finalized || artifactId);
        }
      } else {
        state.reviewData.draft = result.draft;
        renderPreviewReport(result.draft.content);
        await loadReviewWorkspace(generation);
      }
    } catch (err) {
      if (generation === reports.snapshot().generation) {
        state.draftSaving = "failed"; toast(err.message);
        await loadReviewWorkspace(generation);
      }
    } finally {
      state.reviewBusy = false;
      if (generation === reports.snapshot().generation && state.reviewData) renderReviewWorkspace();
      else state.draftSaving = "";
    }
  }

  async function reviewAction(url, options) {
    if (state.reviewBusy || getContext().shareToken) return;
    state.reviewBusy = true;
    const generation = reports.snapshot().generation;
    try { await api(url, options); await refreshAll(); await loadReviewWorkspace(generation); }
    catch (err) { toast(err.message); }
    finally { state.reviewBusy = false; if (generation === reports.snapshot().generation && state.reviewData) renderReviewWorkspace(); }
  }

  function resetSession() {
   state.reviewNavigation += 1;
   state.reviewLoadSequence += 1;
   state.reviewData = null;
   state.reviewLocations.clear();
   clearTimeout(state.reviewTimer);
   state.reviewTimer = null;
   hideReviewSelection();
   clearReportReviewHeader();
  }
  function dispose({forgetDrafts = false} = {}) {
   resetSession();
   if (forgetDrafts) {
     state.reviewDrafts.clear(); state.reviewIssueDrafts.clear(); state.reviewThreadDrafts.clear();
     state.reviewPending.clear(); state.reviewBusy = false;
   }
  }
  return {
   refresh: loadReviewWorkspace, render: renderReviewWorkspace, resetSession, dispose,
   begin: beginReviewThread, openThread: openReviewThread, submit: submitReviewThread,
   captureSelection: captureReviewAnchor, clearSelection: hideReviewSelection,
   highlight: applyReviewHighlights, resizeInput: resizeReviewInput, mutateDraft: mutateWorkingDraft,
   carry: carryReviewConversation, setDownload: setPreviewDownload,
   snapshot: () => {
     const view = state.reviewDrafts.get(reports.snapshot().artifactId);
     return {data: state.reviewData, navigation: state.reviewNavigation, selection: state.reviewSelection,
       view: view ? structuredClone(view) : null,
       threadInput: view?.activeThread ? structuredClone(reviewThreadDraft(view.activeThread)) : null};
   },
  };
}
