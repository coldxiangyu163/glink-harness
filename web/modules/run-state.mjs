/** run-state: product-owned browser module; no startup side effects. */
import { escapeHtml } from './ui.mjs';
import { citationSourceUrl } from './markdown.mjs';
import { t } from './i18n.mjs';

// 子 agent 状态是协议枚举：判定只读集合，显示才查资源；t() 返回值不参与业务判断。
const SUBAGENT_STATUS_LABEL_KEYS = Object.freeze({
  created: "run:subagent.statusCreated",
  queued: "run:subagent.statusQueued",
  running: "run:subagent.statusRunning",
  reported: "run:subagent.statusReported",
});
const SUBAGENT_STATUS_ORDER = Object.freeze(["running", "queued", "reported", "created"]);

function subagentStatusLabel(status) {
  const key = SUBAGENT_STATUS_LABEL_KEYS[status];
  return key ? t(key) : "";
}

export const terminalTypes = new Set(["run_completed", "run_failed", "run_cancelled"]);

const eventTypes = [
  "queued", "run_started", "assistant_delta", "progress_note", "thinking_block", "activity_started",
  "activity_finished", "artifact_created", "task_board_updated", "subagent_status", "warning",
  "workflow_stage", "orchestration_stage", "report_validation", "artifact_preview_ready",
  "run_completed", "run_failed", "run_cancelled",
];

export function runOutlineItems(runs, messages, promptLimit = 12) {
  const orderedRuns = Array.isArray(runs) ? runs : [];
  if (orderedRuns.length < 2) return [];
  const availableMessages = Array.isArray(messages) ? messages : [];
  const limit = Math.max(1, Number(promptLimit) || 12);
  const circledNumbers = [
    "①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨", "⑩",
    "⑪", "⑫", "⑬", "⑭", "⑮", "⑯", "⑰", "⑱", "⑲", "⑳",
  ];
  return orderedRuns.map((run, index) => {
    const runId = String(run.id || "");
    const matchingMessages = availableMessages.filter((message) => (
      String(message.run_id || "") === runId
    ));
    const anchor = matchingMessages.find((message) => message.role === "user")
      || matchingMessages.find((message) => message.role === "assistant")
      || null;
    const prompt = String(run.prompt || "").replace(/\s+/g, " ").trim() || t("run:outline.missingProblem");
    const characters = Array.from(prompt);
    const promptSummary = characters.length > limit
      ? `${characters.slice(0, limit).join("")}…`
      : prompt;
    const kind = t(index === 0 ? "run:outline.kindFirst" : "run:outline.kindFollowUp");
    const indexLabel = circledNumbers[index] || `${index + 1}.`;
    return {
      runId,
      anchorMessageId: anchor ? String(anchor.id || "") : "",
      anchorRole: anchor?.role || "",
      inProgress: ["queued", "running"].includes(run.status),
      indexLabel,
      label: t("run:outline.label", { index: indexLabel, kind, summary: promptSummary }),
      kind,
      prompt,
      promptSummary,
    };
  });
}

export function renderRunOutline(items) {
  if (!Array.isArray(items) || items.length < 2) return "";
  const tabs = items.map((item, index) => {
    const disabled = !item.anchorMessageId;
    const status = item.inProgress ? '<span class="pulse-dot" aria-hidden="true"></span>' : "";
    const current = index === 0 ? ' aria-current="step"' : "";
    const clauses = [];
    if (item.inProgress) clauses.push(t("run:outline.inProgress"));
    if (disabled) clauses.push(t("run:outline.unanchored"));
    const suffix = clauses.map((clause) => `${t("run:outline.clauseSeparator")}${clause}`).join("");
    const ariaLabel = `${t("run:outline.jump", { index: index + 1, kind: item.kind, prompt: item.prompt })}${suffix}`;
    return `<button class="run-outline-tab" type="button" data-run-outline-run="${escapeHtml(item.runId)}" data-run-outline-target="${escapeHtml(item.anchorMessageId)}" aria-label="${escapeHtml(ariaLabel)}"${current}${disabled ? " disabled" : ""}>${status}<span class="run-outline-index" aria-hidden="true">${escapeHtml(item.indexLabel)}</span><span class="run-outline-copy">${escapeHtml(t("run:outline.tabLabel", { kind: item.kind, summary: item.promptSummary }))}</span></button>`;
  }).join("");
  return `<nav id="run-outline" class="run-outline" aria-label="${escapeHtml(t("run:outline.navAria"))}"><div class="run-outline-scroller">${tabs}</div></nav>`;
}

export function verificationErrorForRun(run) {
  const latest = validRunEvents(run?.events || []).reverse();
  const terminal = latest.find((event) => /^run_(completed|failed|cancelled)$/.test(event.type));
  if (run?.answer_status === "verification_error"
    || terminal?.data?.answer_status === "verification_error"
    || terminal?.data?.verification_status === "error") return true;
  const verdict = latest.find((event) => event.type === "workflow_stage"
    && (event.data?.stage === "verification_verdict" || event.data?.status === "error"));
  return verdict?.data?.status === "error" || verdict?.data?.verdict === "error";
}

export function verificationFailedForRun(run) {
  if (verificationErrorForRun(run)) return false;
  const latest = validRunEvents(run?.events || []).reverse();
  const terminal = latest.find((event) => /^run_(completed|failed|cancelled)$/.test(event.type));
  if (run?.answer_status === "verification_failed" || terminal?.data?.answer_status === "verification_failed"
    || terminal?.data?.verification_failed === true) return true;
  const verdict = latest.find((event) => event.type === "workflow_stage"
    && (event.data?.stage === "verification_verdict" || event.data?.exhausted));
  return verdict?.data?.verdict !== "pass" && verdict?.data?.exhausted === true;
}

const VERIFICATION_ADVISORY = "advisory";
const VERIFICATION_ERROR = "error";
const auditBlockerReasons = new Set(["too_many_blockers", "contains_unrepairable_blocker"]);

// The audit is the only channel that may explain a fail as "prompts only".
// Unknown, missing or inconsistent values stay untrusted and fail closed.
function auditSafeCount(value) {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? value : null;
}

// ``verdict`` only feeds the producer's separate top-level ``repair_reason``:
// a blocker-only reason next to zero blockers is a contradiction, not a neutral label.
export function verificationAuditCounts(audit, verdict = null) {
  if (!audit || typeof audit !== "object" || Array.isArray(audit)) return null;
  if (audit.schema_version !== 1) return null;
  const counts = audit.counts;
  if (!counts || typeof counts !== "object" || Array.isArray(counts)) return null;
  const issueCount = auditSafeCount(counts.issue_count);
  const blockerCount = auditSafeCount(counts.blocker_count);
  if (issueCount === null || blockerCount === null) return null;
  if (issueCount <= 0 || blockerCount > issueCount) return null;
  const decision = audit.decision;
  const reason = decision && typeof decision === "object" && !Array.isArray(decision)
    ? String(decision.reason || "") : "";
  const repairReason = verdict && typeof verdict === "object" && !Array.isArray(verdict)
    ? String(verdict.repair_reason || "") : "";
  if (blockerCount === 0 && (auditBlockerReasons.has(reason) || auditBlockerReasons.has(repairReason))) return null;
  return {issueCount, blockerCount};
}

// Per-event projection for the process record. Terminal conflict is handled by
// verificationPresentationForRun, which also sees the run's terminal fields.
export function verificationStageCategory(data) {
  const verdict = data && typeof data === "object" ? data : {};
  if (verdict.status === "error" || verdict.verdict === "error") return "error";
  if (verdict.verdict === "fail") {
    const counts = verificationAuditCounts(verdict.verification_audit, verdict);
    return counts && counts.blockerCount === 0 ? VERIFICATION_ADVISORY : "failed";
  }
  if (verdict.verdict === "pass") return "passed";
  return "not_run";
}

