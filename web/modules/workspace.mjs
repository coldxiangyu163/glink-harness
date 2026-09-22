import {createConversationView} from './conversation-view.mjs';
import {reportDownloadMenuMarkup, bindDownloadMenus} from './artifacts.mjs';
import {createRunSubscriptions} from './run-state.mjs';
import {chatContextMenuMarkup,chatDeleteConfirmMarkup,contextMenuPoint} from './ui.mjs';
import { ApiError, apiErrorMessage, escapeHtml, formatBytes, formatDate, safeUrl, sourceFavicon, truncateChatTitle, uiIcon } from './ui.mjs';
import { branchChatFromMenu as branchChatFromMenuRequest, buildChatCreateBody, deleteChatFromMenu as deleteChatFromMenuRequest, handleComposerPaste as handleComposerPasteRequest, resetNewResearchScenario, runStartOutcome, scenarioBadgeLabel, scenarioRoutingNotice, selectedScenarioFromCatalog, storeArtifactInLibraryRequest as requestArtifactStorage, updateChatFromMenu as updateChatFromMenuRequest } from './requests.mjs';
import { renderMarkdown, reportCitationSources } from './markdown.mjs';
import { LIBRARY_SCOPES, filesFromDrag, isRunAttachment, libraryCount, libraryScopeItems as scopeItemsFor, oversizedFiles, parseBadgeMarkup, uploadLimitBytes as configuredUploadLimit, uploadRowsMarkup } from './uploads.mjs';
import { collectDraft, renderTaskRows, sourceHostname, sourceReadItems, sourceUrlKey, subagentSnapshotForRun, taskBoardPresentation, taskBoardProgressCopy, tasksForRun, terminalTypes } from './run-state.mjs';
import { artifactResolverFor, artifactTypeLabel, deliveryArtifactsForCard, groupArtifactsByRun, isPreviewableArtifact, primaryArtifactForGroups, renderArtifactGroups } from './artifacts.mjs';

import {createReportView} from './report-view.mjs';
import { applyStatic, createI18n, getLocale, localeSwitchFailure, setLocale, subscribe as subscribeLocale, t, LOCALE_STORAGE_KEY } from './i18n.mjs';

// 只增长流式草稿的事件：会话视图可以只更新草稿节点，不重建整段会话。
const DRAFT_ONLY_EVENT_TYPES = new Set(["assistant_delta"]);

