/** artifacts: product-owned browser module; no startup side effects. */
import { escapeHtml, formatBytes, formatDate, truncateChatTitle, uiIcon } from './ui.mjs';
import { t } from './i18n.mjs';

export function isIntermediateArtifact(item) {
  return /\.png$/i.test(String(item?.name || ""));
}

export function artifactPreviewKind(item) {
  const name = String(item?.name || "").toLowerCase();
  const mime = String(item?.mime || "").toLowerCase();
  if (/\.(docx?|pptx?|xlsx?)$/.test(name) || /(wordprocessingml|presentationml|spreadsheetml)/.test(mime)) return "";
  if (mime === "application/pdf" || name.endsWith(".pdf")) return "pdf";
  if (mime.startsWith("text/") || /\.(md|txt|csv|json)$/.test(name)) return "text";
  if (mime.startsWith("image/")) return "image";
  return "";
}

export function isPreviewableArtifact(item) {
  return artifactPreviewKind(item) !== "";
}

export function artifactTypeLabel(item, translator = t) {
  const name = String(item?.name || "");
  if (name === "Final Report.md") return translator("report:artifact.typeFinalReport");
  const extension = name.includes(".") ? name.split(".").pop().toUpperCase() : "";
  return ["DOCX", "PDF"].includes(extension)
    ? extension
    : String(item?.mime || "");
}

export function reportOrder(item) {
  const name = String(item?.name || "");
  if (name === "Final Report.md") return 0;
  const extension = name.includes(".") ? `.${name.split(".").pop().toLowerCase()}` : "";
  return ({ ".docx": 1, ".pdf": 2 })[extension] ?? 9;
}

