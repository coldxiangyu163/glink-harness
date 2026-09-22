/** requests: product-owned browser module; no startup side effects. */

import { getLocale, t } from './i18n.mjs';

export function updateChatFromMenu(chatId, body, request) {
  return request(`/api/chats/${chatId}`, { method: "PATCH", body });
}

export function branchChatFromMenu(chatId, request) {
  return request(`/api/chats/${chatId}/branch`, { method: "POST", body: {} });
}

export function deleteChatFromMenu(chatId, request) {
  return request(`/api/chats/${chatId}`, { method: "DELETE" });
}

export function storeArtifactInLibraryRequest(artifactId, projectId, request) {
  const body = { artifact_id: artifactId };
  if (projectId) body.project_id = projectId;
  return request("/api/library/from-artifact", { method: "POST", body });
}

export async function reviewThreadRequest(chatId, threadId, action, body, request) {
  const base = `/api/chats/${chatId}/review/threads`;
  const suffix = action === "create" ? "" : `/${encodeURIComponent(threadId)}${action === "patch" ? "" : `/${action}`}`;
  return request(base + suffix, { method: action === "patch" ? "PATCH" : "POST", ...(body === undefined ? {} : { body }) });
}

export async function handleComposerPaste(event, {
  upload,
  notify,
} = {}) {
  const files = [...(event.clipboardData?.files || [])];
  if (!files.length) return [];

  event.preventDefault();
  notify(t("workspace:toast.pastePending", { count: files.length }));
  try {
    const uploaded = await upload(files, { selectUploaded: true });
    const failedCount = files.length - uploaded.length;
    if (uploaded.length) {
      notify(failedCount
        ? t("workspace:toast.attachmentsAddedPaste", { count: uploaded.length, failed: failedCount })
        : t("workspace:toast.attachmentsAdded", { count: uploaded.length }));
    } else {
      notify(t("errors:attachmentFailed"));
    }
    return uploaded;
  } catch (err) {
    notify(t("errors:attachmentFailedDetail", { detail: err.message || t("errors:unexpected") }));
    return [];
  }
}

export function resetNewResearchScenario(targetState) {
  targetState.currentScenario = null;
  return targetState;
}

export function selectedScenarioFromCatalog(scenarios, scenarioId) {
  return scenarios.find((item) => item.id === scenarioId) || null;
}

export function buildChatCreateBody({
  title, prompt, scenarioId, projectId, tierId, tiersAvailable,
}) {
  const body = { title, prompt, project_id: projectId };
  if (scenarioId) body.scenario_id = scenarioId;
  if (tiersAvailable) body.tier_id = tierId;
  return body;
}

export function runStartOutcome(result) {
  if (result?.clarification === true && result.message?.role === "assistant") {
    return { kind: "clarification", message: result.message };
  }
  if (result?.run?.id) return { kind: "run", run: result.run };
  throw new Error(t("errors:runStartInvalid"));
}

export function scenarioBadgeLabel(chat, scenarios, routing = false, runStatus = "") {
  if (chat?.scenario_source === "pending") {
    if (["completed", "failed", "cancelled"].includes(runStatus)) return t("workspace:scenario.badgeUnmatched");
    return routing ? t("workspace:scenario.badgeRouting") : t("workspace:scenario.badgeAuto");
  }
  const scenario = selectedScenarioFromCatalog(scenarios, chat?.scenario_id);
  if (!scenario) return t("workspace:scenario.badgeGeneric");
  // 场景名由配置下发中英两份；这里跟随界面语言，缺英文时回落原文。
  const localized = getLocale() === "en" && scenario.name_en ? scenario.name_en : scenario.name;
  return localized || t("workspace:scenario.badgeGeneric");
}

export function scenarioRoutingNotice(value) {
  const chat = value?.data?.stage === "scenario_resolved" ? value.data : value;
  // auto 命中专用场景时不弹提示：时间线已有“已锁定研究场景”节点，badge 同步更新，弹框冗余。
  if (chat.scenario_source === "fallback") {
    return t("workspace:scenario.routingFallback");
  }
  return "";
}
