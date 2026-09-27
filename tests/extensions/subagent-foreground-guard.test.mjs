/**
 * GLINK-SUB-02：pi-glink 自有扩展把 pi-subagents 的派发强制在前台同步执行。
 *
 * 这里直接对扩展导出的纯判定函数做正反例（每个 detach 入口都要有放行/改写或阻止的
 * 断言），并用一个假的 ExtensionAPI 走一遍真实注册路径：
 *   - 非 subagent 工具不受影响；
 *   - `async: true` / 未给 async / agent frontmatter 的 async 默认值 → 一律改写为 false；
 *   - 同步 `tasks`/`chain` 形态放行且不改动内容；
 *   - 后台/定时/控制/管理动作、`share`、`worktree`、工作区外路径 → 阻止并给出原因；
 *   - handler 内部异常（属性访问器抛错、参数不是对象）→ 按 fail-closed 阻止。
 *
 * 扩展源码是 TypeScript（pi 用 jiti 直接加载）；Node ≥24 的类型剥离让本测试可以
 * 直接 import，无需构建步骤。
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import factory, {
  READ_ONLY_ACTIONS,
  SUBAGENT_TOOL_NAME,
  WAIT_TOOL_BLOCK_REASON,
  WAIT_TOOL_NAME,
  WORKSPACE_CHAIN_DIR_RELATIVE,
  guardSubagentCall,
  safeGuardSubagentCall,
} from "../../harnesses/pi/extensions/subagent-foreground-guard.ts";

const WORKSPACE = "/tmp/pi-glink-guard-workspace";
const HOME = "/Users/example";
const OPTIONS = { workspace: WORKSPACE, home: HOME };

/** 通过假的 ExtensionAPI 抓取注册的 tool_call handler。 */
function registeredHandler() {
  const registrations = [];
  factory({ on: (event, handler) => registrations.push({ event, handler }) });
  assert.equal(registrations.length, 1, "扩展只注册一个 tool_call 钩子");
  assert.equal(registrations[0].event, "tool_call");
  return registrations[0].handler;
}

function expectBlocked(input) {
  const decision = safeGuardSubagentCall(input, OPTIONS);
  assert.ok(decision, `应当阻止：${JSON.stringify(input)}`);
  assert.equal(decision.block, true);
  assert.ok(
    typeof decision.reason === "string" && decision.reason.length > 0,
    "阻止必须给模型一条可理解的原因",
  );
  return decision.reason;
}

test("非 subagent 的工具调用不被本扩展介入", () => {
  assert.equal(guardSubagentCall.length > 0, true);
  assert.equal(SUBAGENT_TOOL_NAME, "subagent");
  const handler = registeredHandler();
  assert.equal(handler({ toolName: "bash", input: { command: "ls" } }, { cwd: WORKSPACE }), undefined);
  assert.equal(handler({ toolName: "web_search", input: { queries: ["q"] } }, { cwd: WORKSPACE }), undefined);
});

test("subagent_wait 被阻止并说明“没有后台作业可等”", () => {
  assert.equal(WAIT_TOOL_NAME, "subagent_wait");
  const handler = registeredHandler();
  const decision = handler({ toolName: WAIT_TOOL_NAME, input: {} }, { cwd: WORKSPACE });
  assert.ok(decision);
  assert.equal(decision.block, true);
  assert.equal(decision.reason, WAIT_TOOL_BLOCK_REASON);
  assert.match(decision.reason, /前台同步/);
});

// -- 执行形态：async 一律改写为前台 -------------------------------------------

test("async:true 被改写为前台同步（不阻止）", () => {
  const input = { agent: "researcher", task: "查一手来源", async: true };
  const decision = guardSubagentCall(input, OPTIONS);
  assert.equal(decision, undefined);
  assert.equal(input.async, false);
});

test("没有 async 字段时也显式写成 false（盖掉 agent/配置默认值）", () => {
  const input = { agent: "researcher", task: "查一手来源" };
  assert.equal(guardSubagentCall(input, OPTIONS), undefined);
  assert.equal(input.async, false);
});

