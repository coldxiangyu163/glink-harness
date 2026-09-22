/** ui: product-owned browser module; no startup side effects. */

import { getLocale, t } from './i18n.mjs';

export function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;",
  })[char]);
}

export function safeUrl(value) {
  if (String(value).startsWith("#")) {
    const anchor = String(value).slice(1).replace(/[^\w\u3400-\u9fff-]/g, "-");
    return anchor ? `#${anchor}` : "#";
  }
  try {
    const url = new URL(value);
    return ["http:", "https:"].includes(url.protocol) ? url.href : "#";
  } catch {
    return "#";
  }
}

export function uiIcon(name) {
  const paths = {
    copy: '<rect x="8" y="8" width="11" height="11" rx="2"></rect><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"></path>',
    report: '<path d="M6 3h8l4 4v14H6z"></path><path d="M14 3v5h5M9 12h6M9 16h6"></path>',
    source: '<circle cx="5" cy="6" r="1.5"></circle><circle cx="5" cy="12" r="1.5"></circle><circle cx="5" cy="18" r="1.5"></circle><path d="M9 6h10M9 12h10M9 18h10"></path>',
    branch: '<circle cx="6" cy="5" r="2"></circle><circle cx="17" cy="7" r="2"></circle><circle cx="6" cy="19" r="2"></circle><path d="M6 7v10M8 10c5 0 4-3 7-3"></path>',
    share: '<circle cx="18" cy="5" r="2"></circle><circle cx="6" cy="12" r="2"></circle><circle cx="18" cy="19" r="2"></circle><path d="m8 11 8-5M8 13l8 5"></path>',
    archive: '<rect x="3" y="4" width="18" height="4" rx="1"></rect><path d="M5 8v10a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8"></path><path d="M10 12h4"></path>',
    rename: '<path d="m4 20 4.2-1 10.4-10.4a2.1 2.1 0 0 0-3-3L5.2 16z"></path><path d="m14.5 6.5 3 3"></path>',
    trash: '<path d="M4 7h16M9 7V4h6v3M7 7l1 13h8l1-13M10 11v5M14 11v5"></path>',
    bookmark: '<path d="M6 4h12v17l-6-4-6 4z"></path>',
    folder: '<path d="M3 6.5A2.5 2.5 0 0 1 5.5 4H10l2 2h6.5A2.5 2.5 0 0 1 21 8.5v8A2.5 2.5 0 0 1 18.5 19h-13A2.5 2.5 0 0 1 3 16.5z"></path>',
    eye: '<path d="M2.5 12s3.5-6 9.5-6 9.5 6 9.5 6-3.5 6-9.5 6-9.5-6-9.5-6z"></path><circle cx="12" cy="12" r="2.5"></circle>',
    download: '<path d="M12 3v12M7.5 10.5 12 15l4.5-4.5M4 19h16"></path>',
    more: '<circle cx="5" cy="12" r="1"></circle><circle cx="12" cy="12" r="1"></circle><circle cx="19" cy="12" r="1"></circle>',
    chevron: '<path d="m7 10 5 5 5-5"></path>',
  };
  return `<svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">${paths[name] || ""}</svg>`;
}

export function sourceFavicon(domain) {
  const value = String(domain || "").trim().toLowerCase();
  let hash = 0;
  for (const char of value) hash = (hash * 31 + char.codePointAt(0)) % 360;
  const initial = Array.from(value.replace(/^www\./, ""))[0]?.toUpperCase() || "?";
  const src = `/api/favicon?domain=${encodeURIComponent(value)}`;
  return `<span class="source-favicon source-favicon-tone-${hash % 8}"><img src="${src}" alt="" loading="lazy"><span class="source-favicon-fallback" hidden>${escapeHtml(initial)}</span></span>`;
}

