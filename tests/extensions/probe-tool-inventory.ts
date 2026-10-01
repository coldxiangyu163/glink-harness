/**
 * 无模型加载冒烟用的探针扩展（tests/extensions/smoke-subagents-load.sh）。
 *
 * pi 没有 CLI 的“列出工具”命令，所以这里用官方 extension API 的 `session_start`
 * 钩子把当前进程的工具清单写成一个 JSON 文件，然后请求正常退出。冒烟脚本只读这个
 * 文件与 pi 的 stderr/stdout，不调用模型、不访问网络。
 *
 * 可选：`GLINK_GUARD_MODULE` 指向工作台的前台守卫扩展时，探针还会在同一个 pi
 * 进程里 import 它并跑三条最小自检（async 改写、控制动作阻止、只读动作放行），
 * 结果写进 `guardSelfCheck`——这样“扩展能被 pi 加载”不只是靠“没有报错”推断。
 */

import fs from "node:fs";
import { pathToFileURL } from "node:url";

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

type GuardModule = {
  guardSubagentCall: (
    input: Record<string, unknown>,
    options: { workspace: string },
  ) => { block?: boolean; reason?: string } | undefined;
};

async function guardSelfCheck(workspace: string): Promise<Record<string, unknown>> {
  const modulePath = process.env.GLINK_GUARD_MODULE;
  if (!modulePath) return { skipped: "GLINK_GUARD_MODULE 未设置" };
  try {
    const guard = (await import(pathToFileURL(modulePath).href)) as GuardModule;
    const forced = { agent: "researcher", task: "t", async: true };
    const allowed = guard.guardSubagentCall(forced, { workspace }) === undefined && forced.async === false;
    const blocked = guard.guardSubagentCall(
      { action: "resume", id: "run-1" },
      { workspace },
    )?.block === true;
    const readOnly = guard.guardSubagentCall({ action: "status" }, { workspace }) === undefined;
    return { imported: true, forcedForeground: allowed, blockedControlAction: blocked, readOnlyActionAllowed: readOnly };
  } catch (error) {
    return { imported: false, error: error instanceof Error ? error.message : String(error) };
  }
}

export default function (pi: ExtensionAPI) {
  pi.on("session_start", async (_event, ctx) => {
    const out = process.env.GLINK_PROBE_OUT;
    if (!out) return;
    const inventory = {
      cwd: ctx.cwd,
      mode: ctx.mode,
      agentDir: process.env.PI_CODING_AGENT_DIR ?? null,
      sessionDir: process.env.PI_CODING_AGENT_SESSION_DIR ?? null,
      activeTools: pi.getActiveTools(),
      allTools: pi.getAllTools().map((tool) => ({
        name: tool.name,
        origin: tool.sourceInfo?.origin ?? null,
        source: tool.sourceInfo?.source ?? null,
        path: tool.sourceInfo?.path ?? null,
      })),
      commands: pi.getCommands().map((command) => ({
        name: command.name,
        source: command.source ?? null,
      })),
      guardSelfCheck: await guardSelfCheck(ctx.cwd),
    };
    fs.writeFileSync(out, `${JSON.stringify(inventory, null, 2)}\n`, "utf-8");
    ctx.shutdown();
  });
}