test("agent frontmatter 的 async:true 默认值被显式 false 覆盖（执行路径必经此处）", () => {
  // pi-subagents 只在 params.async === undefined 时应用 agent.defaultAsync
  // （subagent-executor.ts:1799），显式 false 因此是这一层的保险。
  const input = { agent: "custom-async-agent", task: "t", async: undefined };
  assert.equal(guardSubagentCall(input, OPTIONS), undefined);
  assert.equal(input.async, false);
});

test("同步 tasks 数组并行原样放行（feynman 同款前台形态）", () => {
  const tasks = [
    { agent: "researcher", task: "法域 A 取证", output: "notes-1.md" },
    { agent: "researcher", task: "法域 B 取证", output: "notes-2.md" },
    { agent: "researcher", task: "主题线 C 取证" },
  ];
  const input = { tasks: tasks.map((task) => ({ ...task })), concurrency: 3 };
  assert.equal(guardSubagentCall(input, OPTIONS), undefined);
  assert.deepEqual(input.tasks, tasks, "同步派发的任务内容不被改写");
  assert.equal(input.async, false);
});

test("chain 形态同样被拉回前台", () => {
  const input = { chain: [{ agent: "researcher", task: "step 1" }, { agent: "researcher", task: "step 2" }] };
  assert.equal(guardSubagentCall(input, OPTIONS), undefined);
  assert.equal(input.async, false);
});

test("执行形态别名（action: single/parallel/tasks）按执行处理，不是管理动作", () => {
  for (const action of ["single", "parallel", "tasks"]) {
    const input = { action, tasks: [{ agent: "researcher", task: "t", cwd: "sub" }] };
    assert.equal(guardSubagentCall(input, OPTIONS), undefined, action);
    assert.equal(input.async, false, action);
  }
});

test("空串 action 与 pi-subagents 的归一化一致：按执行处理", () => {
  const input = { action: "", agent: "researcher", task: "t" };
  assert.equal(guardSubagentCall(input, OPTIONS), undefined);
  assert.equal(input.async, false);
});

// -- 只读管理动作放行 ----------------------------------------------------------

test("只读管理动作放行：status / doctor / list / get / models", () => {
  for (const action of READ_ONLY_ACTIONS) {
    const input = { action, id: "run-1" };
    assert.equal(guardSubagentCall(input, OPTIONS), undefined, action);
    assert.equal("async" in input, false, `${action} 不需要改写执行形态`);
  }
});

// -- 后台/定时/控制/管理动作阻止 ----------------------------------------------

test("后台/定时/恢复/控制类 action 全部阻止", () => {
  const blockedActions = [
    "resume",
    "steer",
    "append-step",
    "stop",
    "interrupt",
    "schedule",
    "schedule-list",
    "schedule-status",
    "schedule-cancel",
    "watchdog.status",
    "watchdog.check",
    "watchdog.configure",
    "watchdog.recommend-model",
    "grant-spawn-budget",
  ];
  for (const action of blockedActions) {
    const reason = expectBlocked({ action, id: "run-1" });
    assert.match(reason, /action/);
  }
});

test("改定义/删定义的 action 阻止（避免运行中改写子代理定义）", () => {
  for (const action of ["create", "update", "delete", "eject", "disable", "enable", "reset"]) {
    expectBlocked({ action, agent: "researcher", config: {} });
  }
});

test("未知或非字符串 action 阻止（fail-closed）", () => {
  expectBlocked({ action: "definitely-not-an-action" });
  expectBlocked({ action: 7 });
});

// -- 内容与资源出界：share / worktree -----------------------------------------

test("share:true 阻止（会话上传外部 Gist）", () => {
  const reason = expectBlocked({ agent: "researcher", task: "t", share: true });
  assert.match(reason, /share/);
  const input = { agent: "researcher", task: "t", share: false };
  assert.equal(guardSubagentCall(input, OPTIONS), undefined);
});