export function formatBytes(bytes) {
  const n = Number(bytes || 0);
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

export function formatDate(timestamp, locale = null) {
  if (!timestamp) return "";
  // locale 可注入：分享页按内容语言格式化，不跟随访问者的 UI locale。
  return new Intl.DateTimeFormat(locale || getLocale(), { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(new Date(timestamp * 1000));
}

export function truncateChatTitle(value, limit = 34) {
  const title = String(value || "").trim();
  if (title.length <= limit) return title;
  const clipped = title.slice(0, limit - 1);
  const boundary = [...clipped.matchAll(/[\s，。！？；：、,.!?;:]/g)].at(-1);
  const cutAt = boundary && boundary.index >= Math.floor(limit * 0.6)
    ? boundary.index + (/\s/.test(boundary[0]) ? 0 : boundary[0].length)
    : clipped.length;
  return `${clipped.slice(0, cutAt).trimEnd()}…`;
}

export class ApiError extends Error {
  constructor(message, status = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export function apiErrorMessage(payload, status, translator = t) {
  const detail = payload?.detail;
  if (Array.isArray(detail)) {
    return detail.map((item) => {
      const location = Array.isArray(item.loc) ? item.loc.at(-1) : translator("errors:field");
      return `${location}: ${item.msg || translator("errors:invalidInput")}`;
    }).join(translator("common:listSeparator"));
  }
  // v1 detail strings stay verbatim but are labelled as server text until the
  // error protocol (phase 2) replaces them; this is not a compatibility promise.
  if (typeof detail === "string") return translator("errors:serverOriginal", { detail });
  if (typeof payload === "string" && payload.trim()) return payload;
  return translator("errors:requestFailed", { status });
}

export function chatContextMenuMarkup(chat, projects) {
  const projectItems = projects.length
    ? `${projects.map((project) => `
        <button type="button" role="menuitem" data-chat-action="move" data-project-id="${escapeHtml(project.id)}" ${project.id === chat.project_id ? "disabled" : ""}>
          <span>${escapeHtml(project.name)}</span>${project.id === chat.project_id ? `<i aria-label="${t("workspace:chatMenu.currentProject")}">✓</i>` : ""}
        </button>`).join("")}
        <span class="chat-menu-separator" aria-hidden="true"></span>
        <button type="button" role="menuitem" data-chat-action="move-out" ${chat.project_id ? "" : "disabled"}>${t("workspace:chatMenu.moveOut")}</button>`
    : `<span class="chat-menu-empty">${t("workspace:chatMenu.noProjects")}</span>`;
  return `
    <button type="button" role="menuitem" data-chat-action="copy">${uiIcon("copy")}<span>${t("workspace:chatMenu.copy")}</span></button>
    <button type="button" role="menuitem" data-chat-action="rename">${uiIcon("rename")}<span>${t("workspace:chatMenu.rename")}</span></button>
    <div class="chat-menu-parent">
      <button type="button" role="menuitem" aria-haspopup="menu" aria-expanded="false" data-chat-action="projects">${uiIcon("folder")}<span>${t("workspace:chatMenu.moveToProject")}</span><i aria-hidden="true">›</i></button>
      <div class="chat-context-submenu" role="menu" aria-label="${t("workspace:chatMenu.selectProject")}">${projectItems}</div>
    </div>
    <button type="button" role="menuitem" data-chat-action="archive">${uiIcon("archive")}<span>${t("workspace:chatMenu.archive")}</span></button>
    <button class="danger" type="button" role="menuitem" data-chat-action="delete">${uiIcon("trash")}<span>${t("common:action.delete")}</span></button>`;
}

export function chatDeleteConfirmMarkup(chat) {
  return `<section id="chat-delete-confirm" class="chat-delete-confirm" role="alertdialog" aria-labelledby="chat-delete-title" aria-describedby="chat-delete-description">
    <div class="chat-delete-copy">${uiIcon("trash")}<div><strong id="chat-delete-title">${t("workspace:chatMenu.deleteTitle", { title: escapeHtml(chat.title) })}</strong><p id="chat-delete-description">${t("workspace:chatMenu.deleteDescription")}</p></div></div>
    <div class="chat-delete-actions"><button class="button quiet" type="button" data-cancel-chat-delete>${t("common:action.cancel")}</button><button class="button confirm-danger" type="button" data-confirm-chat-delete>${t("common:action.delete")}</button></div>
  </section>`;
}

export function contextMenuPoint(event) {
  event.preventDefault();
  return { x: Number(event.clientX || 0), y: Number(event.clientY || 0) };
}