export function createWorkspace(environment = globalThis) {
  const {document, window = environment, location, navigator, CSS, HTMLImageElement,
    setTimeout, clearTimeout, requestAnimationFrame,
    EventSource, FormData, confirm} = environment;

  const fetch = environment.fetch.bind(environment);
  // 服务端内容字段（场景/领域/档位名与摘要）由配置下发中英两份；
  // 界面按当前语言取，缺英文时回落原文。ID 与版本号是标识符，永不翻译。
  const contentField = (item, key) => {
    if (!item) return "";
    const english = getLocale() === "en";
    return (english && item[`${key}_en`]) || item[key] || "";
  };
  const subscriptions = createRunSubscriptions(EventSource);
  const lifetime = new (environment.AbortController || globalThis.AbortController)();
  let started = false, disposed = false;
  function listen(target, type, handler, options = {}) {
    target.addEventListener(type, handler, {...(typeof options === 'boolean' ? {capture: options} : options), signal: lifetime.signal});
  }

  const updateChatFromMenu = (id, body) => updateChatFromMenuRequest(id, body, api);

  const branchChatFromMenu = id => branchChatFromMenuRequest(id, api);

  const deleteChatFromMenu = id => deleteChatFromMenuRequest(id, api);

  const handleComposerPaste = event => handleComposerPasteRequest(event, {
    upload: (files, options) => uploadLibrary(files, {...options, origin: ATTACHMENT_ORIGIN}),
    notify: toast,
  });

  const APP_REVISION = "0.1.0";

  const $ = (selector, root = document) => root.querySelector(selector);

  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

  // Storage can be unavailable (private mode, disabled); preferences then apply for this session only.
  // Both the read and the write must guard the property access itself, not just the call.
  const readStoredValue = (key) => {
    try { return environment.localStorage?.getItem(key) ?? null; } catch { return null; }
  };

  const writeStoredValue = (key, value) => {
    try { environment.localStorage?.setItem(key, value); } catch { /* keep the in-session value */ }
  };

  const state = {
    user: null,
    config: null,
    projects: [],
    chats: [],
    archived: [],
    library: [],
    scenarioCatalog: { domains: [], scenarios: [] },
    tierCatalog: { default_tier_id: "deep-solve", tiers: [] },
    tiersAvailable: false,
    currentScenario: null,
    currentTier: "deep-solve",
    currentDomain: "general",
    current: null,
    selectedProject: null,
    selectedLibrary: new Set(),
    libraryScope: "library",

    activeRun: null,
    submitting: false,
    backgroundRuns: new Map(),
    stoppingRunId: null,
    authMode: "login",
    toastTimer: null,
    searchTimer: null,

    reviewResearchRuns: new Set(),

    shareToken: null,
    sharedBundle: null,
    sidebarCollapsed: readStoredValue("glink.sidebar.collapsed") === "1",
    chatMenuTrigger: null,
  };

  const composer = $("#composer");

  let localeUnsubscribe = null;

  const promptInput = $("#prompt-input");

  const sendButton = $("#send-button");

  const modeBadge = $("#mode-badge");

  function renderTopbarIcons() {
    $("#sources-button").insertAdjacentHTML("afterbegin", uiIcon("source"));
    $("#share-button").innerHTML = uiIcon("share");
  }

  async function api(path, options = {}, translator = t) {
    const config = { credentials: "same-origin", signal: lifetime.signal, ...options };
    if (config.body && !(config.body instanceof FormData)) {
      config.headers = { "Content-Type": "application/json", ...(config.headers || {}) };
      if (typeof config.body !== "string") config.body = JSON.stringify(config.body);
    }
    let response;
    try {
      response = await fetch(path, config);
    } catch {
      // 网络中断/abort 的浏览器原生消息不得进入 UI：统一为已本地化的 ApiError。
      throw new ApiError(translator('errors:connect'), 0);
    }
    const type = response.headers.get("content-type") || "";
    let payload = "";
    let decoded = true;
    try {
      payload = type.includes("application/json") ? await response.json() : await response.text();
    } catch {
      payload = "";
      decoded = false;
    }
    if (!response.ok) {
      const credentialRequest = ["/api/auth/login", "/api/auth/register"].includes(path);
      const protectedRequest = path.startsWith("/api/") && !path.startsWith("/api/public/");
      if (response.status === 401 && protectedRequest && !credentialRequest) showAuth();
      throw new ApiError(apiErrorMessage(payload, response.status, translator), response.status);
    }
    // 成功状态但 body 无法解析（200 + malformed）：把它当空 payload 交给调用方会继续抛出
    // 原生 TypeError（例如分享页 bundle.messages.filter）；这里用传入 translator 抛受控错误。
    if (!decoded) throw new ApiError(translator('errors:dataLoad'), response.status);
    return payload;
  }

  function toast(message, action = null) {
    if (disposed) return;
    const node = $("#toast");
    node.replaceChildren(document.createTextNode(message));
    if (action) {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = action.label;
      button.addEventListener("click", () => {
        node.hidden = true;
        action.run();
      });
      node.appendChild(button);
    }
    node.hidden = false;
    clearTimeout(state.toastTimer);
    state.toastTimer = setTimeout(() => { node.hidden = true; }, 4200);
  }

  function showAuth() {
    conversation.hide();
    state.user = null;
    $("#app-shell").hidden = true;
    $("#auth-screen").hidden = false;
    composer.hidden = true;
    setAuthMode("login");
  }

  function updateAuthCopy() {
    const register = state.authMode === "register";
    $("#auth-title").textContent = t(register ? "auth:title.register" : "auth:title.login");
    $("#auth-subtitle").textContent = t(register ? "auth:subtitle.register" : "auth:subtitle.login");
    $("#auth-submit").textContent = t(register ? "auth:submit.register" : "auth:submit.login");
    $("#auth-switch").textContent = t(register ? "auth:switchMode.toLogin" : "auth:switchMode.toRegister");
  }

  function setAuthMode(mode) {
    state.authMode = mode;
    const register = mode === "register";
    updateAuthCopy();
    $("#auth-password").autocomplete = register ? "new-password" : "current-password";
    $("#auth-invite-row").hidden = !register;
    $("#auth-invite").required = register;
    $("#auth-error").textContent = "";
  }

  async function authSubmit(event) {
    event.preventDefault();
    const error = $("#auth-error");
    error.textContent = "";
    try {
      const payload = {
        username: $("#auth-username").value.trim(),
        password: $("#auth-password").value,
      };
      if (state.authMode === "register") {
        payload.invite = $("#auth-invite").value.trim();
      }
      const result = await api(`/api/auth/${state.authMode}`, { method: "POST", body: payload });
      state.user = result.user;
      await enterApp();
    } catch (err) {
      error.textContent = err.message;
    }
  }

  async function enterApp() {
    state.shareToken = null;
    state.sharedBundle = null;
    $("#auth-screen").hidden = true;
    $("#app-shell").hidden = false;
    $("#app-shell").classList.remove("shared-mode");
    $("#sidebar").hidden = false;
    $(".topbar").hidden = false;
    composer.hidden = false;
    $("#user-name").textContent = state.user.username;
    $("#user-avatar").textContent = state.user.username.slice(0, 1).toUpperCase();
    await refreshAll();
    route();
  }

  async function refreshAll() {
    const results = await Promise.allSettled([
      api("/api/config"), api("/api/projects"), api("/api/chats"),
      api("/api/chats?archived=true"), api("/api/library"), api("/api/scenarios"),
      api("/api/tiers"),
    ]);
    const [configResult, projectsResult, chatsResult, archivedResult, libraryResult, scenariosResult, tiersResult] = results;
    const valueOr = (result, fallback) => result.status === "fulfilled" ? result.value : fallback;
    const config = valueOr(configResult, {});
    const projects = valueOr(projectsResult, []);
    const chats = valueOr(chatsResult, []);
    const archived = valueOr(archivedResult, []);
    const library = valueOr(libraryResult, []);
    const scenarios = valueOr(scenariosResult, { domains: [], scenarios: [] });
    const tiers = valueOr(tiersResult, { default_tier_id: "deep-solve", tiers: [] });

    state.config = config;
    state.projects = projects;
    state.chats = chats;
    state.archived = archived;
    state.library = library;
    state.scenarioCatalog = scenarios;
    state.tierCatalog = tiers;
    state.tiersAvailable = tiersResult.status === "fulfilled" && Array.isArray(tiers.tiers) && tiers.tiers.length > 0;
    if (state.currentScenario && !scenarios.scenarios.some((item) => item.id === state.currentScenario)) {
      state.currentScenario = null;
    }
    if (state.tiersAvailable && !tiers.tiers.some((item) => item.id === state.currentTier)) {
      state.currentTier = tiers.default_tier_id;
    }
    $("#model-name").textContent = config.model || t('workspace:user.modelUnset');
    $("#project-count").textContent = projects.length;
    $("#library-count").textContent = libraryCount(library);
    $("#active-count").textContent = chats.length;
    $("#archive-count").textContent = archived.length;
    renderNavigation();
    renderScenarioPicker();
    renderTierPicker();
    applySidebarState();
    scheduleParseRefresh();

    const notices = [];
    if (results.slice(0, 6).some((result) => result.status === "rejected")) {
      notices.push(t('workspace:toast.partialLoad'));
    }
    if (configResult.status === "fulfilled" && config.app_revision !== APP_REVISION) {
      notices.push(t('workspace:toast.restartRequired'));
    }
    if (notices.length) toast(notices.join(t('common:listSeparator')));
  }

  function renderNavigation() {
    closeChatContextMenu();
    closeChatDeleteConfirm();
    $("#project-nav").innerHTML = state.projects.map((project) => `
      <a class="nav-row" href="#project/${project.id}" data-project-id="${project.id}">
        <span class="nav-icon">◇</span><span>${escapeHtml(project.name)}</span><em>${project.chat_count}</em>
      </a>`).join("") || `<p class="empty-note">${t('workspace:project.emptyList')}</p>`;
    $("#chat-nav").innerHTML = state.chats.slice(0, 30).map((chat) => `
      <div class="nav-chat-row" data-chat-row-id="${chat.id}">
        <a class="nav-row nav-chat" href="#chat/${chat.id}" data-chat-id="${chat.id}" title="${escapeHtml(chat.title)}">
          <span class="nav-icon">${chat.mode === "agent_team" ? "⌘" : "○"}</span>
          <span class="chat-title">${escapeHtml(chat.title.length > 17 ? chat.title.slice(0, 17) : chat.title)}</span>
          <i class="status-mini ${chat.status}"></i>
        </a>
        <button class="chat-more" type="button" aria-label="${t('workspace:chatMenu.moreActions', { title: escapeHtml(chat.title) })}" aria-haspopup="menu" aria-expanded="false" data-chat-more="${chat.id}">⋮</button>
      </div>`).join("") || `<p class="empty-note">${t('workspace:chat.emptyList')}</p>`;
    $$('[data-chat-row-id]').forEach((row) => {
      row.addEventListener("contextmenu", (event) => {
        const point = contextMenuPoint(event);
        openChatContextMenu(row.dataset.chatRowId, point.x, point.y);
      });
    });
    $$('[data-chat-more]').forEach((button) => button.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      const rect = button.getBoundingClientRect();
      openChatContextMenu(button.dataset.chatMore, rect.right + 4, rect.top, button);
    }));
    renderBackgroundRun();
    updateNavigationActive();
  }

  function closeChatContextMenu() {
    $("#chat-context-menu")?.remove();
    if (state.chatMenuTrigger) state.chatMenuTrigger.setAttribute("aria-expanded", "false");
    state.chatMenuTrigger = null;
  }

  function closeChatDeleteConfirm() {
    $("#chat-delete-confirm")?.remove();
  }

  function openChatDeleteConfirm(chatId) {
    const chat = state.chats.find((item) => item.id === chatId);
    if (!chat) return;
    prepareTransientSurface("delete-confirm");
    closeChatDeleteConfirm();
    const holder = document.createElement("div");
    holder.innerHTML = chatDeleteConfirmMarkup(chat);
    const confirm = holder.firstElementChild;
    document.body.appendChild(confirm);
    const row = $$('[data-chat-row-id]').find((item) => item.dataset.chatRowId === chatId);
    const anchor = row?.getBoundingClientRect();
    const bounds = confirm.getBoundingClientRect();
    const preferredLeft = anchor ? anchor.right + 8 : (window.innerWidth - bounds.width) / 2;
    const preferredTop = anchor ? anchor.top : (window.innerHeight - bounds.height) / 2;
    confirm.style.left = `${Math.max(8, Math.min(preferredLeft, window.innerWidth - bounds.width - 8))}px`;
    confirm.style.top = `${Math.max(8, Math.min(preferredTop, window.innerHeight - bounds.height - 8))}px`;
    $("[data-cancel-chat-delete]", confirm).addEventListener("click", closeChatDeleteConfirm);
    $("[data-confirm-chat-delete]", confirm).addEventListener("click", async (event) => {
      event.currentTarget.disabled = true;
      await deleteChat(chat.id, event.currentTarget);
    });
    $("[data-cancel-chat-delete]", confirm).focus({ preventScroll: true });
  }

  function openChatContextMenu(chatId, x, y, trigger = null) {
    const chat = state.chats.find((item) => item.id === chatId);
    if (!chat) return;
    prepareTransientSurface("chat-menu");
    closeChatContextMenu();
    const menu = document.createElement("div");
    menu.id = "chat-context-menu";
    menu.className = "chat-context-menu";
    menu.setAttribute("role", "menu");
    menu.setAttribute("aria-label", t('workspace:chatMenu.menuAria', { title: chat.title }));
    menu.dataset.chatMenuId = chat.id;
    menu.innerHTML = chatContextMenuMarkup(chat, state.projects);
    document.body.appendChild(menu);
    const bounds = menu.getBoundingClientRect();
    const left = Math.max(8, Math.min(x, window.innerWidth - bounds.width - 8));
    const top = Math.max(8, Math.min(y, window.innerHeight - bounds.height - 8));
    menu.style.left = `${left}px`;
    menu.style.top = `${top}px`;
    const roomRight = window.innerWidth - left - bounds.width;
    if (roomRight < 224) menu.classList.add(left >= 224 ? "submenu-left" : "submenu-stack");
    state.chatMenuTrigger = trigger;
    trigger?.setAttribute("aria-expanded", "true");

    menu.addEventListener("click", async (event) => {
      const button = event.target.closest("[data-chat-action]");
      if (!button || button.disabled) return;
      const action = button.dataset.chatAction;
      if (action === "projects") {
        const expanded = button.getAttribute("aria-expanded") === "true";
        button.setAttribute("aria-expanded", String(!expanded));
        button.closest(".chat-menu-parent").classList.toggle("submenu-open", !expanded);
        return;
      }
      if (action === "rename") {
        startChatRename(chat.id);
        return;
      }
      closeChatContextMenu();
      if (action === "copy") await copyChat(chat.id);
      else if (action === "move") await moveChatToProject(chat.id, button.dataset.projectId);
      else if (action === "move-out") await moveChatToProject(chat.id, null);
      else if (action === "archive") await archiveChat(chat.id);
      else if (action === "delete") openChatDeleteConfirm(chat.id);
    });
    menu.querySelector("button:not([disabled])")?.focus({ preventScroll: true });
  }

  function startChatRename(chatId) {
    closeChatContextMenu();
    const chat = state.chats.find((item) => item.id === chatId);
    const row = $$('[data-chat-row-id]').find((item) => item.dataset.chatRowId === chatId);
    if (!chat || !row) return;
    const input = document.createElement("input");
    input.className = "chat-rename-input";
    input.value = chat.title;
    input.maxLength = 160;
    input.setAttribute("aria-label", t('workspace:chatMenu.renameAria'));
    row.classList.add("renaming");
    row.appendChild(input);
    const cancel = () => {
      input.remove();
      row.classList.remove("renaming");
    };
    const submit = async () => {
      const title = input.value.trim();
      if (!title) { toast(t('workspace:chatMenu.titleRequired')); input.focus(); return; }
      if (title === chat.title) { cancel(); return; }
      input.disabled = true;
      try {
        const saved = await updateChatFromMenu(chat.id, { title });
        if (state.current?.chat.id === chat.id) {
          state.current.chat.title = saved.title;
          $("#page-title").textContent = saved.title;
        }
        await refreshAll();
      } catch (err) {
        input.disabled = false;
        toast(err.message);
        input.focus();
      }
    };
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") { event.preventDefault(); submit(); }
      else if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); cancel(); }
    });
    input.addEventListener("blur", () => { if (!input.disabled) cancel(); });
    input.focus();
    input.select();
  }

  async function moveChatToProject(chatId, projectId) {
    try {
      const saved = await updateChatFromMenu(chatId, { project_id: projectId });
      if (saved.project_id !== projectId) throw new Error(projectId === null ? t('workspace:toast.moveNotRemoved') : t('workspace:toast.moveNotApplied'));
      if (state.current?.chat.id === chatId) {
        state.current.chat.project_id = projectId;
        state.selectedProject = projectId;
      }
      await refreshAll();
      if (location.hash.startsWith("#project/")) renderProject(location.hash.split("/")[1]);
      const project = state.projects.find((item) => item.id === projectId);
      toast(project ? t('workspace:toast.moveDone', { name: project.name }) : t('workspace:toast.moveOutDone'));
    } catch (err) { toast(err.message); }
  }

  function updateNavigationActive() {
    const hash = location.hash || "#home";
    $$(".nav-row").forEach((item) => {
      const active = item.getAttribute("href") === hash;
      item.classList.toggle("active", active);
      if (active) item.setAttribute("aria-current", "page");
      else item.removeAttribute("aria-current");
    });
  }

  function applySidebarState() {
    const shell = $("#app-shell");
    const collapsed = state.sidebarCollapsed && window.matchMedia("(min-width: 901px)").matches;
    shell.classList.toggle("sidebar-collapsed", collapsed);
    const button = $("#sidebar-collapse");
    button.setAttribute("aria-expanded", String(!collapsed));
    button.setAttribute("aria-label", collapsed ? t('workspace:nav.expandSidebar') : t('workspace:nav.collapseSidebar'));
    button.textContent = collapsed ? "⇥" : "⇤";
  }

  function toggleSidebarCollapse() {
    state.sidebarCollapsed = !state.sidebarCollapsed;
    writeStoredValue("glink.sidebar.collapsed", state.sidebarCollapsed ? "1" : "0");
    applySidebarState();
  }

  /**
   * 统一语言入口：每个意图（含点回当前语言）都进入 setLocale 的 ticket 机制，由它取消在途请求；
   * 只有成功提交后才持久化；失败保留原语言并给出已提交语言的可读提示。
   */
  function requestLocale(locale, {persist = false} = {}) {
    return setLocale(locale)
      .then((committed) => {
        if (persist && committed === locale) writeStoredValue(LOCALE_STORAGE_KEY, committed);
        // 同值短路不会通知订阅者，这里仍按当前视图重申文档语言。
        syncDocumentLanguage();
        return committed;
      })
      .catch(() => {
        toast(localeSwitchFailure(getLocale()));
        syncDocumentLanguage();
        return getLocale();
      });
  }

  function renderLocaleSwitch() {
    $$("[data-locale]").forEach((button) => {
      const active = button.dataset.locale === getLocale();
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", String(active));
    });
  }

  /**
   * 分享页 chrome 的语言 = 内容/报告语言，与访问者的 UI locale 完全解耦：
   * 独立实例（createI18n，1a 已验证隔离）不读 localStorage/navigator，也不改默认实例；
   * 报告语言当前固定中文，所以先固定为源语言 zh-CN。
   * 第 4 期衔接（D8/run.report_language，本期不引入该字段）：落地后把下面的 locale 换成
   * 该 run 的报告语言（必要时 await sharedI18n.setLocale(language)，目录加载器已就绪），
   * 分享页即自动跟随内容语言，本架构不需要再改。
   */
  const SHARED_CONTENT_LOCALE = 'zh-CN';
  const sharedI18n = createI18n({ locale: SHARED_CONTENT_LOCALE });
  const sharedT = (key, params) => sharedI18n.t(key, params);

  /**
   * 文档语言声明（<html lang>）由视图层唯一决定，i18n 实例只负责消息与 locale 状态：
   * 分享页是内容语言，其它视图是访问者的全局 UI locale。成功、失败与同值短路都要重申。
   */
  function syncDocumentLanguage() {
    const locale = location.pathname.startsWith("/share/") ? sharedI18n.getLocale() : getLocale();
    document.documentElement?.setAttribute?.('lang', locale);
  }

  /**
   * skip-link 属于文档级 chrome：它可见时的文案必须与 <html lang> 一致，所以只在视图层
   * 完成静态标注投影与 syncDocumentLanguage() 之后揭示；module/init 失败时不揭示。
   */
  function revealSkipLink() {
    document.querySelector('.skip-link')?.removeAttribute('hidden');
  }

  /** 语言重绘：按当前视图状态重投影 chrome，不触碰业务状态（进入新研究只走 renderHome）。 */
  function rerenderForLocale() {
    // 文档语言声明先按当前视图同步（分享页跟随内容语言，其它跟随访问者）。
    syncDocumentLanguage();
    // 分享页与访问者 UI locale 解耦：chrome 由内容语言实例渲染，不参与全局重投影，
    // 也不再因访问者切语言而重建 DOM（焦点保留问题因此不存在）。
    if (location.pathname.startsWith("/share/")) return;
    applyStatic(document);
    updateAuthCopy();
    renderLocaleSwitch();
    if (!state.user) return;
    renderBackgroundRun();
    applySidebarState();
    // 1b：抽屉与预览是当前视图状态的投影，与 hash 无关；切语言后按同一状态重绘。
    const detailDrawer = $("#detail-drawer");
    if (!detailDrawer.hidden) {
      if (detailDrawer.dataset.taskRunId) openTasks(detailDrawer.dataset.taskRunId);
      else if (detailDrawer.dataset.drawerKind === "sources") renderSourcesDrawer();
      else if (detailDrawer.dataset.drawerKind === "attachments") openAttachmentDrawer();
    }
    if (reports.snapshot().artifactId && review.snapshot().data) renderReviewWorkspace();
    const hash = location.hash || "#home";
    if (state.current && hash.startsWith("#chat/")) {
      const chat = state.current.chat;
      $("#page-kicker").textContent = chat.mode === "agent_team" ? "AGENT TEAM" : "REACT";
      $("#page-title").textContent = chat.title;
      renderTierPicker();
      renderChatScenarioBadge(chat);
      updateRunBanner();
      // 1b：按当前 bundle 无副作用重绘会话（研究记录/task rail/消息操作/产物卡），
      // 不经过 showChat/loadChat，不清 composer，也不发起任何请求。
      scheduleConversation();
      promptInput.placeholder = t('workspace:composer.followUpPlaceholder');
      updateComposer();
      return;
    }
    if (hash.startsWith("#library")) renderLibrary();
    else if (hash.startsWith("#project/")) renderProject(hash.split("/")[1]);
    else if (hash.startsWith("#projects")) renderProjects();
    else if (hash.startsWith("#archive")) renderArchive();
    else rerenderHomeForLocale();
    updateComposer();
  }

  /** home 视图的本地化投影：保留 currentScenario / currentTier / selectedLibrary / 输入与焦点。 */
  function rerenderHomeForLocale() {
    $("#page-kicker").textContent = t('workspace:page.kickerResearch');
    $("#page-title").textContent = state.selectedProject
      ? state.projects.find((x) => x.id === state.selectedProject)?.name || t('workspace:page.titleNewResearch')
      : t('workspace:page.titleNewResearch');
    toggleChatActions(false);
    renderScenarioPicker();
    // 档位名与副标也是服务端内容字段：切语言后必须一并重投影，否则首页仍显示旧语言。
    renderTierPicker();
    applyScenario(state.currentScenario);
    updateRunBanner();
  }

  function renderBackgroundRun() {
    const entry = $("#background-run");
    const backgrounds = [...state.backgroundRuns.values()];
    const background = backgrounds.at(-1);
    entry.hidden = !background;
    if (!background) return;
    $("#background-run-label").textContent = t('workspace:backgroundRun.count', { count: backgrounds.length });
    entry.href = `#chat/${background.chatId}`;
    entry.title = backgrounds.length === 1
      ? background.title
      : t('workspace:backgroundRun.title', { title: background.title, count: backgrounds.length });
  }

  function updateChatStatus(chatId, status) {
    const chat = state.chats.find((item) => item.id === chatId);
    if (!chat || chat.status === status) return;
    chat.status = status;
    renderNavigation();
  }

  /** Dropping files on the composer itself must upload them, not just the drawer. */
  function bindComposerDropTarget() {
    const target = composer;
    if (!target) return;
    const carriesFiles = (event) => filesFromDrag(event).length > 0 || [...(event?.dataTransfer?.types || [])].includes("Files");
    listen(target, "dragenter", (event) => {
      if (!carriesFiles(event)) return;
      event.preventDefault();
      target.dataset.dropLabel = t('workspace:composer.dropHint');
      target.classList.add("drag-over");
    });
    listen(target, "dragover", (event) => {
      if (!carriesFiles(event)) return;
      event.preventDefault();
      target.dataset.dropLabel = t('workspace:composer.dropHint');
      target.classList.add("drag-over");
    });
    listen(target, "dragleave", (event) => {
      if (event.target !== target) return;
      target.classList.remove("drag-over");
    });
    listen(target, "drop", async (event) => {
      const files = filesFromDrag(event);
      target.classList.remove("drag-over");
      // Plain text drops stay native; the textarea keeps its own behaviour.
      if (!files.length) return;
      event.preventDefault();
      const uploaded = await uploadLibrary(files, { selectUploaded: true, origin: ATTACHMENT_ORIGIN });
      const failed = files.length - uploaded.length;
      if (uploaded.length) toast(failed
        ? t('workspace:toast.attachmentsAddedFailed', { count: uploaded.length, failed })
        : t('workspace:toast.attachmentsAdded', { count: uploaded.length }));
    });
  }

  /** A pending scan is the one library state a user must watch change; poll while it runs. */
  let parseRefreshTimer = null;
  function scheduleParseRefresh() {
    if (disposed) return;
    const pending = state.library.some((item) => item.parse?.status === "pending");
    if (!pending) {
      if (parseRefreshTimer) { clearTimeout(parseRefreshTimer); parseRefreshTimer = null; }
      return;
    }
    if (parseRefreshTimer) return;
    parseRefreshTimer = setTimeout(async () => {
      parseRefreshTimer = null;
      if (disposed) return;
      try { await refreshAll(); } catch { /* a failed poll must not break the view */ }
      if (disposed) return;
      if (location.hash === "#library") renderLibrary();
      renderAttachmentChips();
      // The open drawer updates in place: rebuilding it would drop an in-flight drag.
      $$("[data-attachment-parse]").forEach((node) => {
        const item = state.library.find((candidate) => candidate.id === node.dataset.attachmentParse);
        if (item) node.innerHTML = parseBadge(item);
      });
      scheduleParseRefresh();
    }, 4000);
  }

  function showView(id) {
    $$(".view").forEach((view) => { view.hidden = view.id !== id; });
    if (id !== "chat-view") conversation.hide();
    updateNavigationActive();
    closeSidebar();
  }

  function moveComposer(slot) {
    const target = $(slot);
    if (target && composer.parentElement !== target) target.appendChild(composer);
  }

  async function route() {
    if (reports.snapshot().artifactId) closeArtifactPreview();
    if (location.pathname.startsWith("/share/")) {
      await loadShared(location.pathname.split("/").pop());
      return;
    }
    if (!state.user) return;
    const raw = location.hash.replace(/^#/, "") || "home";
    const [name, id] = raw.split("/");
    if (name === "chat" && id) await loadChat(id);
    else if (name === "library") renderLibrary();
    else if (name === "projects") renderProjects();
    else if (name === "project" && id) renderProject(id);
    else if (name === "archive") renderArchive();
    else renderHome();
  }

  function enterNewResearch() {
    resetNewResearchScenario(state);
    if (location.hash === "#home" || !location.hash) renderHome();
    else location.hash = "#home";
  }

  function renderHome() {
    state.current = null;
    state.activeRun = null;
    resetNewResearchScenario(state);
    showView("home-view");
    moveComposer("#home-composer-slot");
    promptInput.rows = 4;
    $("#page-kicker").textContent = t('workspace:page.kickerResearch');
    $("#page-title").textContent = state.selectedProject
      ? state.projects.find((x) => x.id === state.selectedProject)?.name || t('workspace:page.titleNewResearch')
      : t('workspace:page.titleNewResearch');
    toggleChatActions(false);
    renderScenarioPicker();
    applyScenario(state.currentScenario);
    updateRunBanner();
  }

  function selectedScenario() {
    return selectedScenarioFromCatalog(
      state.scenarioCatalog.scenarios,
      state.currentScenario,
    );
  }

  function renderScenarioPicker() {
    const domains = state.scenarioCatalog.domains || [];
    const scenarios = state.scenarioCatalog.scenarios || [];
    if (!scenarios.length) {
      state.currentScenario = null;
      $("#scenario-count").textContent = t('common:count.scenarios', { count: 0 });
      $("#domain-tabs").replaceChildren();
      $("#scenario-list").innerHTML = `<p class="empty-note">${t('workspace:scenario.unavailable')}</p>`;
      return;
    }
    const active = scenarios.find((item) => item.id === state.currentScenario) || null;
    if (!domains.some((domain) => domain.id === state.currentDomain)) {
      state.currentDomain = active?.domain_id || domains[0]?.id;
    }
    $("#scenario-count").textContent = t('common:count.scenarios', { count: scenarios.length });
    $("#domain-tabs").innerHTML = domains.map((domain) => `
      <button class="domain-tab" role="tab" aria-selected="${domain.id === state.currentDomain}" data-domain="${domain.id}">
        ${escapeHtml(domain.icon)} ${escapeHtml(contentField(domain, "name"))}
      </button>`).join("");
    const visible = scenarios.filter((item) => item.domain_id === state.currentDomain);
    $("#scenario-list").innerHTML = visible.map((item, index) => `
      <button class="scenario-card" aria-pressed="${item.id === state.currentScenario}" data-scenario="${item.id}">
        <span class="scenario-index">${String(index + 1).padStart(2, "0")} · ${t('workspace:scenario.defaultWorkflow', { workflow: item.workflow === "agent_team" ? "TEAM" : "REACT" })}</span>
        <strong>${escapeHtml(contentField(item, "name"))}</strong><small>${escapeHtml(contentField(item, "summary"))}</small>
        ${item.id === state.currentScenario ? `<span class="scenario-meta"><span>${t('workspace:scenario.defaultTopology', { label: escapeHtml(contentField(item, "workflow_label")) })}</span><span>${t('common:count.roles', { count: item.roles.length })}</span><span>${item.requires_files ? t('workspace:scenario.requiresFiles') : t('workspace:scenario.filesOptional')}</span><span>${escapeHtml(contentField(item, "estimated_minutes"))}</span></span>` : ""}
      </button>`).join("");
    $$('[data-domain]').forEach((button) => button.onclick = () => {
      state.currentDomain = button.dataset.domain;
      if (!scenarios.some((item) => item.id === state.currentScenario && item.domain_id === state.currentDomain)) {
        state.currentScenario = null;
      }
      renderScenarioPicker();
      applyScenario(state.currentScenario);
    });
    $$('[data-scenario]').forEach((button) => button.onclick = () => {
      const next = button.dataset.scenario === state.currentScenario ? null : button.dataset.scenario;
      applyScenario(next);
      renderScenarioPicker();
      promptInput.focus();
    });
  }

  function workflowBadge(workflow) {
    return workflow === "agent_team" ? t('workspace:workflow.agentTeam') : t('workspace:workflow.react');
  }

  function ratingDots(value) {
    return `${"●".repeat(Number(value || 0))}${"○".repeat(5 - Number(value || 0))}`;
  }

  function selectedTier() {
    return state.tierCatalog.tiers.find((item) => item.id === state.currentTier)
      || state.tierCatalog.tiers.find((item) => item.id === state.tierCatalog.default_tier_id);
  }

  function renderTierPicker() {
    const trigger = $("#tier-trigger");
    const menu = $("#tier-menu");
    const tier = selectedTier();
    if (!state.tiersAvailable || !tier) {
      $("#tier-current-name").textContent = t('workspace:tier.unavailable');
      $("#tier-current-version").textContent = "";
      trigger.title = t('workspace:tier.serverDefaultTitle');
      trigger.disabled = true;
      trigger.setAttribute("aria-expanded", "false");
      menu.replaceChildren();
      menu.hidden = true;
      return;
    }
    state.currentTier = tier.id;
    $("#tier-current-name").textContent = contentField(tier, "name");
    $("#tier-current-version").textContent = `v${tier.version}`;
    trigger.title = contentField(tier, "tagline");
    trigger.disabled = Boolean(state.activeRun && ["queued", "running"].includes(state.activeRun.status));
    menu.innerHTML = state.tierCatalog.tiers.map((item) => {
      const locked = item.gated && !item.unlocked;
      return `<button class="tier-option${item.id === tier.id ? " active" : ""}${locked ? " locked" : ""}" type="button" role="option" aria-selected="${item.id === tier.id}" aria-disabled="${locked}" data-tier="${item.id}">
        <span class="tier-option-copy"><strong>${escapeHtml(contentField(item, "name"))} <em>v${escapeHtml(item.version)}</em>${item.gated ? " <i>Preview</i>" : ""}</strong><small>${escapeHtml(contentField(item, "tagline"))}</small></span>
        <span class="tier-ratings"><span>${t('workspace:tier.capability', { dots: ratingDots(item.capability_rating) })}</span><span>${t('workspace:tier.speed', { dots: ratingDots(item.speed_rating) })}</span></span>
        ${locked ? `<span class="tier-lock" aria-label="${t('workspace:tier.lockedAria')}">${t('workspace:tier.locked')}</span>` : ""}
      </button>`;
    }).join("");
    $$('[data-tier]').forEach((button) => button.onclick = () => {
      const item = state.tierCatalog.tiers.find((candidate) => candidate.id === button.dataset.tier);
      if (!item) return;
      if (item.gated && !item.unlocked) { toast(t('workspace:scenario.locked')); return; }
      if (state.current) { toast(t('workspace:scenario.switchTierAfterStart')); return; }
      state.currentTier = item.id;
      $("#tier-menu").hidden = true;
      $("#tier-trigger").setAttribute("aria-expanded", "false");
      renderTierPicker();
    });
  }

  function toggleTierPicker() {
    if (!state.tiersAvailable) { toast(t('workspace:tier.serverDefaultToast')); return; }
    if (state.current) { toast(t('workspace:scenario.switchTierAfterStart')); return; }
    const menu = $("#tier-menu");
    menu.hidden = !menu.hidden;
    $("#tier-trigger").setAttribute("aria-expanded", String(!menu.hidden));
  }

  const defaultPromptHint = () => t('workspace:composer.placeholder');

  function applyScenario(scenarioId) {
    const item = state.scenarioCatalog.scenarios.find((scenario) => scenario.id === scenarioId);
    state.currentScenario = item?.id || null;
    if (item) {
      state.currentDomain = item.domain_id;
      modeBadge.hidden = false;
      modeBadge.textContent = t('workspace:scenario.modeBadge', { workflow: workflowBadge(item.workflow) });
      $("#scenario-current").hidden = false;
      $("#scenario-current-name").textContent = contentField(item, "name");
      $("#scenario-clear").hidden = false;
      $("#scenario-current").title = t('workspace:scenario.domainTitle', { domain: contentField(item, "domain_name"), label: contentField(item, "workflow_label") });
      if (!state.current) promptInput.placeholder = contentField(item, "default_prompt") || defaultPromptHint();
    } else {
      modeBadge.hidden = true;
      $("#scenario-current").hidden = false;
      $("#scenario-current-name").textContent = t('workspace:scenario.autoMatch');
      $("#scenario-clear").hidden = true;
      $("#scenario-current").title = t('workspace:scenario.autoMatchTitle');
      if (!state.current) promptInput.placeholder = defaultPromptHint();
    }
    updateComposer();
  }

  function toggleChatActions(show) {
    ["#sources-button", "#share-button"].forEach((selector) => { $(selector).hidden = !show; });
  }

  async function loadChat(chatId, canApply = () => true, canPreview = () => true) {
    let bundle;
    try {
      bundle = await api(`/api/chats/${chatId}`);
    } catch (err) {
      if (canApply()) { toast(err.message); location.hash = "#home"; }
      return;
    }
    // A review handoff may outlive its preview; return the snapshot without navigating back.
    if (disposed || !canApply()) return bundle;
    showChat(bundle);
    if (state.activeRun) subscribe(state.activeRun.id, bundle, canPreview);
    return bundle;
  }

  function showChat(bundle) {
    state.current = bundle;
    showView("chat-view");
    moveComposer("#chat-composer-slot");
    promptInput.rows = 1;
    const chat = state.current.chat;
    const latest = [...state.current.runs].reverse().find((run) => ["queued", "running"].includes(run.status));
    state.activeRun = latest || null;
    state.selectedProject = chat.project_id;
    state.currentScenario = chat.scenario_source === "pending"
      ? null
      : chat.scenario_id || null;
    state.currentTier = chat.tier_id || state.tierCatalog.default_tier_id;
    promptInput.value = "";
    renderTierPicker();
    modeBadge.hidden = false;
    modeBadge.textContent = workflowBadge(chat.mode);
    renderChatScenarioBadge(chat);
    $("#page-kicker").textContent = chat.mode === "agent_team" ? "AGENT TEAM" : "REACT";
    $("#page-title").textContent = chat.title;
    toggleChatActions(true);
    const sourceCount = state.current.sources.length;
    $("#source-count").textContent = sourceCount;
    $("#sources-button").setAttribute("aria-label", t('workspace:topbar.sources', { count: sourceCount }));
    $("#sources-button").dataset.tip = t('workspace:topbar.sources', { count: sourceCount });
    $("#sources-button").hidden = sourceCount === 0;
    renderConversation({scrollToEnd: true});
    updateRunBanner();
    promptInput.placeholder = t('workspace:composer.followUpPlaceholder');
    updateComposer();
  }

  async function storeArtifactInLibrary(artifactId) {
    try {
      await requestArtifactStorage(artifactId, state.current?.chat.project_id, api);
      await refreshAll();
      toast(t('workspace:toast.librarySaved'));
    } catch (err) { toast(err.message); }
  }

  function subscribe(runId, current = state.current, canPreview = () => true) {
    const run = current?.runs.find((item) => item.id === runId);
    if (!run || terminalTypes.has(`run_${run.status}`)) return;
    const chatId = current.chat.id;
    const title = current.chat.title;
    closeEventSource(runId);
    if (state.current?.chat.id === chatId) state.activeRun = run;
    state.backgroundRuns.set(runId, { runId, chatId, title });
    renderBackgroundRun();
    subscriptions.watch(run, async (item, source) => {
      const type = item.type;
      if (state.current?.chat.id === chatId && ["activity_started", "activity_finished"].includes(type)) {
        for (const read of sourceReadItems(item.data)) {
          if (!current.sources.some((source) => source.run_id === runId && sourceUrlKey(source.url) === sourceUrlKey(read.url))) {
            current.sources.push({ run_id: runId, url: read.url, domain: sourceHostname(read), title: sourceHostname(read) });
          }
        }
        const count = current.sources.length;
        $("#source-count").textContent = count;
        $("#sources-button").setAttribute("aria-label", t('workspace:topbar.sources', { count }));
        $("#sources-button").dataset.tip = t('workspace:topbar.sources', { count });
        $("#sources-button").hidden = count === 0;
      }
      if (["assistant_delta", "progress_note", "activity_started", "activity_finished", "warning"].includes(type)) {
        run.liveDraft = collectDraft(run);
      }
      if (type === "queued") {
        run.status = "queued";
        updateChatStatus(chatId, "queued");
      }
      if (type === "run_started") {
        run.status = "running";
        updateChatStatus(chatId, "running");
      }
      if (
        type === "orchestration_stage"
        && item.data.stage === "scenario_routing"
        && state.current?.chat.id === chatId
        && state.current.chat.scenario_source === "pending"
      ) {
        renderChatScenarioBadge(state.current.chat, { routing: true });
      }
      if (
        type === "orchestration_stage"
        && item.data.stage === "scenario_resolved"
        && ["auto", "fallback"].includes(item.data.scenario_source)
        && state.current?.chat.id === chatId
        && state.current.chat.scenario_source === "pending"
      ) {
        Object.assign(state.current.chat, {
          scenario_id: item.data.scenario_id,
          scenario_version: item.data.scenario_version,
          scenario_source: item.data.scenario_source,
          scenario_reason: item.data.scenario_reason,
          scenario_confidence: item.data.scenario_confidence,
          extra_domain_ids: (item.data.extra_domain_ids || []).join(","),
        });
        Object.assign(run, {
          scenario_id: item.data.scenario_id,
          scenario_version: item.data.scenario_version,
          scenario_source: item.data.scenario_source,
          scenario_reason: item.data.scenario_reason,
          scenario_confidence: item.data.scenario_confidence,
          extra_domain_ids: (item.data.extra_domain_ids || []).join(","),
          domain_pack_id: item.data.domain_pack_id,
          domain_pack_version: item.data.domain_pack_version,
        });
        state.currentScenario = item.data.scenario_id;
        const resolvedScenario = selectedScenario();
        if (resolvedScenario) state.currentDomain = resolvedScenario.domain_id;
        renderChatScenarioBadge(state.current.chat);
        const notice = scenarioRoutingNotice(item);
        if (notice) toast(notice);
      }
      if (type === "run_failed") {
        run.status = "failed";
        run.error = item.data.message || item.data.reason || "";
      }
      if (type === "run_cancelled") run.status = "cancelled";
      if (type === "run_completed") run.status = "completed";
      if (state.current?.chat.id === chatId) {
        scheduleConversation({draftOnly: DRAFT_ONLY_EVENT_TYPES.has(type)});
      }
      const taskDrawer = $("#detail-drawer");
      if (
        (type === "task_board_updated" || terminalTypes.has(type))
        && state.current?.chat.id === chatId
        && !taskDrawer.hidden
        && taskDrawer.dataset.taskRunId === runId
      ) {
        openTasks(runId);
      }
      if (terminalTypes.has(type)) {
        run.ended_at = item.ts || Date.now() / 1000;
        if (state.current?.chat.id === chatId && state.activeRun?.id === runId) {
          renderChatScenarioBadge(state.current.chat);
        }
        closeEventSource(runId, source);
        state.backgroundRuns.delete(runId);
        if (state.stoppingRunId === runId) state.stoppingRunId = null;
        renderBackgroundRun();
        updateChatStatus(chatId, run.status);
        const messageKey = type === "run_completed"
          ? (run.kind === "answer" ? "run:outcome.answerCompleted" : run.kind === "revise" ? "run:outcome.revised" : "run:outcome.reportReady")
          : type === "run_cancelled" ? "run:outcome.stopped" : "run:outcome.failed";
        toast(t(messageKey), { label: t("run:outcome.view"), run: () => { location.hash = `#chat/${chatId}`; } });
        const reviewResearch = state.reviewResearchRuns.delete(runId);
        const previewId = canPreview() && state.current?.chat.id === chatId ? reports.snapshot().artifactId : null;
        const previewGeneration = reports.snapshot().generation;
        const reviewNavigation = review.snapshot().navigation;
        await refreshAll();
        const canLoadChat = () => location.hash === `#chat/${chatId}` && state.current?.chat.id === chatId;
        if (canLoadChat()) {
          await loadChat(chatId, canLoadChat);
          if (previewId && canPreview() && previewGeneration === reports.snapshot().generation && reviewNavigation === review.snapshot().navigation && reports.snapshot().artifactId === previewId && state.current?.chat.id === chatId) {
            const revised = type === "run_completed" && (run.kind === "revise" || reviewResearch) && run.base_artifact_id === previewId
              ? state.current.artifacts.find((a) => a.run_id === runId && /\.md$/i.test(a.name))?.id : null;
            if (revised) carryReviewConversation(previewId, revised);
            await openArtifactPreview(revised || previewId);
          } else if (canLoadChat() && reports.snapshot().artifactId) {
            // Refresh locks/status in the user's chosen preview without opening or carrying anything.
            const generation = reports.snapshot().generation;
            await loadReviewWorkspace(generation);
            if (generation === reports.snapshot().generation && review.snapshot().data) renderReviewWorkspace();
          }
        }
      }
    });
  }

  const closeEventSource = subscriptions.close;
  const closeAllEventSources = subscriptions.dispose;

  function updateRunBanner() {
    const running = state.activeRun && ["queued", "running"].includes(state.activeRun.status);
    const stopping = running && state.stoppingRunId === state.activeRun.id;
    const stopButton = $("#stop-run");
    $("#run-banner").hidden = !running;
    stopButton.textContent = stopping ? t("run:banner.stopping") : t("run:banner.stop");
    stopButton.disabled = Boolean(stopping);
    stopButton.setAttribute("aria-busy", String(Boolean(stopping)));
    promptInput.disabled = Boolean(running);
    $("#attach-button").disabled = Boolean(running);
    updateComposer();
  }

  function updateComposer() {
    const running = state.activeRun && ["queued", "running"].includes(state.activeRun.status);
    const promptLength = promptInput.value.length;
    const promptCount = $("#prompt-count");
    sendButton.disabled = state.submitting || running || !promptInput.value.trim();
    $("#tier-trigger").disabled = Boolean(running) || !state.tiersAvailable;
    sendButton.textContent = state.current ? t('workspace:composer.sendFollowUp') : t('workspace:composer.sendStart');
    promptCount.textContent = `${promptLength} / 32000`;
    promptCount.hidden = promptLength < 30000;
    renderAttachmentChips();
  }

  function renderChatScenarioBadge(chat, { routing = false } = {}) {
    const pending = chat.scenario_source === "pending";
    const runStatus = state.current?.chat.id === chat.id ? state.current.runs.at(-1)?.status : "";
    const ended = ["completed", "failed", "cancelled"].includes(runStatus);
    const scenario = selectedScenarioFromCatalog(
      state.scenarioCatalog.scenarios,
      chat.scenario_id,
    );
    $("#scenario-current").hidden = false;
    $("#scenario-current-name").textContent = scenarioBadgeLabel(
      chat,
      state.scenarioCatalog.scenarios,
      routing,
      runStatus,
    );
    $("#scenario-current").title = pending
      ? ended ? t('workspace:scenario.resolvedEnded') : t('workspace:scenario.routingPending')
      : `${contentField(scenario, "domain_name") || t('workspace:scenario.badgeGeneric')} · ${workflowBadge(chat.mode)}`;
  }

  async function submitPrompt() {
    const prompt = promptInput.value.trim();
    if (!prompt || state.submitting || sendButton.disabled) return;
    state.submitting = true;
    sendButton.disabled = true;
    try {
      let chatId = state.current?.chat.id;
      if (!chatId) {
        const compact = prompt.replace(/\s+/g, " ").trim();
        const firstSentence = compact.split(/[。！？!?\n]/)[0];
        const title = truncateChatTitle(firstSentence) || "新研究"; // i18n-inline:3 — stored default chat title (phase 3)
        const chatBody = buildChatCreateBody({
          title,
          prompt,
          scenarioId: state.currentScenario,
          projectId: state.selectedProject,
          tierId: state.currentTier,
          tiersAvailable: state.tiersAvailable,
        });
        // 建 chat 只记录待匹配占位；场景路由在首个 run 的准备阶段完成。
        const chat = await api("/api/chats", { method: "POST", body: chatBody });
        chatId = chat.id;
        if (["auto", "fallback"].includes(chat.scenario_source)) {
          state.currentScenario = chat.scenario_id || null;
          const notice = scenarioRoutingNotice(chat);
          if (notice) toast(notice);
        }
      }
      const scenarioId = state.current?.chat.scenario_id || state.currentScenario || undefined;
      const tierId = state.current?.chat.tier_id || state.currentTier;
      const runBody = {
        prompt, scenario_id: scenarioId,
        library_ids: [...state.selectedLibrary],
      };
      if (state.tiersAvailable) runBody.tier_id = tierId;
      const result = await api(`/api/chats/${chatId}/runs`, { method: "POST", body: runBody });
      const outcome = runStartOutcome(result);
      promptInput.value = "";
      if (outcome.kind === "run") state.selectedLibrary.clear();
      location.hash = `#chat/${chatId}`;
      await refreshAll();
      await loadChat(chatId);
      if (outcome.kind === "clarification") {
        toast(t('workspace:toast.clarify'));
        promptInput.focus();
        return;
      }
      subscribe(outcome.run.id);
    } catch (err) {
      toast(err.message);
    } finally {
      state.submitting = false;
      updateComposer();
    }
  }

  async function stopRun() {
    const run = state.activeRun;
    if (!run || state.stoppingRunId === run.id) return;
    if (!confirm(t("run:stop.confirm"))) return;
    state.stoppingRunId = run.id;
    updateRunBanner();
    try {
      const result = await api(`/api/runs/${run.id}/stop`, { method: "POST" });
      toast(result.requested ? t("run:stop.requested") : t("run:stop.alreadyEnded"));
    } catch (err) {
      state.stoppingRunId = null;
      updateRunBanner();
      toast(err.message);
    }
  }

  function renderProjects() {
    showView("projects-view");
    state.selectedProject = null;
    $("#page-kicker").textContent = "WORKSPACE";
    $("#page-title").textContent = t('workspace:nav.projects');
    toggleChatActions(false);
    const projectCounts = (project) => `${t('common:count.studies', { count: project.chat_count })} · ${t('common:count.materials', { count: project.library_count })}`;
    $("#project-grid").innerHTML = state.projects.map((project) => `
      <article class="project-card"><button data-open-project="${project.id}"><h3>${escapeHtml(project.name)}</h3><p>${escapeHtml(project.description || t('workspace:project.noDescription'))}</p></button><footer><span>${projectCounts(project)}</span><span><button data-edit-project="${project.id}">${t('common:action.edit')}</button><button data-delete-project="${project.id}">${t('common:action.delete')}</button></span></footer></article>
    `).join("") || `<p class="empty-note">${t('workspace:project.createFirst')}</p>`;
    $$('[data-open-project]').forEach((button) => button.onclick = () => { location.hash = `#project/${button.dataset.openProject}`; });
    $$('[data-edit-project]').forEach((button) => button.onclick = () => openProjectDialog(state.projects.find((x) => x.id === button.dataset.editProject)));
    $$('[data-delete-project]').forEach((button) => button.onclick = async () => {
      if (!confirm(t('workspace:project.deleteConfirm'))) return;
      try { await api(`/api/projects/${button.dataset.deleteProject}`, { method: "DELETE" }); await refreshAll(); renderProjects(); } catch (err) { toast(err.message); }
    });
  }

  function renderProject(projectId) {
    const project = state.projects.find((item) => item.id === projectId);
    if (!project) { location.hash = "#projects"; return; }
    state.selectedProject = projectId;
    showView("projects-view");
    $("#page-kicker").textContent = "PROJECT";
    $("#page-title").textContent = project.name;
    toggleChatActions(false);
    const chats = state.chats.filter((chat) => chat.project_id === projectId);
    const library = state.library.filter((item) => item.project_id === projectId);
    $("#project-grid").innerHTML = `
      <article class="project-card"><h3>${escapeHtml(project.name)}</h3><p>${escapeHtml(project.description || t('workspace:project.noDescriptionDetail'))}</p><footer><span>${t('common:count.studies', { count: chats.length })} · ${t('common:count.materials', { count: library.length })}</span><button id="project-new-research" class="button primary">${t('workspace:project.newResearchInProject')}</button></footer></article>
      ${chats.map((chat) => `<article class="project-card"><button data-project-chat="${chat.id}"><h3>${escapeHtml(chat.title)}</h3><p>${escapeHtml(chat.mode === "agent_team" ? t('workspace:project.agentTeamChat') : t('workspace:project.reactChat'))}</p></button><footer><span>${formatDate(chat.updated_at)}</span><span>${escapeHtml(chat.status)}</span></footer></article>`).join("")}`;
    $("#project-new-research").onclick = enterNewResearch;
    $$('[data-project-chat]').forEach((button) => button.onclick = () => { location.hash = `#chat/${button.dataset.projectChat}`; });
  }

  function openProjectDialog(project = null) {
    prepareTransientSurface("form");
    const dialog = $("#form-dialog");
    $("#dialog-kicker").textContent = project ? "EDIT" : "CREATE";
    $("#dialog-title").textContent = project ? t('workspace:project.editDialog') : t('workspace:project.createDialog');
    $("#dialog-fields").innerHTML = `<label>${t('workspace:project.nameLabel')}<input id="project-name-field" required maxlength="120" value="${escapeHtml(project?.name || "")}"></label><label>${t('workspace:project.descriptionLabel')}<textarea id="project-description-field" maxlength="1000">${escapeHtml(project?.description || "")}</textarea></label>`;
    $("#dialog-error").textContent = "";
    $("#dialog-form").onsubmit = async (event) => {
      event.preventDefault();
      if (event.submitter?.value === "cancel") { dialog.close(); return; }
      try {
        const body = { name: $("#project-name-field").value, description: $("#project-description-field").value };
        const saved = await api(project ? `/api/projects/${project.id}` : "/api/projects", { method: project ? "PATCH" : "POST", body });
        dialog.close(); await refreshAll(); location.hash = `#project/${saved.id}`;
      } catch (err) { $("#dialog-error").textContent = err.message; }
    };
    dialog.showModal();
  }

  /** The library page lists deliberate saves; composer uploads are run material only. */
  const libraryScopeItems = () => scopeItemsFor(state.library, state.libraryScope);

  function renderLibrary() {
    showView("library-view");
    $("#page-kicker").textContent = "KNOWLEDGE";
    $("#page-title").textContent = t('workspace:nav.library');
    toggleChatActions(false);
    const scope = LIBRARY_SCOPES.find((entry) => entry.id === state.libraryScope) || LIBRARY_SCOPES[0];
    const counts = Object.fromEntries(LIBRARY_SCOPES.map((entry) =>
      [entry.id, scopeItemsFor(state.library, entry.id).length]));
    $("#library-filter").innerHTML = LIBRARY_SCOPES.map((entry) =>
      `<button type="button" role="tab" data-library-scope="${entry.id}" aria-selected="${entry.id === scope.id}" class="library-scope${entry.id === scope.id ? " active" : ""}">${t(entry.labelKey)} <em>${counts[entry.id]}</em></button>`).join("");
    $$('[data-library-scope]').forEach((button) => button.onclick = () => {
      state.libraryScope = button.dataset.libraryScope;
      renderLibrary();
    });
    const items = libraryScopeItems();
    $("#library-table").innerHTML = items.map((item) => {
      const project = state.projects.find((x) => x.id === item.project_id);
      const source = isRunAttachment(item) ? `<span class="library-origin">${t('workspace:library.originAttachment')}</span>` : "";
      return `<div class="library-row"><span><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.mime)} ${parseBadge(item)} ${source}</small></span><span>${escapeHtml(project?.name || t('workspace:library.uncategorized'))}</span><span>${formatBytes(item.size)}</span><button data-delete-library="${item.id}" aria-label="${t('workspace:library.deleteAria', { name: escapeHtml(item.name) })}">×</button></div>`;
    }).join("") || `<p class="empty-note">${scope.id === "attachment"
      ? t('workspace:library.emptyAttachment')
      : t('workspace:library.emptyLibrary')}</p>`;
    $$('[data-delete-library]').forEach((button) => button.onclick = async () => {
      if (!confirm(t('workspace:library.deleteConfirm'))) return;
      try { await api(`/api/library/${button.dataset.deleteLibrary}`, { method: "DELETE" }); await refreshAll(); renderLibrary(); } catch (err) { toast(err.message); }
    });
  }

  /** Scanned uploads parse on the host; the badge is the only place a user can see it. */
  const parseBadge = (item) => parseBadgeMarkup(item);

  const uploadLimitBytes = () => configuredUploadLimit(state.config);

  let uploadRows = [];
  function renderUploadStatus() {
    const node = $("#upload-status");
    if (!node) return;
    const visible = uploadRows.filter((row) => row.visible);
    if (!visible.length) {
      node.hidden = true;
      node.innerHTML = "";
      return;
    }
    node.hidden = false;
    node.innerHTML = uploadRowsMarkup(uploadRows);
  }

  // On localhost a small upload finishes in ~20 ms, so a bar that appears and vanishes
  // inside a frame reads as noise. Rule: nothing is shown until an upload has actually
  // taken a moment, and once shown it stays long enough to read. Fast uploads are
  // confirmed by the toast, the attachment chip and the library row instead.
  const UPLOAD_REVEAL_MS = 400;
  const UPLOAD_MIN_VISIBLE_MS = 900;
  function finishUploadRow(row, phase, error = "") {
    row.phase = phase;
    row.error = error;
    clearTimeout(row.revealTimer);
    if (!row.visible) {
      uploadRows = uploadRows.filter((candidate) => candidate !== row);
      return;
    }
    renderUploadStatus();
    const hold = Math.max(0, UPLOAD_MIN_VISIBLE_MS - (Date.now() - row.shownAt));
    setTimeout(() => {
      uploadRows = uploadRows.filter((candidate) => candidate !== row);
      renderUploadStatus();
    }, hold);
  }

  /** XHR instead of fetch so a 100 MB upload can show real byte progress. */
  function uploadForm(path, form, onProgress) {
    const XHR = environment.XMLHttpRequest;
    if (!XHR) return api(path, { method: "POST", body: form });
    return new Promise((resolve, reject) => {
      const request = new XHR();
      request.open("POST", path);
      request.withCredentials = true;
      request.upload?.addEventListener("progress", (event) => {
        if (event.lengthComputable) onProgress(event.loaded, event.total);
      });
      request.addEventListener("load", () => {
        let payload = request.responseText;
        try { payload = JSON.parse(request.responseText); } catch { /* keep the raw body */ }
        if (request.status >= 200 && request.status < 300) {
          onProgress(undefined, undefined);  // bytes are in: the server is now probing/parsing
          resolve(payload);
          return;
        }
        reject(new ApiError(payload?.error || payload || t('errors:uploadHttp', { status: request.status }), request.status));
      });
      request.addEventListener("error", () => reject(new ApiError(t('errors:connect'), 0)));
      request.addEventListener("abort", () => reject(new ApiError(t('errors:uploadCancelled'), 0)));
      request.send(form);
    });
  }

  // Attachments dropped/pasted into the composer feed this run; they must not pile up
  // in the library the way a deliberate library save does.
  const ATTACHMENT_ORIGIN = "attachment";
  async function uploadLibrary(files, { selectUploaded = false, origin = "" } = {}) {
    const uploaded = [];
    const limit = uploadLimitBytes();
    const queue = [...files];
    const rows = queue.map((file) => ({
      name: file.name, label: "", loaded: 0, total: file.size || 0, phase: "uploading",
      visible: false, shownAt: 0, revealTimer: null,
    }));
    for (const oversized of oversizedFiles(queue, limit)) {
      const index = queue.indexOf(oversized);
      toast(t('workspace:toast.oversized', { name: oversized.name, size: formatBytes(oversized.size), limit: formatBytes(limit) }));
      rows[index].skipped = true;
    }
    const pending = queue.filter((_file, index) => !rows[index].skipped);
    const pendingRows = rows.filter((row) => !row.skipped);
    if (!pending.length) return uploaded;
    uploadRows.push(...pendingRows);
    if (pending.length > 1) {
      pendingRows.forEach((row, index) => { row.label = `${index + 1}/${pending.length} `; });
    }
    for (const row of pendingRows) {
      if (pending.length > 1) row.label = `${pendingRows.indexOf(row) + 1}/${pending.length} `;
    }
    renderUploadStatus();
    for (const [index, file] of pending.entries()) {
      const row = pendingRows[index];
      // The reveal clock starts per file: queued rows must not sit at "0%" while waiting.
      row.revealTimer = setTimeout(() => {
        if (disposed) return;
        row.visible = true;
        row.shownAt = Date.now();
        renderUploadStatus();
      }, UPLOAD_REVEAL_MS);
      const form = new FormData();
      form.append("file", file);
      if (origin) form.append("origin", origin);
      // Run material is not a project asset; only deliberate library saves get a project.
      if (!origin && state.selectedProject) form.append("project_id", state.selectedProject);
      try {
        const item = await uploadForm("/api/library", form, (loaded, total) => {
          if (loaded !== undefined) { row.loaded = loaded; row.total = total || row.total; }
          else { row.loaded = row.total; row.phase = "parsing"; }
          renderUploadStatus();
        });
        row.phase = "parsing";
        if (row.visible) renderUploadStatus();
        uploaded.push(item);
        toast(t('workspace:toast.uploaded', { name: file.name }));
      } catch (err) {
        toast(`${file.name}: ${err.message}`);
        finishUploadRow(row, "failed", err.message);
        continue;
      }
      finishUploadRow(row, "done");
    }
    await refreshAll();
    if (selectUploaded) {
      uploaded.forEach((item) => state.selectedLibrary.add(item.id));
      renderAttachmentChips();
    }
    if (location.hash === "#library") renderLibrary();
    return uploaded;
  }
  function prepareTransientSurface(kind) {
    if (kind !== "chat-menu") closeChatContextMenu();
    if (kind !== "delete-confirm") closeChatDeleteConfirm();
    if (kind !== "user") $("#user-popover").hidden = true;
    if (kind !== "detail") $("#detail-drawer").hidden = true;
    if (kind !== "search" && $("#search-dialog").open) $("#search-dialog").close();
    if (kind !== "form" && $("#form-dialog").open) $("#form-dialog").close();
    if (kind !== "preview" && reports.snapshot().artifactId) closeArtifactPreview();
  }

  function openAttachmentDrawer() {
    prepareTransientSurface("detail");
    const drawer = $("#detail-drawer");
    delete drawer.dataset.taskRunId;
    drawer.dataset.drawerKind = "attachments";
    drawer.classList.add("attachment-mode");
    $("#drawer-kicker").textContent = t('workspace:upload.drawerKicker');
    $("#drawer-title").textContent = t('workspace:upload.drawerTitle');
    const fileExt = (name) => (name.includes(".") ? name.split(".").pop() : "FILE").slice(0, 4).toUpperCase();
    const libraryChoices = state.library.map((item) => `
      <label class="task-row attachment-choice${state.selectedLibrary.has(item.id) ? " selected" : ""}"><input type="checkbox" data-library-check="${item.id}" ${state.selectedLibrary.has(item.id) ? "checked" : ""}><span class="file-icon">${escapeHtml(fileExt(item.name))}</span><span><p>${escapeHtml(item.name)}</p><small>${formatBytes(item.size)} ${isRunAttachment(item) ? `<span class="library-origin">${t('workspace:library.originAttachment')}</span> ` : ""}<span data-attachment-parse="${item.id}">${parseBadge(item)}</span></small></span></label>
    `).join("") || `<p class="empty-note">${t('workspace:upload.emptyLibrary')}</p>`;
    const limitNote = uploadLimitBytes() ? t('workspace:upload.limitNote', { limit: formatBytes(uploadLimitBytes()) }) : "";
    $("#drawer-content").innerHTML = `
      <section class="attachment-upload-card" id="attachment-dropzone">
        <button id="attachment-upload-local" class="button quiet" type="button">📎 ${t('workspace:upload.fromLocal')}</button>
        <p>${t('workspace:upload.dropzone')}${limitNote}</p>
      </section>
      <div class="drawer-section-title"><span>${t('workspace:upload.chooseFromLibrary')}</span><span data-attachment-count>${t('workspace:upload.selectedCount', { selected: state.selectedLibrary.size, total: state.library.length })}</span></div>
      <div class="attachment-library-list">${libraryChoices}</div>
      <div class="attachment-drawer-foot"><span>${t('workspace:upload.instantApply')}</span><button id="attachment-done" class="button primary" type="button">${t('workspace:upload.done')}</button></div>`;
    $("#attachment-upload-local").onclick = () => $("#composer-upload").click();
    $$('[data-library-check]').forEach((box) => box.onchange = () => {
      if (box.checked) state.selectedLibrary.add(box.dataset.libraryCheck); else state.selectedLibrary.delete(box.dataset.libraryCheck);
      box.closest(".attachment-choice")?.classList.toggle("selected", box.checked);
      const count = $("#drawer-content [data-attachment-count]");
      if (count) count.textContent = t('workspace:upload.selectedCount', { selected: state.selectedLibrary.size, total: state.library.length });
      renderAttachmentChips();
    });
    $("#attachment-done").onclick = () => { drawer.hidden = true; };
    const dropzone = $("#attachment-dropzone");
    ["dragenter", "dragover"].forEach((type) => dropzone.addEventListener(type, (event) => { event.preventDefault(); dropzone.classList.add("drag-over"); }));
    ["dragleave", "drop"].forEach((type) => dropzone.addEventListener(type, (event) => { event.preventDefault(); dropzone.classList.remove("drag-over"); }));
    dropzone.addEventListener("drop", async (event) => {
      const files = [...(event.dataTransfer?.files || [])];
      if (files.length) { await uploadLibrary(files, { selectUploaded: true }); openAttachmentDrawer(); }
    });
    drawer.hidden = false;
  }

  function renderAttachmentChips() {
    $("#attachment-chips").innerHTML = [...state.selectedLibrary].map((id) => {
      const item = state.library.find((x) => x.id === id);
      if (!item) return "";
      const parsing = item.parse?.status === "pending";
      const mark = parsing ? `<em title="${t('workspace:upload.chipParsingTitle')}">${t('workspace:upload.chipParsing')}</em>` : "";
      return `<span class="attachment-chip${parsing ? " pending" : ""}">${escapeHtml(item.name)}${mark}<button data-remove-attachment="${id}" aria-label="${t('workspace:upload.removeAria', { name: escapeHtml(item.name) })}">×</button></span>`;
    }).join("");
    $$('[data-remove-attachment]').forEach((button) => button.onclick = () => { state.selectedLibrary.delete(button.dataset.removeAttachment); renderAttachmentChips(); });
  }

  function renderArchive() {
    showView("archive-view");
    $("#page-kicker").textContent = "ARCHIVE";
    $("#page-title").textContent = t('workspace:nav.archive');
    toggleChatActions(false);
    $("#archive-list").innerHTML = state.archived.map((chat) => `<div class="archive-row" data-archive-chat="${chat.id}"><strong>${escapeHtml(chat.title)}</strong><span>${formatDate(chat.updated_at)}</span><button data-restore-chat="${chat.id}">${t('common:action.restore')}</button></div>`).join("") || `<p class="empty-note">${t('workspace:archive.empty')}</p>`;
    $$('[data-archive-chat]').forEach((row) => row.onclick = (event) => { if (!event.target.closest("button")) location.hash = `#chat/${row.dataset.archiveChat}`; });
    $$('[data-restore-chat]').forEach((button) => button.onclick = async () => { await api(`/api/chats/${button.dataset.restoreChat}`, { method: "PATCH", body: { archived: false } }); await refreshAll(); renderArchive(); });
  }

  function openTasks(runId) {
    prepareTransientSurface("detail");
    const drawer = $("#detail-drawer");
    drawer.dataset.taskRunId = runId;
    delete drawer.dataset.drawerKind;
    drawer.classList.remove("attachment-mode");
    const run = state.current.runs.find((item) => item.id === runId);
    const tasks = tasksForRun(run);
    const board = taskBoardPresentation(tasks, run.status, subagentSnapshotForRun(run));
    $("#drawer-kicker").textContent = "RUN";
    $("#drawer-title").textContent = t("run:task.drawerTitle", { progress: taskBoardProgressCopy(board) });
    $("#drawer-content").innerHTML = tasks.length
      ? renderTaskRows(board)
      : `<p class="empty-note">${escapeHtml(t("run:task.empty"))}</p>`;
    $("#detail-drawer").hidden = false;
  }

  function openSources() {
    prepareTransientSurface("detail");
    delete $("#detail-drawer").dataset.taskRunId;
    $("#detail-drawer").dataset.drawerKind = "sources";
    $("#detail-drawer").classList.remove("attachment-mode");
    $("#drawer-kicker").textContent = "EVIDENCE";
    renderSourcesDrawer();
    $("#detail-drawer").hidden = false;
  }

  function renderSourcesDrawer() {
    const scrollTop = $("#drawer-content").scrollTop;
    $("#drawer-title").textContent = t("run:sources.title", { count: state.current?.sources.length || 0 });
    const sources = state.current?.sources || [];
    $("#drawer-content").innerHTML = sources.length ? sources.map((source, index) => {
      const domain = sourceHostname(source);
      return `<div class="source-row"><span class="source-index">${index + 1}</span><a class="source-copy" href="${escapeHtml(safeUrl(source.url))}" target="_blank" rel="noopener noreferrer"><strong>${escapeHtml(source.title || domain || t("run:sources.unknownTitle"))}</strong><span class="source-domain-line">${sourceFavicon(domain)}<small>${escapeHtml(source.url)}</small></span></a></div>`;
    }).join("") : `<p class="empty-note">${escapeHtml(t("run:sources.empty"))}</p>`;
    $("#drawer-content").scrollTop = scrollTop;
  }

  async function branchChat(messageId = null) {
    if (!state.current) return;
    try {
      const branch = await api(`/api/chats/${state.current.chat.id}/branch`, { method: "POST", body: { message_id: messageId } });
      await refreshAll(); location.hash = `#chat/${branch.id}`; toast(t('workspace:toast.branchCreated'));
    } catch (err) { toast(err.message); }
  }

  async function copyChat(chatId) {
    try {
      const branch = await branchChatFromMenu(chatId);
      await refreshAll();
      location.hash = `#chat/${branch.id}`;
      toast(t('workspace:toast.copyChat'));
    } catch (err) { toast(err.message); }
  }

  async function deleteChat(chatId, confirmButton) {
    try {
      await deleteChatFromMenu(chatId);
      const currentDeleted = state.current?.chat.id === chatId;
      if (currentDeleted) state.current = null;
      closeChatDeleteConfirm();
      await refreshAll();
      if (currentDeleted) location.hash = "#home";
      else if (location.hash.startsWith("#project/")) renderProject(location.hash.split("/")[1]);
      toast(t('workspace:toast.chatDeleted'));
    } catch (err) {
      confirmButton.disabled = false;
      toast(err.message);
    }
  }

  async function shareChat() {
    if (!state.current) return;
    try {
      const result = await api(`/api/chats/${state.current.chat.id}/share`, { method: "POST", body: { expires_in_days: 7 } });
      await navigator.clipboard.writeText(result.url);
      toast(result.warning || t('workspace:toast.shareCopied'));
    } catch (err) { toast(err.message); }
  }

  async function archiveChat(chatId = state.current?.chat.id) {
    if (!chatId) return;
    try {
      await updateChatFromMenu(chatId, { archived: true });
      const currentArchived = state.current?.chat.id === chatId;
      await refreshAll();
      if (currentArchived) location.hash = "#archive";
      toast(t('workspace:toast.archived'));
    } catch (err) { toast(err.message); }
  }

  /** 分享页呈现投影：用内容语言实例渲染 chrome，不重新请求（与访问者 UI locale 无关）。 */
  function renderSharedContent(bundle) {
    const token = state.shareToken;
    const contentLocale = sharedI18n.getLocale();
    const assistants = bundle.messages.filter((message) => message.role === "assistant");
    const answerMessage = assistants.at(-1);
    const answer = answerMessage?.content || sharedT("report:share.emptyReport");
    const sources = Array.isArray(bundle.sources) ? bundle.sources : [];
    const supportsRunFiltering = Boolean(answerMessage?.run_id)
      && sources.some((source) => Object.prototype.hasOwnProperty.call(source, "run_id"));
    const answerSources = reportCitationSources(answer, supportsRunFiltering
      ? sources.filter((source) => source.run_id === answerMessage.run_id)
      : sources);
    const citationPrefix = `share-${token}`;
    const artifactResolver = artifactResolverFor(
      bundle.artifacts,
      (item) => `/api/public/shares/${encodeURIComponent(token)}/artifacts/${encodeURIComponent(item.id)}`,
    );
    const sharedGroups = groupArtifactsByRun(bundle.artifacts, bundle.runs || [])
      .map((group) => ({ ...group, artifacts: deliveryArtifactsForCard(group) }));
    const sharedPrimary = primaryArtifactForGroups(sharedGroups);
    const sharedArtifacts = sharedGroups.flatMap((group) => group.artifacts);
    const sharedRow = (item) => {
      const downloadMenu = item.id === sharedPrimary?.id
        ? reportDownloadMenuMarkup(item, bundle.artifacts, artifactEndpoint, sharedT)
        : "";
      // 分享页的产物行按内容语言渲染；产物名与报告正文仍是原文内容。
      return `<div class="artifact-row"><span class="file-icon">${escapeHtml(item.name.split(".").pop().toUpperCase())}</span><span class="artifact-copy"><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(artifactTypeLabel(item, sharedT))} · ${formatBytes(item.size)}</small></span><span>${isPreviewableArtifact(item) ? `<button data-shared-preview="${item.id}">${escapeHtml(sharedT("report:artifact.preview"))}</button>` : ""}${downloadMenu || `<a href="/api/public/shares/${token}/artifacts/${item.id}">${escapeHtml(sharedT("report:artifact.download"))}</a>`}</span></div>`;
    };
    $("#shared-content").innerHTML = `<div class="report"><h1>${escapeHtml(bundle.chat.title)}</h1>${renderMarkdown(answer, { citationPrefix, citationSources: answerSources, artifactResolver, translator: sharedT })}</div>${sharedArtifacts.length ? `<section class="artifacts"><div class="artifacts-head"><div class="artifacts-title"><h3>${escapeHtml(sharedT("report:artifact.deliveryTitle"))}</h3></div><span class="artifact-count">${sharedArtifacts.length}</span></div>${renderArtifactGroups(sharedGroups, sharedRow, sharedGroups.length > 1, sharedT, contentLocale)}</section>` : ""}`;
    bindReportAnchors($("#shared-content"));
    $$('[data-shared-preview]').forEach((button) => button.onclick = () => openArtifactPreview(button.dataset.sharedPreview));
    bindDownloadMenus($("#shared-content"), {signal: lifetime.signal});
  }

  async function loadShared(token) {
    $("#auth-screen").hidden = true;
    $("#app-shell").hidden = false;
    composer.hidden = true;
    $("#sidebar").hidden = true;
    $("#app-shell").classList.add("shared-mode");
    $(".topbar").hidden = true;
    showView("shared-view");
    // bootstrap 已按访问者 locale 投影过静态标注，这里用内容语言实例覆盖（含 skip-link），
    // 并同步文档语言声明；分享页此后不再参与访问者的语言切换。
    applyStatic(document, sharedT);
    syncDocumentLanguage();
    // 静态标注与 <html lang> 同时就绪后才揭示 skip-link（它可见时必须与文档语言一致）。
    revealSkipLink();
    try {
      const bundle = await api(`/api/public/shares/${token}`, {}, sharedT);
      // 分享页渲染前提的结构校验：成功响应也可能缺字段，必须在数据边界转为受控错误，
      // 否则下游会抛出原生 TypeError 并显示在页面上。
      if (!bundle || typeof bundle !== "object" || !Array.isArray(bundle.messages) || !bundle.chat || typeof bundle.chat !== "object") {
        throw new ApiError(sharedT("errors:dataLoad"), 200);
      }
      state.shareToken = token;
      state.sharedBundle = bundle;
      renderSharedContent(bundle);
    } catch (err) {
      // 只呈现受控文案：api()/结构校验的 ApiError 已按视图语言本地化，其余（含原生异常）兜底，
      // 绝不把浏览器/JS 原生错误串带到分享页。
      const message = err instanceof ApiError ? err.message : sharedT("errors:dataLoad");
      $("#shared-content").innerHTML = `<p class="empty-note">${escapeHtml(message)}</p>`;
    }
  }

  function openSearch() {
    prepareTransientSurface("search");
    const dialog = $("#search-dialog");
    dialog.showModal();
    $("#global-search").value = "";
    $("#search-results").innerHTML = `<p class="empty-note">${t('workspace:search.empty')}</p>`;
    requestAnimationFrame(() => $("#global-search").focus());
  }

  async function searchInput() {
    clearTimeout(state.searchTimer);
    const q = $("#global-search").value.trim();
    if (!q) { $("#search-results").innerHTML = `<p class="empty-note">${t('workspace:search.empty')}</p>`; return; }
    state.searchTimer = setTimeout(async () => {
      try {
        const results = await api(`/api/search?q=${encodeURIComponent(q)}`);
        $("#search-results").innerHTML = results.map((item) => `<button class="search-result" data-result-type="${item.type}" data-result-id="${item.id}"><strong>${escapeHtml(item.title)}</strong><small>${escapeHtml(item.snippet || item.type)}</small></button>`).join("") || `<p class="empty-note">${t('workspace:search.noResults')}</p>`;
        $$('[data-result-id]').forEach((button) => button.onclick = () => {
          $("#search-dialog").close();
          location.hash = button.dataset.resultType === "chat" ? `#chat/${button.dataset.resultId}` : button.dataset.resultType === "project" ? `#project/${button.dataset.resultId}` : "#library";
        });
      } catch (err) { $("#search-results").innerHTML = `<p class="empty-note">${escapeHtml(err.message)}</p>`; }
    }, 220);
  }

  function openConfig() {
    prepareTransientSurface("form");
    const dialog = $("#form-dialog");
    $("#dialog-kicker").textContent = "LOCAL RUNTIME";
    $("#dialog-title").textContent = t('workspace:config.title');
    $("#dialog-fields").innerHTML = `<div class="report"><p><strong>${t('workspace:config.model')}</strong><br>${escapeHtml(state.config.model)}</p><p><strong>${t('workspace:config.endpoint')}</strong><br>${escapeHtml(state.config.endpoint)}</p><p><strong>${t('workspace:config.mode')}</strong><br>${t('workspace:config.modeValue')}</p><p><strong>${t('workspace:config.network')}</strong><br>${t('workspace:config.networkValue')}</p><p><strong>${t('workspace:config.search')}</strong><br>${state.config.search ? t('workspace:config.searchConfigured') : t('workspace:config.searchUnset')}</p></div>`;
    $("#dialog-error").textContent = "";
    $("#dialog-submit").hidden = true;
    $("#dialog-form").onsubmit = (event) => { event.preventDefault(); dialog.close(); $("#dialog-submit").hidden = false; };
    dialog.addEventListener("close", () => { $("#dialog-submit").hidden = false; }, { once: true });
    dialog.showModal();
  }

  function openSidebar() { $("#sidebar").classList.add("open"); $("#sidebar-scrim").hidden = false; }

  function closeSidebar() { $("#sidebar").classList.remove("open"); $("#sidebar-scrim").hidden = true; }

  function bindEvents() {
    renderTopbarIcons();
    listen(document, "error", (event) => {
      const image = event.target;
      if (!(image instanceof HTMLImageElement) || !image.matches(".source-favicon img")) return;
      image.hidden = true;
      const fallback = image.nextElementSibling;
      if (fallback?.classList.contains("source-favicon-fallback")) fallback.hidden = false;
    }, true);
    listen($("#auth-form"), "submit", authSubmit);
    listen($("#auth-switch"), "click", () => setAuthMode(state.authMode === "login" ? "register" : "login"));
    listen(promptInput, "input", updateComposer);
    listen(promptInput, "paste", handleComposerPaste);
    listen(promptInput, "keydown", (event) => {
      if ((event.metaKey || event.ctrlKey) && event.key === "Enter") { event.preventDefault(); submitPrompt(); }
    });
    listen(sendButton, "click", submitPrompt);
    listen($("#tier-trigger"), "click", toggleTierPicker);
    listen($("#stop-run"), "click", stopRun);
    listen($("#new-research"), "click", () => {
      state.selectedProject = null;
      state.currentTier = state.tierCatalog.default_tier_id;
      enterNewResearch();
      renderTierPicker();
      promptInput.focus();
    });
    listen($(".brand"), "click", (event) => {
      event.preventDefault();
      enterNewResearch();
    });
    listen($("#project-add"), "click", () => openProjectDialog());
    listen($("#projects-toggle"), "click", (event) => {
      const expanded = event.currentTarget.getAttribute("aria-expanded") === "true";
      event.currentTarget.setAttribute("aria-expanded", String(!expanded));
      event.currentTarget.setAttribute("aria-label", expanded ? t('workspace:nav.expandProjects') : t('workspace:nav.collapseProjects'));
      event.currentTarget.textContent = expanded ? "⌄" : "⌃";
      $("#project-nav").hidden = expanded;
    });
    listen($("#project-create-main"), "click", () => openProjectDialog());
    listen($("#open-search"), "click", openSearch);
    listen($("#global-search"), "input", searchInput);
    listen($("#attach-button"), "click", openAttachmentDrawer);
    listen($("#composer-upload"), "change", async (event) => {
      // This input belongs to the attachment drawer ("从本机上传"), whose copy promises
      // the file is kept in the library. Composer drops and pastes do not come through here.
      await uploadLibrary([...event.target.files], { selectUploaded: true });
      event.target.value = "";
      openAttachmentDrawer();
    });
    bindComposerDropTarget();
    listen($("#scenario-current"), "click", (event) => {
      if (event.target.closest("#scenario-clear")) return;
      if (state.current) { toast(t('workspace:scenario.switchScenarioAfterStart')); return; }
      $("#scenario-heading")?.scrollIntoView({ block: "center" });
    });
    listen($("#scenario-clear"), "click", () => {
      if (state.current) { toast(t('workspace:scenario.switchScenarioAfterStart')); return; }
      applyScenario(null);
      renderScenarioPicker();
    });
    listen($("#drawer-close"), "click", () => { $("#detail-drawer").hidden = true; });
    listen($("#preview-close"), "click", closeArtifactPreview);
    listen(document, "selectionchange", () => {
      if (review.snapshot().selection && window.getSelection()?.isCollapsed) hideReviewSelection();
    });
    listen($("#form-dialog-close"), "click", () => $("#form-dialog").close());
    listen($("#form-dialog-cancel"), "click", () => $("#form-dialog").close());
    listen($("#sources-button"), "click", openSources);
    listen($("#share-button"), "click", shareChat);
    listen($("#sidebar-open"), "click", openSidebar);
    listen($("#sidebar-collapse"), "click", toggleSidebarCollapse);
    listen($("#sidebar-close"), "click", closeSidebar);
    listen($("#sidebar-scrim"), "click", closeSidebar);
    listen($("#library-upload-button"), "click", () => $("#library-upload").click());
    listen($("#library-upload"), "change", async (event) => {
      await uploadLibrary([...event.target.files]);
      event.target.value = "";
    });
    listen($("#user-menu"), "click", () => {
      const popover = $("#user-popover");
      if (popover.hidden) {
        prepareTransientSurface("user");
        popover.hidden = false;
      } else {
        popover.hidden = true;
      }
    });
    listen($("#settings-button"), "click", openConfig);
    listen($("#logout-button"), "click", async () => {
      await api("/api/auth/logout", { method: "POST" });
      closeArtifactPreview();
      review.dispose({forgetDrafts: true});
      state.reviewResearchRuns.clear();
      closeAllEventSources();
      state.backgroundRuns.clear();
      renderBackgroundRun();
      showAuth();
    });
    listen(window, "hashchange", route);
    listen(window, "resize", () => { applySidebarState(); closeChatContextMenu(); closeChatDeleteConfirm(); });
    listen(document, "scroll", () => { closeChatContextMenu(); closeChatDeleteConfirm(); }, true);
    listen(window, "beforeunload", dispose);
    [$("#search-dialog"), $("#form-dialog")].forEach((dialog) => {
      listen(dialog, "pointerdown", (event) => {
        if (event.target === dialog) dialog.close();
      });
    });
    listen(document, "pointerdown", (event) => {
      const target = event.target;
      const chatMenu = $("#chat-context-menu");
      if (chatMenu && !chatMenu.contains(target) && !target.closest?.("[data-chat-more]")) closeChatContextMenu();
      const deleteConfirm = $("#chat-delete-confirm");
      if (deleteConfirm && !deleteConfirm.contains(target)) closeChatDeleteConfirm();
      const popover = $("#user-popover");
      if (!popover.hidden && !popover.contains(target) && !$("#user-menu").contains(target)) {
        popover.hidden = true;
      }
      const drawer = $("#detail-drawer");
      const drawerTrigger = target.closest?.("#attach-button, #sources-button, [data-open-tasks]");
      if (!drawer.hidden && !drawer.contains(target) && !drawerTrigger) drawer.hidden = true;
      if (!target.closest?.(".artifact-menu-wrap")) {
        $$(".artifact-menu").forEach((menu) => { menu.hidden = true; });
      }
      const tierPicker = target.closest?.(".tier-picker");
      if (!tierPicker) {
        $("#tier-menu").hidden = true;
        $("#tier-trigger").setAttribute("aria-expanded", "false");
      }
    });
    listen(window, "resize", resizeReviewInput);
    listen(document, "keydown", (event) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") { event.preventDefault(); openSearch(); }
      if ((event.metaKey || event.ctrlKey) && event.shiftKey && event.key.toLowerCase() === "o") { event.preventDefault(); enterNewResearch(); }
      if (event.key === "Escape") {
        closeChatContextMenu();
        closeChatDeleteConfirm();
        $("#user-popover").hidden = true;
        $("#tier-menu").hidden = true;
        $("#tier-trigger").setAttribute("aria-expanded", "false");
        $("#detail-drawer").hidden = true;
        if (!$("#preview-drawer").hidden) closeArtifactPreview();
      }
    });
    $$("[data-locale]").forEach((button) => listen(button, "click", () => requestLocale(button.dataset.locale, {persist: true})));
    listen(window, "storage", (event) => {
      if (event.key !== LOCALE_STORAGE_KEY || !event.newValue) return;
      requestLocale(event.newValue, {persist: false});
    });
  }

  async function init() {
    if (started || disposed) return;
    started = true;
    localeUnsubscribe = subscribeLocale(() => rerenderForLocale());
    bindEvents();
    renderLocaleSwitch();
    // i18n 实例不再写 <html lang>：工作台初始化时按当前视图同步一次。
    syncDocumentLanguage();
    if (location.pathname.startsWith("/share/")) { await loadShared(location.pathname.split("/").pop()); return; }
    // 非分享视图的静态标注已由 bootstrap 按访问者 locale 投影、lang 也已同步：此时揭示 skip-link。
    revealSkipLink();
    // 初始视图先做一次本地化投影，避免静态标注移除后的空窗；业务状态仍未建立。
    renderHome();
    let startupError = null;
    try {
      const result = await api("/api/auth/me");
      state.user = result.user;
    } catch (err) {
      if (disposed) return;
      if (err.status === 401) { showAuth(); return; }
      startupError = err;
      state.user = { username: t('workspace:user.localUser') };
    }
    try {
      await enterApp();
    } catch (err) {
      toast(err.message || t('errors:dataLoad'));
    }
    if (startupError) toast(startupError.message || t('errors:serverUnavailable'));
  }
  const reports = createReportView({environment, request: api, notify: toast,
   context: () => ({current: state.current, sharedBundle: state.sharedBundle, shareToken: state.shareToken, activeRun: state.activeRun, sharedTranslator: sharedT}),
   prepareSurface: prepareTransientSurface,
   runs: {refresh: refreshAll, loadChat, subscribe, stop: stopRun,
     has: id => subscriptions.has(id), track: id => state.reviewResearchRuns.add(id), untrack: id => state.reviewResearchRuns.delete(id)},
  });
  const review = reports.review;
  const {open: openArtifactPreview, close: closeArtifactPreview, endpoint: artifactEndpoint, bindAnchors: bindReportAnchors, highlightSource} = reports;
  const {refresh: loadReviewWorkspace, render: renderReviewWorkspace, carry: carryReviewConversation,
   clearSelection: hideReviewSelection, captureSelection: captureReviewAnchor, resizeInput: resizeReviewInput} = review;
  const conversation = createConversationView({environment, reports: {
    open: reports.open, endpoint: reports.endpoint, bindAnchors: reports.bindAnchors, highlightSource: reports.highlightSource,
  }, actions: {
    branchMessage: branchChat, openTasks, storeArtifact: storeArtifactInLibrary, notify: toast,
    rendered: () => {
      // Drawer identity is a dataset, never display copy (D2: translated text never drives behaviour).
      if (!$("#detail-drawer").hidden && $("#detail-drawer").dataset.drawerKind === "sources") renderSourcesDrawer();
      updateRunBanner();
    },
  }});
  function renderConversation(options) { conversation.show(state.current, state.activeRun, options); }
  function scheduleConversation(options) { conversation.schedule(state.current, state.activeRun, options); }

  function dispose() {
    if (disposed) return;
    disposed = true;
    localeUnsubscribe?.();
    localeUnsubscribe = null;
    lifetime.abort();
    subscriptions.dispose();
    reports.close();
    review.dispose({forgetDrafts: true});
    clearTimeout(state.searchTimer);
    clearTimeout(state.toastTimer);
    conversation.dispose();
    state.current = null;
    state.activeRun = null;
    state.backgroundRuns.clear();
    state.reviewResearchRuns.clear();
  }
  return {
   init, dispose, loadChat, loadShared, showChat, reports, subscriptions,
   conversation: {render: renderConversation},
   research: {subscribe, stop: stopRun, track: id => state.reviewResearchRuns.add(id),
     tracked: () => [...state.reviewResearchRuns]},
   snapshot: () => ({current: state.current, activeRun: state.activeRun, shareToken: state.shareToken, sharedBundle: state.sharedBundle}),
  };
}