test("worktree:true 阻止（run 目录之外的 git worktree 生命周期不由本 run 保证）", () => {
  const reason = expectBlocked({ tasks: [{ agent: "researcher", task: "t" }], worktree: true });
  assert.match(reason, /worktree/);
});

// -- 路径边界：只允许本 run 工作区内部 ----------------------------------------

test("工作区外的 cwd / sessionDir / output 被阻止（含 ~ 展开）", () => {
  const escapers = [
    { cwd: "../outside" },
    { cwd: "~/elsewhere" },
    { cwd: "/tmp/elsewhere" },
    { sessionDir: "/tmp/pi-subagent-sessions" },
    { sessionDir: "~/sessions" },
    { output: "/etc/passwd" },
    { tasks: [{ agent: "researcher", task: "t", cwd: "../../escape" }] },
    { chain: [{ agent: "researcher", task: "t", cwd: "/tmp/escape" }] },
  ];
  for (const input of escapers) {
    const reason = expectBlocked({ agent: "researcher", task: "t", ...input });
    assert.match(reason, /工作区之外/);
  }
});

test("工作区内的相对/绝对路径放行", () => {
  const allowed = [
    { cwd: "sub" },
    { cwd: `${WORKSPACE}/nested` },
    { sessionDir: ".pi-subagents/sessions" },
    { output: "notes-1.md" },
    { tasks: [{ agent: "researcher", task: "t", cwd: "parts/a" }] },
  ];
  for (const input of allowed) {
    const guardInput = { agent: "researcher", task: "t", ...input };
    assert.equal(guardSubagentCall(guardInput, OPTIONS), undefined, JSON.stringify(input));
  }
});

test("chainDir 被强制改写进工作区（pi-subagents 默认落在 OS 临时目录）", () => {
  const input = { chain: [{ agent: "researcher", task: "t" }], chainDir: "/tmp/elsewhere/chain" };
  assert.equal(guardSubagentCall(input, OPTIONS), undefined);
  assert.equal(input.chainDir, `${WORKSPACE}/${WORKSPACE_CHAIN_DIR_RELATIVE}`);
});

test("没有 chainDir 时也写进工作区（不给 OS 临时目录留缺口）", () => {
  const input = { agent: "researcher", task: "t" };
  assert.equal(guardSubagentCall(input, OPTIONS), undefined);
  assert.equal(input.chainDir, `${WORKSPACE}/${WORKSPACE_CHAIN_DIR_RELATIVE}`);
});

// -- fail-closed ----------------------------------------------------------------

test("handler 内部异常按 fail-closed 阻止（属性访问器抛错）", () => {
  const hostile = {
    get action() {
      throw new Error("hostile input");
    },
  };
  const decision = safeGuardSubagentCall(hostile, OPTIONS);
  assert.ok(decision);
  assert.equal(decision.block, true);
  assert.match(decision.reason, /fail-closed/);
  assert.match(decision.reason, /hostile input/);
});

test("回调路径上的异常同样阻止该次调用", () => {
  const handler = registeredHandler();
  const hostile = {
    get action() {
      throw new Error("getter exploded");
    },
  };
  const decision = handler({ toolName: "subagent", input: hostile }, { cwd: WORKSPACE });
  assert.ok(decision);
  assert.equal(decision.block, true);
});

test("参数不是对象时阻止", () => {
  for (const value of [null, undefined, 42, "subagent", ["tasks"]]) {
    const decision = safeGuardSubagentCall(value, OPTIONS);
    assert.ok(decision, String(value));
    assert.equal(decision.block, true);
  }
});

test("正常派发经注册的 handler 走通并落回前台", () => {
  const handler = registeredHandler();
  const input = { tasks: [{ agent: "researcher", task: "两路取证" }], async: true };
  assert.equal(handler({ toolName: "subagent", input }, { cwd: WORKSPACE }), undefined);
  assert.equal(input.async, false);
});