function artifactBasename(path) {
  const encoded = String(path || "").split(/[?#]/, 1)[0];
  let decoded;
  try {
    decoded = decodeURIComponent(encoded);
  } catch {
    decoded = encoded;
  }
  return decoded.replace(/\\/g, "/").split("/").filter(Boolean).at(-1) || "";
}

export function artifactResolverFor(artifacts, endpointFor) {
  const available = Array.isArray(artifacts) ? artifacts : [];
  return (path) => {
    const name = artifactBasename(path);
    const artifact = name ? available.find((item) => item.name === name) : null;
    return artifact ? endpointFor(artifact) : "";
  };
}

export function pdfDegradationBadge(qa) {
  if (!qa) return "";
  // delivery_status is authoritative: passed=false with delivered=true means
  // advisory-only warnings, not a missing PDF.
  const notDelivered = qa.delivery_status === "not_delivered";
  if (!notDelivered && !qa.degraded && qa.passed !== false) return "";
  const warnings = Array.isArray(qa.warnings) ? qa.warnings.filter(Boolean) : [];
  // 服务端 delivery_label（第 3 期键化）是内容：所有状态都优先原样呈现（跟随报告语言），
  // 仅旧数据缺失该字段时才按状态回落 UI 兜底文案。
  const fallbackLabel = notDelivered ? t("report:pdf.notDelivered") : t("report:pdf.degraded");
  const label = typeof qa.delivery_label === "string" && qa.delivery_label
    ? qa.delivery_label
    : fallbackLabel;
  const detail = warnings.join(t("report:pdf.warningSeparator")) || qa.detail || label;
  const stateClass = notDelivered ? " pdf-not-delivered-badge" : "";
  return `<span class="pdf-degraded-badge${stateClass}" role="status" data-tip="${escapeHtml(detail)}">${escapeHtml(label)}</span>`;
}

export function artifactMenuMarkup(item) {
  const moreLabel = escapeHtml(t("report:artifact.moreActions"));
  return `<span class="artifact-menu-wrap"><button class="artifact-more" data-artifact-menu="${item.id}" aria-label="${moreLabel}" data-tip="${moreLabel}">${uiIcon("more")}</button><span class="artifact-menu" data-artifact-menu-popover="${item.id}" hidden>${isPreviewableArtifact(item) ? `<button type="button" data-preview-artifact="${item.id}">${uiIcon("eye")}<span>${escapeHtml(t("report:artifact.preview"))}</span></button>` : ""}<a href="/api/artifacts/${item.id}">${uiIcon("download")}<span>${escapeHtml(t("report:artifact.download"))}</span></a><button type="button" data-store-artifact="${item.id}">${uiIcon("bookmark")}<span>${escapeHtml(t("report:artifact.storeToLibrary"))}</span></button></span></span>`;
}

export function groupArtifactsByRun(artifacts, runs = []) {
  const runList = Array.isArray(runs) ? runs : [];
  const runPositions = new Map(runList.map((run, index) => [String(run.id), index]));
  const runsById = new Map(runList.map((run) => [String(run.id), run]));
  const groupsByRun = new Map();
  (Array.isArray(artifacts) ? artifacts : [])
    .filter((item) => !isIntermediateArtifact(item))
    .forEach((item, artifactIndex) => {
      const runId = String(item.run_id || "");
      const key = runId || "__unassigned__";
      if (!groupsByRun.has(key)) {
        groupsByRun.set(key, {
          runId,
          run: runsById.get(runId) || null,
          runNumber: runPositions.has(runId) ? runPositions.get(runId) + 1 : null,
          artifactIndex,
          artifacts: [],
        });
      }
      groupsByRun.get(key).artifacts.push(item);
    });
  const groups = [...groupsByRun.values()];
  groups.forEach((group) => group.artifacts.sort(
    (left, right) => reportOrder(left) - reportOrder(right),
  ));
  groups.sort((left, right) => {
    if (left.runNumber !== null && right.runNumber !== null) {
      return right.runNumber - left.runNumber;
    }
    if (left.runNumber !== null) return -1;
    if (right.runNumber !== null) return 1;
    return left.artifactIndex - right.artifactIndex;
  });
  return groups;
}

export function conversationArtifactLayout(messages, artifacts, runs = []) {
  const messageList = Array.isArray(messages) ? messages : [];
  const groups = groupArtifactsByRun(artifacts, runs);
  const lastAssistantIndexByRun = new Map();
  messageList.forEach((message, index) => {
    const runId = String(message.run_id || "");
    if (message.role === "assistant" && runId) lastAssistantIndexByRun.set(runId, index);
  });

  const groupsAfterMessage = new Map();
  const unassignedArtifacts = [];
  groups.forEach((group) => {
    const messageIndex = lastAssistantIndexByRun.get(group.runId);
    const runIsActive = ["queued", "running"].includes(group.run?.status);
    if (group.run && messageIndex !== undefined && !runIsActive) {
      groupsAfterMessage.set(messageIndex, group);
    } else if (!runIsActive) {
      unassignedArtifacts.push(...group.artifacts);
    }
  });
  const unassignedGroup = unassignedArtifacts.length
    ? {
        runId: "",
        run: null,
        runNumber: null,
        artifactIndex: groups.length,
        artifacts: unassignedArtifacts.sort(
          (left, right) => reportOrder(left) - reportOrder(right),
        ),
      }
    : null;
  return {
    groupsAfterMessage,
    unassignedGroup,
    groupCount: groupsAfterMessage.size + (unassignedGroup ? 1 : 0),
  };
}

function artifactRunPresentation(group, translator = t, locale = null) {
  const run = group.run || {};
  const statusKey = ({
    completed: "report:artifact.runStatusCompleted",
    failed: "report:artifact.runStatusFailed",
    cancelled: "report:artifact.runStatusCancelled",
    running: "report:artifact.runStatusRunning",
    queued: "report:artifact.runStatusQueued",
  })[run.status];
  const status = statusKey ? translator(statusKey) : translator("report:artifact.runStatusUnknown");
  const completedAt = run.ended_at ? formatDate(run.ended_at, locale) : "";
  const promptKind = translator(group.runNumber > 1 ? "report:artifact.promptKindFollowUp" : "report:artifact.promptKindFirst");
  return {
    title: group.runNumber
      ? translator("report:artifact.groupTitleRound", { round: group.runNumber })
      : translator("report:artifact.groupTitleLegacy"),
    prompt: translator("report:artifact.groupPrompt", {
      kind: promptKind,
      prompt: truncateChatTitle(run.prompt, 48) || translator("report:artifact.missingPrompt"),
    }),
    meta: [status, completedAt].filter(Boolean).join(" · "),
  };
}

function renderArtifactGroupHeader(group, translator = t, locale = null) {
  const presentation = artifactRunPresentation(group, translator, locale);
  return `<header class="artifact-group-head"><div class="artifact-group-copy"><strong>${escapeHtml(presentation.title)}</strong><span>${escapeHtml(presentation.prompt)}</span></div><small>${escapeHtml(presentation.meta)}</small></header>`;
}

export function renderArtifactGroups(groups, renderRow, showGroupHeaders = groups.length > 1, translator = t, locale = null) {
  return groups.map((group) => `<div class="artifact-group" data-artifact-run-id="${escapeHtml(group.runId)}">${showGroupHeaders ? renderArtifactGroupHeader(group, translator, locale) : ""}${group.artifacts.map(renderRow).join("")}</div>`).join("");
}

export function primaryArtifactForGroups(groups) {
  const latestGroup = groups[0];
  return latestGroup?.artifacts.find((item) => item.name === "Final Report.md")
    || latestGroup?.artifacts[0];
}

export function deliveryArtifactsForCard(group) {
  const artifacts = Array.isArray(group?.artifacts) ? group.artifacts : [];
  const visible = artifacts.filter((item) => !/\.docx$/i.test(String(item?.name || "")));
  return visible.length ? visible : artifacts;
}

function artifactExtension(item) {
  const name = String(item?.name || "");
  return name.includes(".") ? name.split(".").pop().toLowerCase() : "";
}

function reportFormatArtifact(artifact, artifacts, extension) {
  if (artifactExtension(artifact) === extension) return artifact;
  const runId = String(artifact?.run_id || "");
  return (Array.isArray(artifacts) ? artifacts : []).find((item) => (
    String(item.run_id || "") === runId && artifactExtension(item) === extension
  )) || null;
}

const REPORT_DOWNLOAD_FORMATS = [
  ["md", "report:artifact.downloadFormatMd"],
  ["docx", "report:artifact.downloadFormatDocx"],
  ["pdf", "report:artifact.downloadFormatPdf"],
];

function pdfQaForRun(runs, runId) {
  const run = runs.find((item) => item.id === runId);
  if (!run) return null;
  const event = [...(run.events || [])].reverse().find((item) => item.type === "report_validation");
  return run.validation?.pdf_qa || event?.data?.pdf_qa || null;
}

export function reportDownloadMenuMarkup(artifact, artifacts, endpointFor, translator = t) {
  // 公开前即可发现 Word：只有同一 run/版本真实存在 DOCX 时才提示。
  const hasWord = Boolean(reportFormatArtifact(artifact, artifacts, "docx"));
  const toggleLabel = escapeHtml(translator(hasWord ? "report:artifact.downloadWithWord" : "report:artifact.download"));
  const items = REPORT_DOWNLOAD_FORMATS.map(([extension, labelKey]) => {
    const target = reportFormatArtifact(artifact, artifacts, extension);
    return target
      ? `<a href="${endpointFor(target.id, false)}" data-download-format="${extension}">${uiIcon("download")}<span>${escapeHtml(translator(labelKey))}</span></a>`
      : "";
  }).join("");
  if (!items) return "";
  return `<span class="artifact-menu-wrap artifact-download-wrap"><button type="button" class="artifact-row-button artifact-download-toggle" data-download-menu="${artifact.id}" aria-haspopup="true" aria-expanded="false" aria-label="${toggleLabel}">${uiIcon("download")}<span>${toggleLabel}</span>${uiIcon("chevron")}</button><span class="artifact-menu artifact-download-menu" data-download-menu-popover="${artifact.id}" hidden>${items}</span></span>`;
}

export function renderArtifactCard(group, {runs, endpointFor, showGroupHeader = false}) {
  const visibleGroup = { ...group, artifacts: deliveryArtifactsForCard(group) };
  const primary = primaryArtifactForGroups([visibleGroup]);
  const runArtifacts = Array.isArray(group?.artifacts) ? group.artifacts : [];
  const latestQa = group.runId ? pdfQaForRun(runs, group.runId) : null;
  return `<section class="artifacts artifacts-inline"><div class="artifacts-head"><div class="artifacts-title">${uiIcon("folder")}<h3>${escapeHtml(t("report:artifact.deliveryTitle"))}</h3>${pdfDegradationBadge(latestQa)}</div><div class="artifact-head-actions"><span class="artifact-count">${visibleGroup.artifacts.length}</span>${primary && isPreviewableArtifact(primary) ? `<button class="button artifact-head-button" data-preview-artifact="${primary.id}" aria-label="${escapeHtml(t("report:artifact.preview"))}" data-tip="${escapeHtml(t("report:artifact.preview"))}">${uiIcon("eye")}<span>${escapeHtml(t("report:artifact.preview"))}</span></button>` : ""}</div></div>${renderArtifactGroups([visibleGroup], (item) => {
    const extension = item.name.includes(".") ? item.name.split(".").pop().toLowerCase() : "file";
    const type = ["md", "docx", "pdf"].includes(extension) ? extension : "other";
    const isPrimary = item.id === primary?.id;
    const downloadMenu = isPrimary ? reportDownloadMenuMarkup(item, runArtifacts, endpointFor) : "";
    const actions = isPrimary
      ? `<span class="artifact-actions">${downloadMenu || `<a class="artifact-row-button" href="/api/artifacts/${item.id}">${uiIcon("download")}<span>${escapeHtml(t("report:artifact.download"))}</span></a>`}${isPreviewableArtifact(item) ? `<button class="artifact-row-button artifact-primary-action" data-preview-artifact="${item.id}">${escapeHtml(t("report:artifact.openReport"))}</button>` : ""}${artifactMenuMarkup(item)}</span>`
      : `<span class="artifact-actions">${artifactMenuMarkup(item)}</span>`;
    const degraded = extension === "pdf" ? pdfDegradationBadge(pdfQaForRun(runs, item.run_id)) : "";
    return `<div class="artifact-row"><span class="file-icon file-icon-${type}">${uiIcon("report")}</span><span class="artifact-copy"><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(artifactTypeLabel(item))} · ${formatBytes(item.size)}</small></span>${degraded}${actions}</div>`;
  }, showGroupHeader)}</section>`;
}

export function bindDownloadMenus(root, {signal} = {}) {
  const $ = selector => root.querySelector(selector);
  const $$ = selector => [...root.querySelectorAll(selector)];
  $$('[data-download-menu]').forEach((button) => button.addEventListener("click", () => {
    const menu = $(`[data-download-menu-popover="${button.dataset.downloadMenu}"]`);
    if (!menu) return;
    $$('[data-artifact-menu-popover]').concat($$('[data-download-menu-popover]')).forEach((item) => {
      if (item !== menu) item.hidden = true;
    });
    menu.hidden = !menu.hidden;
    button.setAttribute("aria-expanded", String(!menu.hidden));
  }, {signal}));
  $$('[data-download-format]').forEach((link) => link.addEventListener("click", () => {
    link.closest(".artifact-menu")?.setAttribute("hidden", "");
  }, {signal}));
}
