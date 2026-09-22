/** review-anchors: product-owned browser module; no startup side effects. */
import { t } from './i18n.mjs';

/** Longest excerpt a selection anchor may carry; mirrors `deploy/local_web/limits.py`. */
export const MAX_ANCHOR_EXCERPT = 55000;

const isHighSurrogate = (unit) => unit >= 0xd800 && unit <= 0xdbff;
const isLowSurrogate = (unit) => unit >= 0xdc00 && unit <= 0xdfff;

/** Drop a lone surrogate at the requested edge before counting code points. */
function dropSplitSurrogate(text, side) {
  if (!text) return text;
  if (side === "end" && isHighSurrogate(text.charCodeAt(text.length - 1))) return text.slice(0, -1);
  if (side === "start" && isLowSurrogate(text.charCodeAt(0))) return text.slice(1);
  return text;
}

function firstCodePoints(text, limit) {
  return Array.from(dropSplitSurrogate(text, "start")).slice(0, limit).join("");
}

function lastCodePoints(text, limit) {
  return Array.from(dropSplitSurrogate(text, "end")).slice(-limit).join("");
}

export function selectionReviewAnchor(root, selection) {
  if (!selection?.rangeCount || selection.isCollapsed || !root?.contains(selection.anchorNode) || !root.contains(selection.focusNode)) return null;
  const range = selection.getRangeAt(0);
  const raw = range.toString();
  const trimmed = dropSplitSurrogate(dropSplitSurrogate(raw.trim(), "start"), "end");
  if (!trimmed) return null;
  // Over-limit selections are rejected, never silently shortened: a truncated
  // quote would discuss different text than the user actually selected.
  if (Array.from(trimmed).length > MAX_ANCHOR_EXCERPT) return { error: "excerpt_too_long" };
  const excerpt = trimmed;
  const before = range.cloneRange();
  before.selectNodeContents(root);
  before.setEnd(range.startContainer, range.startOffset);
  const start = before.toString().length + raw.indexOf(trimmed);
  let heading = "";
  root.querySelectorAll("h1,h2,h3,h4,h5,h6").forEach((node) => {
    if (node.contains(range.startContainer) || (node.compareDocumentPosition(range.startContainer) & 4)) heading = node.textContent;
  });
  const documentText = root.textContent;
  const end = start + excerpt.length;
  return { heading: firstCodePoints(heading, 300), excerpt,
    prefix: lastCodePoints(documentText.slice(0, start), 200),
    suffix: firstCodePoints(documentText.slice(end), 200) };
}

export function locateReviewExcerpt(text, anchor) {
  if (!anchor?.excerpt) return null;
  const matches = [];
  for (let at = text.indexOf(anchor.excerpt); at !== -1; at = text.indexOf(anchor.excerpt, at + 1)) matches.push(at);
  // A unique quote is safe. Repeated quotes require exact surrounding evidence.
  const candidates = matches.length === 1 ? matches : matches.filter((at) =>
    (!anchor.prefix || text.slice(0, at).endsWith(anchor.prefix)) &&
    (!anchor.suffix || text.slice(at + anchor.excerpt.length).startsWith(anchor.suffix)));
  return candidates.length === 1 ? { start: candidates[0], end: candidates[0] + anchor.excerpt.length } : null;
}

export function markReviewAnchor(root, anchor, threadId, title) {
  const match = locateReviewExcerpt(root.textContent, anchor);
  if (!match) return false;
  const doc = root.ownerDocument;
  const walker = doc.createTreeWalker(root, 4);
  const segments = [];
  let node, offset = 0;
  while ((node = walker.nextNode())) {
    const end = offset + node.textContent.length;
    if (end > match.start && offset < match.end) segments.push({ node, start: Math.max(0, match.start - offset), end: Math.min(node.textContent.length, match.end - offset) });
    offset = end;
  }
  // One Range per text node preserves links, citation nodes and their listeners.
  for (const segment of segments.reverse()) {
    const range = doc.createRange();
    range.setStart(segment.node, segment.start);
    range.setEnd(segment.node, segment.end);
    const mark = doc.createElement("mark");
    mark.className = "review-highlight";
    mark.dataset.threadId = threadId;
    mark.setAttribute("tabindex", "0");
    mark.setAttribute("role", "button");
    mark.setAttribute("aria-label", t("review:anchor.open", {
      title: title || anchor.heading || t("review:anchor.fallbackTitle"),
    }));
    range.surroundContents(mark);
  }
  return segments.length > 0;
}

export function bindReviewHighlight(mark, openThread, selection = () => globalThis.getSelection()) {
  const open = (event) => {
    if (event.type === "click" && selection()?.isCollapsed === false) return;
    // Native link/citation activation must keep its destination and existing handler.
    // Keyboard activation on the focusable mark opens only the discussion.
    if (event.type !== "click" || !mark.closest("a")) { event.preventDefault(); event.stopPropagation(); }
    openThread(mark.dataset.threadId);
  };
  mark.onclick = open;
  mark.onkeydown = (event) => { if (["Enter", " "].includes(event.key)) open(event); };
}

export function reviewVersionForArtifact(data, artifactId) {
  return data?.versions?.find((version) => version.artifact_id === artifactId) || null;
}

export function reviewThreadReadOnly(thread) {
  return ["rejected", "resolved", "closed"].includes(thread?.status);
}

export function reviewThreadsForVersion(data, version) {
  // Projection is server-owned. Never infer resolution from a changed quote.
  return (data.threads || []).filter((thread) => thread.version_id === version.id ||
    (data.versions[0]?.id === version.id && ["mapped", "relink_required"].includes(thread.projection_status)) ||
    thread.resolved_version_id === version.id);
}

// 状态码 → 显示文案的登记表：判定只读协议值，未知值原样呈现。
const THREAD_STATUS_LABEL_KEYS = Object.freeze({
  open: "review:status.open",
  needs_evidence: "review:status.needsEvidence",
  change_proposed: "review:status.changeProposed",
  rejected: "review:status.rejected",
  resolved: "review:status.resolved",
  closed: "review:status.closed",
});

export function reviewThreadStatus(thread, data, locations = new Map()) {
  const resolved = data.versions.find((item) => item.id === thread.resolved_version_id);
  if (resolved) return t("review:status.resolvedAt", { number: resolved.number });
  const projection = locations.get(thread.id) || thread.projection_status;
  if (projection === "relink_required") return t("review:status.relinkRequired");
  const statusKey = THREAD_STATUS_LABEL_KEYS[thread.status];
  const label = statusKey ? t(statusKey) : thread.status || t("review:status.open");
  return `${label}${projection === "mapped" ? t("review:status.mapped") : ""}`;
}