export function verificationPresentationForRun(run) {
  const events = validRunEvents(run?.events || []);
  const latest = [...events].reverse();
  const terminal = latest.find((event) => /^run_(completed|failed|cancelled)$/.test(event.type));
  // Only the latest legitimate verdict may read the audit. Other stages never
  // explain a fail as prompts-only, even when they carry exhausted/audit data.
  const verdict = latest.find((event) => event.type === "workflow_stage"
    && event.data?.stage === "verification_verdict");
  // 无合法 verdict 的旧事件只保留保守失败，永不提供审计计数或中性准入。
  const legacyExhausted = !verdict && latest.some((event) => event.type === "workflow_stage"
    && event.data?.exhausted === true);
  const terminalFailed = run?.answer_status === "verification_failed"
    || terminal?.data?.answer_status === "verification_failed"
    || terminal?.data?.verification_failed === true;
  const errorSignal = verificationErrorForRun({...run, events});
  const counts = verificationAuditCounts(verdict?.data?.verification_audit, verdict?.data);
  // category 与 Python 只读投影 verification_category 保持同一语义；
  // expected 只区分两种「无结论」：本档位不含验证（false）还是应验证却无
  // 结论（true）。历史 run 缺该字段时为 null，不把旧报告重新标成异常。
  const expected = run?.validation?.verification_expected;
  let category = "not_run";
  if (terminalFailed && errorSignal) category = "failed";
  else if (errorSignal) category = "error";
  else if (verdict?.data?.verdict === "fail") {
    category = counts && counts.blockerCount === 0 ? VERIFICATION_ADVISORY : "failed";
  } else if (verdict?.data?.exhausted === true) category = "failed";
  else if (terminalFailed) category = "failed";
  else if (verdict?.data?.verdict === "pass") category = "passed";
  else if (legacyExhausted) category = "failed";
  return {
    category,
    expected: expected === true ? true : expected === false ? false : null,
    issueCount: counts ? counts.issueCount : null,
    blockerCount: counts ? counts.blockerCount : null,
  };
}

export function recoveredFromErrorForRun(run) {
  return (run?.events || []).some((item) => (
    item.data?.recovered_from_error === true || item.data?.completion_degraded === true
  ));
}

export function sourceReadItems(data) {
  const reads = data?.source_reads;
  return reads?.version === 1 && Array.isArray(reads.items)
    ? reads.items.filter((item) => item && typeof item.url === "string" && citationSourceUrl(item.url)
      && ["reading", "read", "limited", "unavailable"].includes(item.status)) : [];
}

export function sourceUrlKey(value) {
  try { const url = new URL(value); return `${url.origin}${url.pathname === "/" ? "" : url.pathname}${url.search}`; } catch { return ""; }
}

export function sourceHostname(source) {
  try { return new URL(source.url).hostname; } catch { return ""; }
}

// Read targets are stricter than report citations: a credential-bearing query
// must never become a visible link, even if a legacy event still carries it.
// Keep in sync with the backend public_source_url sensitive-query gate.
function privateTargetQuery(url) {
  return [...url.searchParams.keys()].some((key) => (
    /(key|token|auth|pass|secret|credential|session|signature|cookie|jwt|x-amz|x-goog)/i.test(key)
    || /^(?:sig|code)$/i.test(key)
  ));
}

function publicTargetUrl(value) {
  const url = citationSourceUrl(value);
  return url && !privateTargetQuery(url) ? url : null;
}

