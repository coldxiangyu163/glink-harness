import { escapeHtml, formatBytes } from './ui.mjs';
import { t } from './i18n.mjs';

import { renderMarkdown, reportCitationSources } from './markdown.mjs';

import { artifactPreviewKind, artifactResolverFor, artifactTypeLabel, isIntermediateArtifact, isPreviewableArtifact } from './artifacts.mjs';

import {createReviewWorkspace} from './review-workspace.mjs';

/** 预览加载的受控失败：message 已按视图语言本地化可直接呈现；浏览器/JS 原生错误不得进入页面。 */
class PreviewFailure extends Error {
  constructor(message) { super(message); this.name = "PreviewFailure"; }
}

/** Owns preview identity and its review child; closing it never cancels a research subscription. */
export function createReportView({environment, request, notify: toast, context, prepareSurface: prepareTransientSurface, runs}) {
  const {document, CSS, setTimeout} = environment;
  const fetch = environment.fetch.bind(environment);
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const state = {previewArtifact: null, previewGeneration: 0, previewText: null};
  const snapshot = () => ({artifactId: state.previewArtifact, generation: state.previewGeneration, text: state.previewText});
  const review = createReviewWorkspace({environment, request, notify: toast, context, runs,
   reports: {snapshot, open: openArtifactPreview, render: renderPreviewReport}});
  function previewArtifacts() {
    return context().current?.artifacts || context().sharedBundle?.artifacts || [];
  }

  /** 分享页（无登录态）的预览 chrome 走内容语言实例；工作台返回 null。 */
  function sharedTranslatorFor() {
    const translator = context().shareToken ? context().sharedTranslator : null;
    return typeof translator === "function" ? translator : null;
  }

  function reportImageArtifactUrl(reference, artifacts = previewArtifacts()) {
    const name = String(reference || "").split(/[/?#]/).filter(Boolean).at(-1);
    const artifact = artifacts.find((item) => item.name === name && item.mime.startsWith("image/"));
    return artifact ? artifactEndpoint(artifact.id, true) : "";
  }

  function artifactEndpoint(artifactId, inline = true) {
    const suffix = inline ? "?inline=true" : "";
    return context().shareToken
      ? `/api/public/shares/${encodeURIComponent(context().shareToken)}/artifacts/${artifactId}${suffix}`
      : `/api/artifacts/${artifactId}${suffix}`;
  }

  async function openArtifactPreview(artifactId) {
    const shareTranslator = sharedTranslatorFor();
    const pageT = shareTranslator || t;
    const artifactSet = previewArtifacts();
    const artifact = artifactSet.find((item) => item.id === artifactId);
    if (!artifact) { toast(pageT("report:preview.notFound")); return; }
    // 不可内联渲染的格式（如 DOCX）不打开预览，只在交付文件区提供下载。
    const previewKind = artifactPreviewKind(artifact);
    if (!previewKind) { toast(pageT("report:preview.unsupported")); return; }
    prepareTransientSurface("preview");
    // Each opening owns its DOM and citation IDs, including reopening the same file.
    const previewGeneration = ++state.previewGeneration;
    state.previewArtifact = artifact.id;
    state.previewText = null;
    review.resetSession();
    $("#detail-drawer").hidden = true;
    $("#preview-title").textContent = artifact.name;
    $("#preview-title").title = artifact.name;
    $("#preview-meta").textContent = `${/\.md$/i.test(artifact.name) ? "Markdown" : artifactTypeLabel(artifact, pageT)} · ${formatBytes(artifact.size)}`;
    $("#preview-download").href = artifactEndpoint(artifact.id, false);
    // 分享页的预览 chrome 用内容语言实例；工作台预览仍用全局实例。
    if (shareTranslator) review.setDownload(shareTranslator("report:preview.download"));
    else review.setDownload();
    $("#preview-body").innerHTML = `<p class="empty-note">${escapeHtml(pageT("report:preview.loading"))}</p>`;
    $("#preview-drawer").hidden = false;
    const reviewable = !context().shareToken && Boolean(context().current) && /\.md$/i.test(artifact.name);
    $("#preview-drawer").classList.toggle("review-workspace", reviewable);
    $("#app-shell").classList.toggle("review-open", reviewable);

    $("#review-knowledge").hidden = true;
    $("#review-rail").hidden = !reviewable;
    $("#review-rail").innerHTML = reviewable ? `<p class="empty-note">${escapeHtml(pageT("report:preview.reviewLoading"))}</p>` : "";

    $("#app-shell").classList.add("preview-open");
    if (reviewable) review.refresh(previewGeneration);
    renderPreviewFiles();
    const endpoint = artifactEndpoint(artifact.id, true);
    try {
      if (previewKind === "pdf") {
        $("#preview-body").innerHTML = `<iframe title="${escapeHtml(artifact.name)}" src="${endpoint}"></iframe>`;
      } else if (previewKind === "text") {
        let response;
        try {
          response = await fetch(endpoint, { credentials: "same-origin" });
        } catch {
          // 网络中断/abort 的浏览器原生消息（如 Failed to fetch）不得进入页面。
          throw new PreviewFailure(pageT("errors:connect"));
        }
        if (!response.ok) throw new PreviewFailure(pageT("report:preview.loadFailed", { status: response.status }));
        let text;
        try {
          text = await response.text();
        } catch {
          // 响应体读取中途失败：同样只能呈现受控文案。
          throw new PreviewFailure(pageT("errors:connect"));
        }
        if (previewGeneration !== state.previewGeneration) return;
        if (/\.md$/i.test(artifact.name) || artifact.mime === "text/markdown") {
          state.previewText = text;
          renderPreviewReport(review.snapshot().data?.draft?.content ?? text);
          if (reviewable && review.snapshot().data) review.render();
        } else {
          $("#preview-body").innerHTML = `<pre>${escapeHtml(text)}</pre>`;
        }
      } else {
        $("#preview-body").innerHTML = `<div class="preview-placeholder"><img class="preview-image" src="${endpoint}" alt="${escapeHtml(artifact.name)}"></div>`;
      }
    } catch (err) {
      if (previewGeneration !== state.previewGeneration) return;
      // 只呈现受控文案：未预期的失败（含原生异常）统一兜底，绝不透出 err.message。
      const message = err instanceof PreviewFailure ? err.message : pageT("errors:dataLoad");
      $("#preview-body").innerHTML = `<p class="empty-note">${escapeHtml(message)}</p>`;
    }
  }

  function renderPreviewReport(text) {
    const artifact = previewArtifacts().find((item) => item.id === state.previewArtifact);
    if (!artifact) return;
    const runArtifacts = previewArtifacts().filter((item) => item.run_id === artifact.run_id);
    const artifactResolver = artifactResolverFor(runArtifacts, (item) => artifactEndpoint(item.id, false));
    const imageResolver = (reference) => reportImageArtifactUrl(reference, runArtifacts);
    const citationSources = reportCitationSources(text);
    const citationPrefix = `preview-${state.previewGeneration}`;
    // 分享页的预览正文属于内容面：其 UI 兑底 copy（引用 aria/tooltip、图表兜底等）
    // 用分享页的内容语言实例，不跟随访问者的 UI locale。
    const sharedTranslator = sharedTranslatorFor();
    const body = $("#preview-body"), scrollTop = body.scrollTop;
    review.clearSelection();
    body.innerHTML = `<article class="report">${renderMarkdown(text, { imageResolver, artifactResolver, citationPrefix, citationSources, ...(sharedTranslator ? { translator: sharedTranslator } : {}) })}</article>`;
    bindReportAnchors(body);
    body.onmouseup = review.captureSelection; body.onkeyup = review.captureSelection; body.onscroll = review.clearSelection;
    body.scrollTop = scrollTop;
    review.highlight();
  }

  function highlightSource(target) {
    if (!target) return;
    target.classList.remove("source-highlight");
    void target.offsetWidth;
    target.classList.add("source-highlight");
    setTimeout(() => target.classList.remove("source-highlight"), 1800);
  }

  function bindReportAnchors(root) {
    $$('a[href^="#"]', root).forEach((link) => link.onclick = (event) => {
      event.preventDefault();
      const id = decodeURIComponent(link.getAttribute("href").slice(1));
      const target = root.querySelector(`#${CSS.escape(id)}`);
      target?.scrollIntoView({ block: "center", behavior: "smooth" });
      highlightSource(target);
    });
  }

  function renderPreviewFiles() {
    const artifacts = previewArtifacts();
    const selected = artifacts.find((item) => item.id === state.previewArtifact);
    $("#preview-files").innerHTML = artifacts
      // Formats belong to one published run; unowned legacy files cannot imply a version group.
      .filter((item) => selected?.run_id ? item.run_id === selected.run_id : item.id === selected?.id)
      .filter((item) => !isIntermediateArtifact(item))
      .filter(isPreviewableArtifact)
      .map((item) => `
      <button class="preview-file ${item.id === state.previewArtifact ? "active" : ""}" data-preview-file="${item.id}">${escapeHtml(item.name)}</button>
    `).join("");
    $$('[data-preview-file]').forEach((button) => button.onclick = () => openArtifactPreview(button.dataset.previewFile));
  }

  function closeArtifactPreview() {
    const pageT = sharedTranslatorFor() || t;
    state.previewGeneration += 1;
    state.previewArtifact = null;
    review.resetSession();
    $("#review-rail").innerHTML = "";
    $("#review-rail").hidden = true;

    $("#review-knowledge").hidden = true;
    state.previewText = null;
    $("#preview-drawer").classList.remove("review-workspace");
    $("#preview-drawer").hidden = true;
    $("#app-shell").classList.remove("preview-open", "review-open");
    $("#preview-body").innerHTML = `<p class="empty-note">${escapeHtml(pageT("report:preview.loading"))}</p>`;
  }
  return {snapshot, review, open: openArtifactPreview, close: closeArtifactPreview,
   endpoint: artifactEndpoint, bindAnchors: bindReportAnchors, highlightSource};
}