function publicStreamText(value) {
  const decoded = String(value || "").replace(/&lt;|&#0*60;|&#x0*3c;/gi, "<")
    .replace(/&gt;|&#0*62;|&#x0*3e;/gi, ">");
  // Token drafts have not passed response parsing yet. Fail closed on private
  // tags (including nested or malformed blocks), rather than exposing fragments.
  // Canonical public progress arrives separately after backend sanitization.
  if (/<\/?(?:think|reasoning|seed:|model:)/i.test(decoded)) return "";
  return decoded.replace(/<\/?report\b[^>]*>/gi, "").replace(/<[\s\S]*$/, "");
}

export function collectDraft(run) {
  if (["completed", "failed", "cancelled"].includes(run.status)) return "";
  const events = validRunEvents(run.events || []);
  const deltas = events.filter((event) => event.type === "assistant_delta");
  const last = deltas.at(-1);
  if (!last) return "";
  const key = progressTurnKey(last.data);
  if (events.some((event) => ["progress_note", "activity_started", "activity_finished"].includes(event.type)
    && progressTurnKey(event.data) === key)) return "";
  return publicStreamText(deltas.filter((event) => progressTurnKey(event.data) === key)
    .map((event) => event.data.text || "").join(""));
}

export function tasksForRun(run) {
  const snapshot = validRunEvents(run?.events || []).reverse()
    .find((event) => event.type === "task_board_updated");
  return Array.isArray(snapshot?.data?.tasks) ? snapshot.data.tasks : [];
}

export function subagentSnapshotForRun(run) {
  const snapshot = validRunEvents(run?.events || []).reverse()
    .find((event) => event.type === "subagent_status");
  if (!snapshot || !Array.isArray(snapshot.data?.agents)) return null;
  return snapshot.data.agents.flatMap((agent) => {
    const name = String(agent?.name || "").trim();
    const status = String(agent?.status || "").toLowerCase();
    if (!name || !SUBAGENT_STATUS_LABEL_KEYS[status]) return [];
    return [{
      name,
      status,
      totalTasks: Math.max(0, Number(agent.total_tasks) || 0),
    }];
  });
}

export function subagentSummaryCopy(agents) {
  if (!Array.isArray(agents) || !agents.length) return "";
  const counts = { running: 0, queued: 0, reported: 0, created: 0 };
  agents.forEach((agent) => { if (counts[agent.status] != null) counts[agent.status] += 1; });
  const detail = SUBAGENT_STATUS_ORDER
    .filter((status) => counts[status])
    .map((status) => t("run:subagent.statusCount", {
      total: counts[status], label: subagentStatusLabel(status),
    }))
    .join(t("run:subagent.separator"));
  return detail ? t("run:subagent.summary", { count: agents.length, detail }) : "";
}

export function collectReportsWaitCopy(startedAt, total, remaining, now = Date.now()) {
  const seconds = startedAt > 0 ? Math.max(0, now / 1000 - startedAt) : 0;
  const base = t("run:collect.waitMinutes", { minutes: Math.floor(seconds / 60) });
  return total == null ? base
    : `${base}${t("run:collect.separator")}${t("run:collect.waitDetail", { remaining, total })}`;
}

function taskStatus(task) {
  const raw = String(task?.resolution || task?.status || "open").toLowerCase();
  if (["resolved", "completed"].includes(raw)) return "resolved";
  if (raw === "in_progress") return "in_progress";
  if (raw === "cancelled") return "cancelled";
  return "open";
}

export function taskBoardPresentation(tasks, runStatus = "") {
  const subagents = arguments.length > 2 ? arguments[2] : null;
  const terminalRun = ["completed", "failed", "cancelled"]
    .includes(String(runStatus || "").toLowerCase());
  const statusByOwner = new Map((subagents || []).map((agent) => [agent.name, agent.status]));
  const items = tasks.map((task) => {
    const rawStatus = taskStatus(task);
    const status = task?.closed_by_run
      || (terminalRun && ["open", "in_progress"].includes(rawStatus))
      ? "closed"
      : rawStatus;
    const owners = (Array.isArray(task.owners) ? task.owners : [])
      .map((owner) => String(owner || "").trim())
      .filter((owner) => owner && statusByOwner.has(owner))
      .map((name) => ({ name, status: statusByOwner.get(name) }));
    return {
      id: String(task.id || ""),
      title: String(task.description || task.title || task.id || t("run:task.untitled")),
      status,
      owners,
    };
  });
  const completed = items.filter((task) => task.status === "resolved").length;
  const cancelled = items.filter((task) => task.status === "cancelled").length;
  const closed = items.filter((task) => task.status === "closed").length;
  const current = items.find((task) => task.status === "in_progress")
    || items.find((task) => task.status === "open") || null;
  return {
    items, completed, cancelled, closed, total: items.length - cancelled, current,
  };
}

export function taskBoardProgressCopy(board) {
  const progress = `${board.completed}/${board.total}`;
  return board.closed > 0
    ? t("run:task.progressClosed", { progress })
    : t("run:task.progress", { progress });
}

export function taskBoardCurrentCopy(board) {
  const active = board.items.filter((task) => task.status === "in_progress").length;
  if (active > 1) return t("run:task.activeMany", { count: active });
  if (board.current) {
    return t(board.current.status === "in_progress" ? "run:task.current" : "run:task.next", {
      title: board.current.title,
    });
  }
  if (board.closed > 0) return t("run:task.endedClosed", { count: board.closed });
  if (board.total === 0 && board.cancelled > 0) return t("run:task.allCancelled");
  if (board.completed === board.total) return t("run:task.allCompleted");
  return t("run:task.noneActive");
}

export function selectTaskBoardRun(runs, activeRun) {
  const ordered = Array.isArray(runs) ? runs : [];
  if (activeRun && ["queued", "running"].includes(activeRun.status)) {
    return ordered.find((run) => run.id === activeRun.id) || activeRun;
  }
  return ordered.at(-1) || null;
}

export function renderTaskRows(board) {
  const states = {
    resolved: ["✓", t("run:task.stateResolved")],
    in_progress: ["▶", t("run:task.stateInProgress")],
    open: ["○", t("run:task.stateOpen")],
    cancelled: ["⊘", t("run:task.stateCancelled")],
    closed: ["■", t("run:task.stateClosed")],
  };
  return `<ol class="task-board-list">${board.items.map((task) => {
    const presentation = states[task.status];
    const current = task.status === "in_progress" ? ' aria-current="step"' : "";
    const owners = task.owners.map((owner) => (
      `${owner.name}${t("run:task.ownerSeparator")}${subagentStatusLabel(owner.status)}`
    )).join(" / ");
    return `<li class="task-row ${task.status}"${current}><span class="task-state" aria-hidden="true">${presentation[0]}</span><span><p>${escapeHtml(task.title)}</p><small>${escapeHtml(presentation[1])}</small>${owners ? `<small>${escapeHtml(owners)}</small>` : ""}</span></li>`;
  }).join("")}</ol>`;
}

function compactStepDetail(value, limit = 140) {
  const text = String(value || "").replace(/\s+/g, " ").trim();
  return text.length > limit ? `${text.slice(0, limit)}…` : text;
}

function clipStepTitle(value, limit) {
  const text = String(value ?? "").replace(/\s+/g, " ").trim();
  return text.length > limit ? `${text.slice(0, limit)}…` : text;
}

// Read targets are the one public fact both fetch channels share: the main
// loop's argument preview and the agent-team reader's source_reads update.
// Only URLs that pass the citation gate may become a visible target.
function publicTargetLabel(value, limit = 60) {
  try {
    const url = new URL(String(value ?? ""));
    if (!["http:", "https:"].includes(url.protocol)) return "";
    return clipStepTitle(`${url.hostname}${url.pathname === "/" ? "" : url.pathname}`, limit);
  } catch { return ""; }
}

function fetchTargetsFromDetail(detail) {
  const raw = String(detail ?? "").trim();
  let args = null;
  if (detail && typeof detail === "object") args = detail;
  else if (raw) {
    try { args = JSON.parse(raw); } catch { args = null; }
  }
  const object = args && typeof args === "object" && !Array.isArray(args) ? args : {};
  if (args === null && raw.startsWith("{")) {
    for (const match of raw.matchAll(/"(url|urls)"\s*:\s*("(?:\\.|[^"\\])*")/g)) {
      try { object[match[1]] = JSON.parse(match[2]); } catch { /* incomplete scalar */ }
    }
  }
  const fallback = args === null && !raw.startsWith("{") ? raw : "";
  return [object.url ?? object.urls ?? fallback].flat()
    .filter((item) => item !== undefined && item !== null && String(item).trim())
    .map((item) => String(item).trim())
    .filter((value) => publicTargetUrl(value));
}

function stepTitle(activity, detail) {
  const raw = String(detail ?? "").trim();
  let args = null;
  if (detail && typeof detail === "object") args = detail;
  else if (raw) {
    try { args = JSON.parse(raw); } catch { args = null; }
  }
  const object = args && typeof args === "object" && !Array.isArray(args) ? args : {};
  // Argument previews may end mid-JSON. Read only complete safe scalar fields,
  // rather than turning the whole JSON/report payload into a tool title.
  if (args === null && raw.startsWith("{")) {
    for (const match of raw.matchAll(/"(q|query|url|path|file|filename)"\s*:\s*("(?:\\.|[^"\\])*")/g)) {
      try { object[match[1]] = JSON.parse(match[2]); } catch { /* incomplete scalar */ }
    }
  }
  const fallback = args === null && !raw.startsWith("{") ? raw : "";
  const values = (value) => (Array.isArray(value) ? value : [value])
    .filter((item) => item !== undefined && item !== null && String(item).trim())
    .map((item) => String(item).trim());

  if (activity === "web_search") {
    const queries = values(object.q ?? object.query ?? object.queries ?? fallback);
    const query = clipStepTitle(queries.join(" · "), 60);
    return query ? t("run:tool.search", { query }) : activityLabel(activity);
  }
  if (["web_fetch", "download_file"].includes(activity)) {
    const url = clipStepTitle(values(object.url ?? object.urls ?? fallback)
      .map((value) => publicTargetLabel(value)).filter(Boolean).join(" · "), 60);
    return url ? t("run:tool.fetch", { target: url }) : activityLabel(activity);
  }
  // Commands and workspace paths belong only in opt-in tool/trace details.
  if (activity === "bash") return activityLabel(activity);
  if (["create_file", "write_file", "read_file"].includes(activity)) {
    const path = String(object.path ?? object.file ?? object.filename ?? fallback);
    const filename = clipStepTitle(path.split(/[\\/]/).filter(Boolean).at(-1) || path, 60);
    const verb = activity === "read_file" ? "run:tool.read" : "run:tool.write";
    return filename ? t(verb, { name: filename }) : activityLabel(activity);
  }
  if (activity === "create_subagent") {
    const rawAgents = Array.isArray(object.agents)
      ? object.agents : [object.agents ?? fallback].filter(Boolean);
    const agents = rawAgents.map((item) => {
      if (item && typeof item === "object") return item.name || item.system_prompt || "";
      try {
        const parsed = JSON.parse(String(item));
        return parsed.name || parsed.system_prompt || item;
      } catch { return item; }
    }).filter(Boolean);
    const description = clipStepTitle(agents.join(" · "), 40);
    return description ? t("run:tool.subagent", { detail: description }) : t("run:tool.subagentEmpty");
  }
  if (activity === "assign_task") {
    const tasks = Array.isArray(object.tasks) ? object.tasks : values(object.tasks ?? fallback);
    const descriptions = tasks.map((task) => (
      task && typeof task === "object"
        ? task.prompt || task.description || task.agent || ""
        : String(task)
    )).filter(Boolean);
    const description = clipStepTitle(descriptions.join(" · "), 40);
    return description ? t("run:tool.assign", { detail: description }) : t("run:tool.assignEmpty");
  }
  if (activity === "collect_reports") return t("run:tool.collect");
  return activityLabel(activity);
}

function renderProcessStep({ status, title, detail, duration = "", key = "", detailState = new Map() }) {
  const summary = compactStepDetail(detail) || t("run:tool.rawEvent");
  return `<details class="step-row step-${status}" ${detailAttrs(key, detailState)}>
    <summary><span class="step-dot ${status}"></span><span class="step-copy"><strong>${escapeHtml(title)}</strong><small>${escapeHtml(summary)}</small></span>${duration ? `<time>${escapeHtml(duration)}</time>` : ""}<span class="step-chevron">⌄</span></summary>
    <div class="step-detail">${escapeHtml(detail || summary)}</div>
  </details>`;
}

export function productEventPresentation(event) {
  if (event.type === "workflow_stage") return workflowStagePresentation(event.data);
  if (event.type === "orchestration_stage") {
    return {
      status: event.data.stage === "scenario_routing" ? "running" : "ok",
      title: orchestrationStageTitle(event.data.stage),
      detail: orchestrationStageLabel(event.data.stage),
    };
  }
  if (event.type === "artifact_preview_ready") {
    return {
      status: "ok",
      title: t("run:event.previewReadyTitle"),
      detail: t("run:event.previewReadyDetail"),
    };
  }
  const checks = Array.isArray(event.data.checks) ? event.data.checks : [];
  const passed = checks.filter((item) => item.passed).length;
  const failed = event.data.status !== "passed";
  return {
    status: failed ? "failed" : "ok",
    title: t("run:event.deliveryScore", { score: event.data.score ?? 0 }),
    detail: checks.length
      ? t("run:event.deliveryPassed", { passed, total: checks.length })
      : t("run:event.deliveryDone"),
  };
}

export function validRunEvents(events) {
  const retained = [];
  const seen = new Set();
  for (const event of [...(events || [])].sort((a, b) => (a.seq || 0) - (b.seq || 0))) {
    if (event.seq != null && seen.has(event.seq)) continue;
    seen.add(event.seq);
    const data = event.data || {};
    if (event.type === "warning" && data.discard_stream) {
      for (let index = retained.length - 1; index >= 0; index -= 1) {
        const prior = retained[index];
        const p = prior.data || {};
        if (!["assistant_delta", "progress_note"].includes(prior.type)) continue;
        const match = data.discard_call_id
          ? p.llm_call_id === data.discard_call_id
            && (data.reason === "turn_rolled_back" || (data.discard_attempt_id
              ? p.attempt_id === data.discard_attempt_id : p.attempt === data.attempt))
          : p.turn != null && p.turn === data.turn
            && (data.reason === "turn_rolled_back" || p.attempt === data.attempt);
        if (match) retained.splice(index, 1);
      }
    }
    retained.push(event);
  }
  return retained;
}

function progressTurnKey(data, fallback = "") {
  if (data.loop_id && data.llm_call_id) return `${data.loop_id}:${data.role_id || ""}:${data.llm_call_id}`;
  return data.llm_call_id || `${data.loop_id || "legacy"}:${data.role_id || ""}:${data.turn ?? data.call_id ?? fallback}`;
}

function publicProgressNote(data) {
  // Already sanitized/redacted by the producer. Do not decode or strip tags:
  // joining fragments could reconstruct a secret that the producer removed.
  const text = String(data?.text || "");
  return /<|&(?:lt;|gt;|#)/i.test(text) ? "" : text.slice(0, 1200).trim();
}

// 带轮号的阶段在多个投影里各有一套整句资源：实时（带轮号）、收口（带轮号）、研究记录（不带轮号）。
// 选择完全由结构化 type/stage 决定；绝不解析已经渲染出来的文案，也不依赖标题里是否出现过某个词。
const STAGE_TITLE_COPY = Object.freeze({
  "workflow_stage:verification_started": Object.freeze({
    params: (data) => ({ round: Math.max(0, Number(data?.attempt) || 0) + 1 }),
    running: "run:stage.verificationRound",
    settled: "run:stage.verificationRoundSettled",
    record: "run:stage.verificationRecord",
    recordSettled: "run:stage.verificationRecordSettled",
  }),
  "workflow_stage:repair_started": Object.freeze({
    params: (data) => ({ round: Math.max(1, Number(data?.attempt) || 1) }),
    running: "run:stage.repairRound",
    settled: "run:stage.repairRoundSettled",
    record: "run:stage.repairRecord",
    recordSettled: "run:stage.repairRecordSettled",
  }),
  "workflow_stage:repair_completed": Object.freeze({
    settled: "run:stage.repairedSettled",
  }),
  "orchestration_stage:scenario_routing": Object.freeze({
    settled: "run:stage.scenarioRoutingSettled",
  }),
});
// status 之外仍需在终态收口的阶段：文案描述“仍在进行”，但状态位不是 running。
const IN_FLIGHT_STAGE_KEYS = new Set([
  "workflow_stage:repair_completed",
  "orchestration_stage:report_compilation",
  "orchestration_stage:summary_compilation",
]);

function stageTitle(event, variant) {
  const spec = STAGE_TITLE_COPY[`${event?.type}:${event?.data?.stage}`];
  const key = spec ? spec[variant] : "";
  if (!key) return "";
  return t(key, spec.params ? spec.params(event.data) : undefined);
}

// 研究记录不呈现轮号：有专属记录的阶段用已登记的无轮号文案，其余沿用实时文案。
function recordTitleFor(step) {
  if (!step.recordTitle) return step.title;
  return step.status === "running" ? step.recordTitle : step.recordSettledTitle;
}

export function buildRunSummary(rawEvents, runStatus = "") {
  const events = validRunEvents(rawEvents);
  const terminal = [...events].reverse().find((event) => /^run_(completed|failed|cancelled)$/.test(event.type));
  const ended = !!terminal || ["completed", "failed", "cancelled"].includes(runStatus);
  const failedRun = terminal?.type === "run_failed" || runStatus === "failed";
  const steps = [];
  const rounds = new Map();
  const tools = new Map();
  let routing = null;
  let activeStage = null;
  const roundFor = (event) => {
    const data = event.data || {};
    const key = progressTurnKey(data, event.seq);
    if (!rounds.has(key)) {
      const round = {
        kind: "turn", key, seq: event.seq, turn: data.turn,
        role: data.role_id || "", status: "ok", title: "", detail: "", tools: [],
      };
      rounds.set(key, round);
      steps.push(round);
    }
    return rounds.get(key);
  };
  for (const event of events) {
    const data = event.data || {};
    if (event.type === "progress_note") {
      const round = roundFor(event);
      round.detail = publicProgressNote(data);
    } else if (["activity_started", "activity_finished"].includes(event.type)) {
      const round = roundFor(event);
      const key = `${round.key}:${data.call_id || event.seq}`;
      let tool = tools.get(key);
      if (!tool) {
        tool = { key, seq: event.seq, activity: data.activity, title: activityLabel(data.activity || "tool"), status: "running", input: "", output: "" };
        tools.set(key, tool);
        round.tools.push(tool);
      }
      // Public read targets never carry read status into the record; the
      // status only decides whether this call has settled. A call without a
      // safe target keeps its ordinary lifecycle and shows no link.
      const rememberTarget = (url, readStatus = "") => {
        tool.targets ??= new Map();
        tool.targets.set(sourceUrlKey(url) || url, { url, readStatus });
      };
      if (event.type === "activity_started" && !data.source_read_update) {
        tool.title = stepTitle(data.activity, data.detail);
        tool.input = String(data.detail || "");
        if (data.activity === "web_fetch") {
          for (const url of fetchTargetsFromDetail(data.detail)) rememberTarget(url);
        }
      } else if (!data.source_read_update) {
        tool.status = data.interrupted ? "ended" : data.ok === false ? "failed" : "ok";
        tool.output = String(data.detail || "");
        tool.duration = data.duration_ms >= 1000
          ? t("run:tool.durationSeconds", { seconds: (data.duration_ms / 1000).toFixed(1) })
          : "";
      } else if (data.activity === "web_fetch") {
        for (const item of sourceReadItems(data)) {
          if (publicTargetUrl(item.url)) rememberTarget(item.url, item.status);
        }
        if (tool.targets?.size && tool.status !== "failed") {
          const reading = [...tool.targets.values()].some((item) => item.readStatus === "reading");
          tool.status = reading ? "running" : "ok";
        }
      }
    } else if (event.type === "warning") {
      // Tool failures are localized to their own row, not a whole search phase.
      if (data.reason === "tool_error") {
        const matched = events.some((item) => item.type === "activity_finished" && item.data?.ok === false
          && (data.call_id ? item.data.call_id === data.call_id
            && (!data.llm_call_id || item.data.llm_call_id === data.llm_call_id)
            : item.seq < event.seq && String(data.message || "").startsWith(`${item.data.activity} failed`)));
        if (!matched) {
          steps.push({
            key: `event:${event.seq}`, status: "warning", toolError: true,
            title: t("run:warning.toolErrorTitle"), detail: t("run:warning.toolErrorDetail"),
          });
        }
        continue;
      }
      const retry = ["llm_attempt_discarded", "turn_rolled_back"].includes(data.reason);
      steps.push({
        key: `event:${event.seq}`, status: "warning", warning: true,
        title: retry ? t("run:warning.retryTitle") : t("run:warning.genericTitle"),
        detail: retry ? t("run:warning.retryDetail") : t("run:warning.genericDetail"),
      });
    } else if (["workflow_stage", "orchestration_stage", "report_validation", "artifact_preview_ready"].includes(event.type)) {
      const step = { ...productEventPresentation(event), key: `event:${event.seq}` };
      const settledTitle = stageTitle(event, "settled");
      if (settledTitle) step.settledTitle = settledTitle;
      const recordTitle = stageTitle(event, "record");
      if (recordTitle) {
        step.recordTitle = recordTitle;
        step.recordSettledTitle = stageTitle(event, "recordSettled");
      }
      if (IN_FLIGHT_STAGE_KEYS.has(`${event.type}:${data.stage}`)) step.live = true;
      if (activeStage) {
        activeStage.status = "ended";
        if (activeStage.settledTitle) activeStage.title = activeStage.settledTitle;
        activeStage.detail = t("run:warning.supersededDetail");
      }
      activeStage = step.status === "running" ? step : null;
      if (event.type === "orchestration_stage" && data.stage === "scenario_routing") routing = step;
      if (routing && data.stage === "scenario_resolved") {
        Object.assign(routing, {
          status: "ok",
          title: t("run:warning.scenarioResolvedTitle"),
          detail: t("run:warning.scenarioResolvedDetail"),
        });
        routing = null;
      }
      steps.push(step);
    }
  }
  for (const round of rounds.values()) {
    for (const tool of round.tools) {
      if (ended && tool.status === "running") tool.status = "ended";
    }
    round.status = round.tools.some((tool) => tool.status === "running") ? "running" : "ok";
    round.title = round.turn != null ? t("run:turn.round", { round: round.turn }) : t("run:turn.tools");
  }
  if (!rounds.size && events.some((event) => ["thinking_block", "assistant_transcript"].includes(event.type))) {
    steps.unshift({
      key: "legacy", status: "ok",
      title: t("run:legacy.title"), detail: t("run:legacy.detail"),
    });
  }
  if (ended) {
    // 收口只看结构化状态：阶段是否已结束由 status / in-flight 标记决定，与当前语言或文案无关。
    for (const step of steps) {
      if (step.kind === "turn") continue;
      if (step.status !== "running" && !step.live) continue;
      step.status = failedRun ? "failed" : "ended";
      if (step.settledTitle) step.title = step.settledTitle;
      step.detail = "";
    }
    if (routing) Object.assign(routing, {
      status: failedRun ? "failed" : "ok",
      title: t("run:warning.routingUnfinishedTitle"),
      detail: t("run:warning.routingUnfinishedDetail"),
    });
  }
  return steps;
}

function detailAttrs(key, detailState, defaultOpen = false) {
  return `data-detail-key="${escapeHtml(key)}"${(detailState.get(key) ?? defaultOpen) ? " open" : ""}`;
}

function renderPaged(items, render, key, detailState, size) {
  if (items.length <= size) return items.map(render).join("");
  const older = items.slice(0, -size);
  const open = detailState.get(key);
  return `<details class="timeline-history" data-lazy-detail ${detailAttrs(key, detailState)}><summary>${escapeHtml(t("run:tool.historyOlder", { count: older.length }))}</summary>${open ? renderPaged(older, render, `${key}:older`, detailState, size) : ""}</details>${items.slice(-size).map(render).join("")}`;
}

export function renderTimeline(steps, prefix = "timeline", detailState = new Map()) {
  const renderTool = (tool) => {
    const key = `${prefix}:tool:${tool.key}`;
    const labels = {
      running: t("run:tool.timelineStatusRunning"),
      ok: t("run:tool.timelineStatusOk"),
      failed: t("run:tool.timelineStatusFailed"),
      ended: t("run:tool.timelineStatusEnded"),
    };
    const detail = [
      tool.input && t("run:tool.parameters", { detail: tool.input }),
      tool.output && t("run:tool.result", { detail: tool.output }),
    ].filter(Boolean).join("\n");
    return `<details class="timeline-tool" ${detailAttrs(key, detailState)}><summary><span class="step-dot ${tool.status}" aria-hidden="true"></span><span>${escapeHtml(tool.title)}</span><small>${escapeHtml(labels[tool.status] || labels.ok)}</small>${tool.duration ? `<time>${escapeHtml(tool.duration)}</time>` : ""}</summary><div class="step-detail">${escapeHtml(detail)}</div></details>`;
  };
  return renderPaged(steps, (step) => step.kind !== "turn" ? renderSummaryStep(step)
    : `<section class="research-turn" role="listitem"><header>${escapeHtml(step.title)}</header>${step.detail ? `<p class="progress-note">${escapeHtml(step.detail)}</p>` : ""}<div class="turn-tools">${renderPaged(step.tools, renderTool, `${prefix}:tools:${step.key}`, detailState, 6)}</div></section>`, `${prefix}:history`, detailState, 12);
}

function renderResearchRecord(events, steps, overview, prefix, detailState) {
  const valid = validRunEvents(events);
  const preparation = new Set(["scenario_routing", "scenario_resolved", "research_planning"]);
  const lastPreparation = valid.filter((event) => event.type === "orchestration_stage" && preparation.has(event.data.stage)).at(-1);
  const stages = new Map(steps.filter((step) => step.kind !== "turn").map((step) => [step.key, step]));
  const tools = new Map(steps.flatMap((step) => step.tools || []).map((tool) => [tool.seq, tool]));
  const records = [];
  const notes = new Set(overview.note ? [overview.note] : []);
  for (const event of valid) {
    const data = event.data || {};
    if (event.type === "progress_note") {
      const text = publicProgressNote(data);
      if (text && !notes.has(text)) {
        records.push({ kind: "note", text });
        notes.add(text);
      }
      continue;
    }
    const tool = tools.get(event.seq);
    if (tool && ["web_search", "web_fetch", "download_file", "read_file"].includes(tool.activity)) {
      const kind = tool.activity === "web_search" ? "search" : "read";
      let group = records.at(-1);
      if (group?.kind !== kind) {
        group = { kind, key: tool.key, tools: [] };
        records.push(group);
      }
      group.tools.push(tool);
      continue;
    }
    const step = stages.get(`event:${event.seq}`);
    if (!step) continue;
    if (event.type === "orchestration_stage") {
      if (preparation.has(data.stage)) {
        if (event !== lastPreparation) continue;
        records.push({
          ...step,
          title: overview.ended
            ? t("run:record.preparationEnded")
            : data.stage === "scenario_routing" ? t("run:record.preparationRunning") : t("run:record.preparation"),
          detail: "",
        });
        continue;
      }
      if (!["report_compilation", "artifact_verification", "summary_compilation"].includes(data.stage)) continue;
    }
    if (event.type === "workflow_stage" && (!["verification_started", "verification_verdict", "repair_started", "repair_completed"].includes(data.stage)
      || (data.stage === "verification_verdict" && !["pass", "fail"].includes(data.verdict)
        && verificationStageCategory(data) !== VERIFICATION_ERROR))) continue;
    const bareFailure = event.type === "workflow_stage" && data.stage === "verification_verdict"
      && verificationStageCategory(data) === "failed" && !data.will_retry && !data.exhausted;
    records.push(bareFailure
      ? { status: "failed", title: t("run:verification.failedTitle"), detail: t("run:verification.failedDetail") }
      : { ...step, title: recordTitleFor(step) });
  }
  const renderTool = (tool) => {
    const labels = {
      running: t("run:tool.recordStatusRunning"),
      ok: t("run:tool.recordStatusOk"),
      failed: t("run:tool.recordStatusFailed"),
      ended: t("run:tool.recordStatusEnded"),
    };
    const target = researchTarget(tool);
    return renderSummaryStep({
      status: tool.status, title: tool.title,
      // 只传纯文本：链接由 renderSummaryTitle 用已校验的 href 组装。
      titleLink: target ? {
        href: target.href,
        title: target.title,
        text: t("run:tool.fetch", { target: target.label }),
        more: target.more,
      } : null,
      detail: labels[tool.status] || labels.ok,
    });
  };
  if (!records.length) {
    return renderSummaryStep({
      status: "ended",
      title: t("run:record.emptyTitle"), detail: t("run:record.emptyDetail"),
    });
  }
  return renderPaged(records, (record) => {
    if (record.kind === "note") return `<p class="progress-note" role="listitem">${escapeHtml(record.text)}</p>`;
    if (!["search", "read"].includes(record.kind)) return renderSummaryStep(record);
    const summary = record.kind === "search" ? t("run:record.activitySearch") : t("run:record.activityRead");
    const header = t("run:record.activityHeader", { label: summary, count: record.tools.length });
    return `<section class="research-activity research-${record.kind}" role="listitem"><header>${escapeHtml(header)}</header>${renderPaged(record.tools, renderTool, `${prefix}:activity:${record.key}`, detailState, 6)}</section>`;
  }, `${prefix}:history`, detailState, 12);
}

// The record links the first read target and counts the rest; the full set
// stays in the link title. Read status and its reason never enter the record.
// Returns plain text plus an already validated href; it never returns markup,
// so no HTML can travel through the translation layer.
function researchTarget(tool) {
  const targets = [...(tool.targets?.values() ?? [])].map((item) => item.url).filter(publicTargetUrl);
  if (!targets.length) return null;
  return {
    href: publicTargetUrl(targets[0]).href,
    label: publicTargetLabel(targets[0]) || targets[0],
    title: targets.join("\n"),
    more: targets.length > 1 ? t("run:record.targetMore", { count: targets.length }) : "",
  };
}

// The only place that assembles the record link: href, translated text and the
// trailing count are all escaped here, and every part is plain text upstream.
function renderSummaryTitle(step) {
  const link = step.titleLink;
  if (!link) return escapeHtml(step.title);
  return `<a class="research-target" href="${escapeHtml(link.href)}" target="_blank" rel="noopener noreferrer" title="${escapeHtml(link.title)}">${escapeHtml(link.text)}</a>${link.more ? escapeHtml(link.more) : ""}`;
}

function renderSummaryStep(step) {
  const current = step.status === "running" ? ' aria-current="step"' : "";
  const title = renderSummaryTitle(step);
  return `<div class="phase-row phase-${step.status}" role="listitem"${current}><span class="step-dot ${step.status}" aria-hidden="true"></span><span class="step-copy"><strong>${title}</strong><small>${escapeHtml(step.detail)}</small></span>${step.duration ? `<time>${escapeHtml(step.duration)}</time>` : ""}</div>`;
}

export function developerTraceData(events) {
  return (Array.isArray(events) ? events : []).map((event) => ({
    seq: event.seq,
    type: event.type,
    data: event.data || {},
  }));
}

function developerEventPresentation(event, prefix, detailState) {
  const status = event.type === "warning" || event.type === "run_failed"
    || (event.type === "activity_finished" && !event.data.ok)
    ? "failed"
    : "ok";
  const title = event.type === "thinking_block"
    ? t("run:developer.analysis")
    : ["activity_started", "activity_finished"].includes(event.type)
      ? t("run:developer.activity", {
        label: activityLabel(event.data.activity || "tool"),
        phase: event.type === "activity_started" ? t("run:developer.phaseStarted") : t("run:developer.phaseFinished"),
      })
      : event.type;
  const detail = JSON.stringify(event.data || {}, null, 2);
  return renderProcessStep({
    status,
    title: `${event.seq ?? "-"} · ${title}`,
    key: `${prefix}:event:${event.seq}`,
    detailState,
    detail,
    duration: event.data.duration_ms ? `${Math.round(event.data.duration_ms)}ms` : "",
  });
}

function renderDeveloperTrace(events, prefix, detailState) {
  const trace = developerTraceData(events);
  const key = `${prefix}:developer`;
  const summary = t("run:developer.summary", { count: trace.length });
  return `<details class="developer-trace" data-lazy-detail ${detailAttrs(key, detailState)}><summary><span>${escapeHtml(t("run:developer.title"))}</span><small>${escapeHtml(summary)}</small><span class="step-chevron">⌄</span></summary><div class="developer-events">${detailState.get(key) ? renderPaged(trace, (event) => developerEventPresentation(event, prefix, detailState), `${key}:history`, detailState, 60) : ""}</div></details>`;
}

export function buildRunOverview(run, steps = buildRunSummary(run.events || [], run.status), artifacts = []) {
  const events = validRunEvents(run.events || []);
  const latest = [...events].reverse();
  const terminal = latest.find((event) => /^run_(completed|failed|cancelled)$/.test(event.type));
  const runStatus = ["failed", "cancelled"].includes(run.status) ? run.status
    : terminal ? terminal.type.slice(4) : run.status;
  const ended = ["completed", "failed", "cancelled"].includes(runStatus);
  const queued = latest.find((event) => event.type === "queued");
  const position = Number(queued?.data?.position);
  const ahead = Number.isFinite(position) ? Math.max(0, position - 1) : 0;
  const verdict = latest.find((event) => event.type === "workflow_stage"
    && event.data?.stage === "verification_verdict");
  const presentation = verificationPresentationForRun({ ...run, events });
  const verificationError = verificationErrorForRun({ ...run, events });
  const verificationFailed = !verificationError && (verdict?.data?.verdict === "fail"
    || verificationFailedForRun({ ...run, events }));
  const degraded = recoveredFromErrorForRun({ ...run, events });
  // Only the latest public verdict owns its issues. A newer pass or an empty
  // failure must never inherit an earlier, potentially repaired issue list.
  const issues = verdict?.data?.verdict === "fail" && Array.isArray(verdict.data.issues)
    ? verdict.data.issues.map((issue) => typeof issue === "string" ? issue
      : typeof issue?.message === "string" ? issue.message : "").filter((text) => text.trim()) : [];
  const delivered = events.some((event) => event.type === "artifact_preview_ready"
    || (event.type === "run_completed" && event.data?.report_ready === true)
    || (event.type === "artifact_created" && event.data?.generated === true && event.data?.name === "Final Report.md"))
    || artifacts.some((item) => item.run_id === run.id && item.name === "Final Report.md");
  // Thin projection of structured stages, not a second timeline. Unknown labels
  // and raw tool arguments remain in opt-in details, never the overview.
  const stage = latest.find((event) => ["workflow_stage", "orchestration_stage", "artifact_preview_ready"].includes(event.type));
  const workflowTitles = {
    verification_started: t("run:overview.stageVerification"),
    repair_started: t("run:overview.stageRepair"),
    repair_completed: t("run:overview.stageRepairCompleted"),
  };
  const orchestrationTitles = {
    scenario_routing: t("run:overview.stageScenarioRouting"),
    scenario_resolved: t("run:overview.stageScenarioResolved"),
    research_planning: t("run:overview.stageResearchPlanning"),
    report_compilation: t("run:overview.stageReportCompilation"),
    artifact_verification: t("run:overview.stageArtifactVerification"),
    summary_compilation: t("run:overview.stageSummaryCompilation"),
    revise: t("run:overview.stageRevise"),
  };
  let status = t("run:overview.statusRunning");
  if (runStatus === "failed") status = t("run:overview.statusFailed");
  else if (runStatus === "cancelled") status = t("run:overview.statusCancelled");
  else if (runStatus === "completed") {
    status = delivered ? t("run:overview.statusDelivered") : t("run:overview.statusCompleted");
  } else if (runStatus === "queued") status = t("run:overview.statusQueued", { ahead });
  else if (stage?.type === "workflow_stage") {
    // 核验阶段遵守 error 优先级：验证器未返回有效结果时不得显示“研究中”或“验证通过”。
    const stageCategory = stage.data.stage === "verification_verdict"
      ? verificationStageCategory(stage.data) : "";
    if (workflowTitles[stage.data.stage]) status = workflowTitles[stage.data.stage];
    else if (stageCategory === VERIFICATION_ERROR) status = t("run:verification.errorStatus");
    else if (stageCategory === VERIFICATION_ADVISORY) {
      status = t("run:verification.advisoryTitle", {
        count: verificationAuditCounts(stage.data.verification_audit, stage.data)?.issueCount ?? 0,
      });
    } else if (stageCategory === "failed") {
      status = stage.data.will_retry ? t("run:verification.retryStatus") : t("run:verification.failedTitle");
    } else if (stageCategory === "passed") status = t("run:verification.passedTitle");
    else status = t("run:overview.statusRunning");
  } else if (stage?.type === "orchestration_stage") {
    status = orchestrationTitles[stage.data.stage] || t("run:overview.statusRunning");
  } else if (stage?.type === "artifact_preview_ready") status = t("run:overview.previewReady");
  const note = !ended && runStatus !== "queued"
    ? latest.filter((event) => event.type === "progress_note").map((event) => publicProgressNote(event.data)).find(Boolean) || "" : "";
  // 工具异常/警告计数只读结构化标记，不再拿渲染后的标题跟某一段文案比对。
  const toolErrors = steps.flatMap((step) => step.tools || []).filter((tool) => tool.status === "failed").length
    + steps.filter((step) => step.toolError).length;
  const warnings = steps.filter((step) => step.warning && !step.toolError).length;
  const subagents = subagentSnapshotForRun({ events });
  const board = taskBoardPresentation(tasksForRun({ events }), runStatus, subagents);
  const collectingTool = !ended && runStatus === "running"
    ? steps.flatMap((step) => step.tools || [])
      .filter((tool) => tool.activity === "collect_reports" && tool.status === "running").at(-1)
    : null;
  const collectStartedAt = Number(
    collectingTool ? events.find((event) => event.seq === collectingTool.seq)?.ts : 0,
  ) || 0;
  const subagentTotal = subagents === null ? null : subagents.length;
  const subagentRemaining = (subagents || [])
    .filter((agent) => ["running", "queued"].includes(agent.status)).length;
  const activity = collectingTool
    ? collectReportsWaitCopy(collectStartedAt, subagentTotal, subagentRemaining)
    : "";
  const subagentSummary = subagentSummaryCopy(subagents);
  return {
    status, runStatus, ended, note, verificationFailed, verificationError, degraded, issues, toolErrors, warnings,
    verificationCategory: presentation.category, verificationIssueCount: presentation.issueCount,
    activity, board, collectStartedAt, subagentRemaining, subagentSummary, subagentTotal,
    dot: runStatus === "failed" ? "failed"
      : presentation.category === "failed" || presentation.category === "error" || degraded ? "warning"
        : runStatus === "cancelled" || runStatus === "queued" ? "ended" : ended ? "ok" : "running",
  };
}

export function renderRun(run, detailState = new Map(), artifacts = []) {
  const events = run.events || [];
  const steps = buildRunSummary(events, run.status);
  const overview = buildRunOverview(run, steps, artifacts);
  const { board } = overview;
  const prefix = `run:${run.id}`;
  const processOpen = detailState.get(prefix) ?? true;
  const taskProgress = taskBoardProgressCopy(board);
  const taskDetail = overview.activity || overview.subagentSummary || taskBoardCurrentCopy(board);
  const activityAttrs = overview.activity
    ? ` data-run-activity="${escapeHtml(run.id)}" data-collect-started-at="${overview.collectStartedAt}" data-subagent-total="${overview.subagentTotal ?? ""}" data-subagent-remaining="${overview.subagentRemaining}"`
    : "";
  const taskEntry = board.items.length ? `<div class="run-overview-tasks"><div><span>${escapeHtml(taskProgress)}</span><p${activityAttrs}>${escapeHtml(taskDetail)}</p></div><button class="button text-button" data-open-tasks="${escapeHtml(run.id)}" aria-label="${escapeHtml(t("run:task.openBoardAria", { progress: taskProgress }))}">${escapeHtml(t("run:task.view"))}</button></div>` : "";
  const verification = overview.verificationCategory === "error" ? `<section class="run-risk" role="status"><strong>${escapeHtml(t("run:verification.errorStatus"))}</strong><p>${escapeHtml(t("run:verification.errorRiskDetail"))}</p></section>` : overview.verificationCategory === VERIFICATION_ADVISORY ? `<section class="run-risk run-verification-advisory" role="status"><strong>${escapeHtml(t("run:verification.advisoryTitle", { count: overview.verificationIssueCount ?? 0 }))}</strong><details class="run-verification-issues" ${detailAttrs(`${prefix}:verification`, detailState)}><summary>${escapeHtml(t("run:verification.advisorySummary"))}</summary>${overview.issues.length ? `<ul>${overview.issues.map((issue) => `<li>${escapeHtml(issue)}</li>`).join("")}</ul>` : `<p>${escapeHtml(t("run:verification.advisoryEmpty"))}</p>`}</details></section>` : overview.verificationCategory === "failed" ? `<section class="run-risk" role="status"><strong>${escapeHtml(t("run:verification.failedTitle"))}</strong><details class="run-verification-issues" ${detailAttrs(`${prefix}:verification`, detailState)}><summary>${escapeHtml(t("run:verification.failedSummary"))}</summary>${overview.issues.length ? `<ul>${overview.issues.map((issue) => `<li>${escapeHtml(issue)}</li>`).join("")}</ul>` : `<p>${escapeHtml(t("run:verification.failedRiskEmpty"))}</p>`}</details></section>` : "";
  const failureEvent = validRunEvents(events).reverse().find((event) => event.type === "run_failed");
  const failureMessage = run.error || failureEvent?.data?.message || t("run:overview.failureFallback");
  const failureCard = overview.runStatus === "failed" ? `<section class="run-failure" role="alert"><strong>${escapeHtml(t("run:overview.failureTitle"))}</strong><p>${escapeHtml(failureMessage)}</p><small>${escapeHtml(t("run:overview.failureHint"))}</small></section>` : "";
  return `<section class="run-overview" data-run-id="${escapeHtml(run.id)}" aria-label="${escapeHtml(t("run:overview.titleAria"))}">
    <header class="run-overview-head"><span class="step-dot ${overview.dot}" aria-hidden="true"></span><h3>${escapeHtml(overview.status)}</h3><time data-run-elapsed="${escapeHtml(run.id)}" aria-label="${escapeHtml(t("run:overview.elapsedAria"))}">${formatRunElapsed(run)}</time></header>
    ${taskEntry}
    ${overview.note ? `<div class="run-latest-progress"><span>${escapeHtml(t("run:overview.latestProgress"))}</span><p class="progress-note">${escapeHtml(overview.note)}</p></div>` : ""}
    ${verification}
    ${overview.degraded ? `<p class="run-risk" role="status"><strong>${escapeHtml(t("run:overview.degradedLabel"))}</strong> · ${escapeHtml(t("run:overview.degradedDetail"))}</p>` : ""}
    ${overview.toolErrors ? `<p class="tool-warning-count" role="status">${escapeHtml(t("run:overview.toolErrors", { count: overview.toolErrors }))}</p>` : ""}
    ${overview.warnings ? `<p class="tool-warning-count" role="status">${escapeHtml(t("run:overview.warnings", { count: overview.warnings }))}</p>` : ""}
    <details class="run-process" data-lazy-detail ${detailAttrs(prefix, detailState, true)}>
      <summary><span>${escapeHtml(t("run:overview.processSummary"))}</span><span class="step-chevron" aria-hidden="true">⌄</span></summary>
      ${processOpen ? `<div class="run-summary" role="list" aria-label="${escapeHtml(t("run:overview.recordAria"))}">${renderResearchRecord(events, steps, overview, prefix, detailState)}</div>${renderDeveloperTrace(events, prefix, detailState)}` : ""}
    </details>
  </section>${failureCard}`;
}

export function formatRunElapsed(run) {
  const events = run.events || [];
  const startedEvent = events.find((event) => (
    event.type === "run_started"
    || (event.type === "orchestration_stage" && event.data.stage === "research_planning")
  ));
  const startedAt = Number(run.started_at || startedEvent?.ts || events[0]?.ts || 0);
  if (!startedAt) return "00:00";
  const endedAt = Number(run.ended_at || 0);
  const seconds = Math.max(0, Math.floor((endedAt || Date.now() / 1000) - startedAt));
  return `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
}

export function workflowStagePresentation(data) {
  const stage = data.stage || "";
  if (stage === "verification_started") {
    return {
      status: "running",
      title: stageTitle({ type: "workflow_stage", data }, "running"),
      detail: t("run:stage.verificationDetail"),
    };
  }
  if (stage === "verification_verdict" && (data.status === "error" || data.verdict === "error")) {
    return {
      status: "failed",
      title: t("run:verification.errorTitle"),
      detail: t("run:verification.errorDetail"),
    };
  }
  if (stage === "verification_verdict" && verificationStageCategory(data) === VERIFICATION_ADVISORY) {
    const count = verificationAuditCounts(data.verification_audit, data)?.issueCount ?? 0;
    return {
      status: "ended",
      title: t("run:verification.advisoryTitle", { count }),
      detail: t("run:verification.advisoryDetail", { count }),
    };
  }
  if (stage === "verification_verdict" && data.verdict === "pass") {
    return {
      status: "ok",
      title: t("run:verification.passedTitle"),
      detail: t("run:verification.passedDetail"),
    };
  }
  if (stage === "verification_verdict" && data.will_retry) {
    return {
      status: "failed",
      title: t("run:verification.retryTitle"),
      detail: t("run:verification.retryDetail"),
    };
  }
  if (stage === "repair_started") {
    const count = Math.max(0, Number(data.issue_count) || 0);
    const mode = data.mode === "regenerate" ? t("run:stage.repairModeRegenerate") : t("run:stage.repairModePatch");
    return {
      status: "running",
      title: stageTitle({ type: "workflow_stage", data }, "running"),
      detail: t("run:stage.repairDetail", { count, mode }),
    };
  }
  if (stage === "repair_completed") {
    return {
      status: "ok",
      title: t("run:stage.repairedTitle"),
      detail: t("run:stage.repairedDetail", { chars: Number(data.candidate_chars) || 0 }),
    };
  }
  if (stage === "verification_verdict" && data.exhausted) {
    const repaired = Math.max(0, Number(data.attempt) || 0) > 0;
    return {
      status: "failed",
      title: repaired ? t("run:verification.exhaustedRepairedTitle") : t("run:verification.exhaustedTitle"),
      detail: repaired
        ? t("run:verification.exhaustedRepairedDetail")
        : t("run:verification.exhaustedDetail"),
    };
  }
  return {
    status: "running",
    // data.label 是服务端 legacy 原文：未知阶段只原样呈现，不改写、不翻译。
    title: data.label || t("run:stage.fallbackTitle"),
    detail: stage || t("run:stage.fallbackDetail"),
  };
}

function orchestrationStageTitle(stage) {
  return ({
    scenario_routing: t("run:orchestration.scenarioRouting"),
    scenario_resolved: t("run:orchestration.scenarioResolved"),
    research_planning: t("run:orchestration.researchPlanning"),
    report_compilation: t("run:orchestration.reportCompilation"),
    artifact_verification: t("run:orchestration.artifactVerification"),
    summary_compilation: t("run:orchestration.summaryCompilation"),
  })[stage] || t("run:orchestration.fallbackTitle");
}

function orchestrationStageLabel(stage) {
  return ({
    scenario_routing: t("run:orchestration.labelScenarioRouting"),
    scenario_resolved: t("run:orchestration.labelScenarioResolved"),
    research_planning: t("run:orchestration.labelResearchPlanning"),
    report_compilation: t("run:orchestration.labelReportCompilation"),
    artifact_verification: t("run:orchestration.labelArtifactVerification"),
    summary_compilation: t("run:orchestration.labelSummaryCompilation"),
  })[stage] || t("run:orchestration.fallbackDetail");
}

function activityLabel(name) {
  return ({
    web_search: t("run:activity.webSearch"),
    web_fetch: t("run:activity.webFetch"),
    read_file: t("run:activity.readFile"),
    write_file: t("run:activity.writeFile"),
    create_file: t("run:activity.createFile"),
    bash: t("run:activity.bash"),
    add_task: t("run:activity.addTask"),
    update_task: t("run:activity.updateTask"),
    create_subagent: t("run:activity.createSubagent"),
    assign_task: t("run:activity.assignTask"),
    collect_reports: t("run:activity.collectReports"),
    submit_report: t("run:activity.submitReport"),
  })[name] || name;
}

/** One workspace owns its streams; preview navigation does not own or cancel them. */
export function createRunSubscriptions(EventSource) {
  const sources = new Map();
  function close(runId, source = sources.get(runId)) {
    source?.close();
    if (sources.get(runId) === source) sources.delete(runId);
  }
  function watch(run, receive) {
    if (terminalTypes.has(`run_${run.status}`)) return;
    close(run.id);
    const source = new EventSource(`/api/runs/${run.id}/events`);
    sources.set(run.id, source);
    for (const type of eventTypes) source.addEventListener(type, async raw => {
      // A replaced/disposed stream may still have queued browser callbacks.
      if (sources.get(run.id) !== source) return;
      const item = JSON.parse(raw.data);
      if (run.events.some(event => event.seq === item.seq)) return;
      run.events.push(item);
      await receive(item, source);
    });
    source.onerror = () => {
      if (terminalTypes.has(`run_${run.status}`)) close(run.id, source);
    };
  }
  function dispose() {
    for (const id of [...sources.keys()]) close(id);
  }
  return {watch, close, dispose, has: id => sources.has(id)};
}
