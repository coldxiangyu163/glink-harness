/**
 * pi-glink 自有扩展：把 pi-subagents 的派发强制在前台同步执行。
 *
 * 背景：pi-subagents 0.40.0 没有“禁止 async/detached”的配置或环境开关，模型传入
 * `async: true` 即走 detached 子进程（GLINK-SUB-01，docs/pi-integration-notes.md §8）。
 * 本仓库改用官方 `tool_call` 钩子（pi docs/extensions.md：钩子可以就地改写
 * `event.input`，也可以 `block` 阻止本次调用；hook handler 抛错按 pi 语义阻止工具）
 * 把这一层收回到主 run 的同步工具调用里——生命周期、取消、超时与沙箱回收因此自动覆盖。
 *
 * 处置原则（偏向可预测，详见 docs/pi-integration-notes.md 的 subagents 小节）：
 *
 * 1. 执行类调用（无 `action`）：`async` 一律改写为 `false`。前台是同一个工具的受支持
 *    形态，改写不损失能力，又能盖掉 agent frontmatter 的 `async: true` 与配置默认值；
 *    前台 `tasks` 数组并行（2–4 个 researcher）保持放行。
 * 2. 管理/控制类 `action`：只放行只读动作（`status`/`doctor`/`list`/`get`/`models`），
 *    其余（resume/steer/append-step/stop/interrupt/schedule…/watchdog…/
 *    grant-spawn-budget/create/update/delete/eject/disable/enable/reset）无法保证
 *    生命周期，直接阻止并给出可理解的错误。未知 action 同样阻止（fail-closed）。
 * 3. 会把内容或资源送出本 run 的参数：`share: true`（上传会话到外部 Gist）、
 *    `worktree: true`（在 run 目录外创建 git worktree）阻止。
 * 4. 路径参数（`cwd`、`sessionDir`、`chainDir`、`output`，含并行/链式任务里的 `cwd`）
 *    必须落在本次 run 的工作区内：宿主只对本 run 工作区做收尾脱敏与目录边界保护，
 *    写到 `~`、仓库或运行目录之外的内容不在覆盖范围内；`chainDir` 不看模型给的值，
 *    直接改写为工作区内的 `.pi-subagents/chain-runs`（链式运行的产物因此进入收尾脱敏范围）。
 * 5. `subagent_wait`（等待工具）直接阻止并说明原因：本运行没有后台作业可等。
 *
 * 兜底：任何内部异常都按 fail-closed 阻止该次调用，而不是把未经检查的参数放给
 * pi-subagents。pi 本身也会把 `tool_call` handler 的异常当作阻止。
 */

import os from "node:os";
import path from "node:path";

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

/** pi-subagents 注册的父级派发工具名（src/extension/index.ts:403）。 */
export const SUBAGENT_TOOL_NAME = "subagent";

/**
 * pi-subagents 的等待工具（src/runs/background/wait-tool.ts:22）。
 *
 * 它与配置一起被“退化为空操作”，但工具本身仍然注册在模型面前；本运行里没有任何
 * 后台作业可等，所以直接阻止并告诉模型继续，而不是让它白花一轮。
 */
export const WAIT_TOOL_NAME = "subagent_wait";

/** 阻止 `subagent_wait` 时给模型的原因（所有子代理都是前台同步调用）。 */
export const WAIT_TOOL_BLOCK_REASON =
	"本次运行的所有子代理派发都是前台同步调用，没有后台作业可等；" +
	"subagent_wait 在此没有意义，直接继续取证与整合。";

/**
 * 允许放行的只读管理动作（agent-management.ts:1152 的分派表里仅读取状态/定义的项）。
 * 其余 action 一律阻止。
 */
export const READ_ONLY_ACTIONS = new Set(["status", "doctor", "list", "get", "models"]);

/** `chainDir` 被改写到工作区内的这个子目录（不保留模型给的路径）。 */
export const WORKSPACE_CHAIN_DIR_RELATIVE = ".pi-subagents/chain-runs";

/** 需要留在本 run 工作区内的路径参数；数组内的同名键同样检查。 */
const WORKSPACE_PATH_FIELDS = new Set(["cwd", "sessionDir", "output"]);

export interface GuardDecision {
	readonly block: boolean;
	readonly reason?: string;
}

export interface GuardOptions {
	/** 主 run 的工作区（pi 进程 cwd）；路径参数必须落在它里面。 */
	readonly workspace: string;
	/** 用于把 `~/…` 展开成绝对路径；默认取 `os.homedir()`。 */
	readonly home?: string;
}

function insideWorkspace(workspace: string, candidate: string): boolean {
	const base = path.resolve(workspace);
	const target = path.resolve(candidate);
	return target === base || target.startsWith(base + path.sep);
}

/** `~/x` 与 `~` 按 pi-subagents 的 `expandTilde` 语义展开后再判定。 */
function resolveFieldPath(value: string, options: GuardOptions): string {
	const home = options.home ?? os.homedir();
	if (value === "~") return path.resolve(home);
	if (value.startsWith("~/")) return path.resolve(home, value.slice(2));
	return path.resolve(options.workspace, value);
}

function firstEscapingPath(
	value: unknown,
	field: string,
	options: GuardOptions,
	depth = 0,
): { field: string; value: string } | undefined {
	if (depth > 4 || value === null || typeof value !== "object") return undefined;
	if (Array.isArray(value)) {
		for (const item of value) {
			const found = firstEscapingPath(item, field, options, depth + 1);
			if (found) return found;
		}
		return undefined;
	}
	for (const [key, entry] of Object.entries(value)) {
		const label = WORKSPACE_PATH_FIELDS.has(key) ? key : `${field}.${key}`;
		if (typeof entry === "string" && WORKSPACE_PATH_FIELDS.has(key)) {
			if (!insideWorkspace(options.workspace, resolveFieldPath(entry, options))) {
				return { field: label, value: entry };
			}
			continue;
		}
		const found = firstEscapingPath(entry, label, options, depth + 1);
		if (found) return found;
	}
	return undefined;
}

function blocked(reason: string): GuardDecision {
	return { block: true, reason };
}

function managementDecision(action: string): GuardDecision | undefined {
	if (READ_ONLY_ACTIONS.has(action)) return undefined;
	return blocked(
		`subagent 的 action="${action}" 属于后台/定时/控制/管理面，本次运行只在主 run 内做前台同步派发。` +
			`允许的 action 只有只读的 ${[...READ_ONLY_ACTIONS].join(" / ")}；` +
			"需要深度取证时直接（并行）派 researcher，并自己整合与核查。",
	);
}

/**
 * `action` 与 pi-subagents 自身的归一化保持一致：`single`/`parallel`/`tasks` 是
 * 执行形态别名（src/runs/foreground/subagent-executor.ts:3636），空串与 null 同样
 * 落回执行路径；只有真正的管理/控制动作进入只读判定。
 */
function classifyAction(raw: unknown): "execution" | "management" | "invalid" {
	if (raw === undefined || raw === null) return "execution";
	if (typeof raw !== "string") return "invalid";
	const action = raw.trim().toLowerCase();
	if (action === "") return "execution";
	if (action === "single" || action === "parallel" || action === "tasks") return "execution";
	return "management";
}

/**
 * 检查一次 `subagent` 调用，必要时**就地改写** `input`（`async` 归零），
 * 返回阻止决定或 `undefined`（放行）。
 */
export function guardSubagentCall(
	input: Record<string, unknown>,
	options: GuardOptions,
): GuardDecision | undefined {
	switch (classifyAction(input.action)) {
		case "invalid":
			return blocked("subagent 的 action 必须是字符串；本次运行不接受无法检查的管理动作。");
		case "management": {
			const action = (input.action as string).trim().toLowerCase();
			return managementDecision(action);
		}
		default:
			break;
	}
	if (input.share === true) {
		return blocked(
			"subagent 的 share:true 会把子代理会话上传到外部 Gist，内容离开本 run 目录；本次运行禁止。",
		);
	}
	if (input.worktree === true) {
		return blocked(
			"subagent 的 worktree:true 会在本 run 目录之外创建 git worktree，其生命周期不由本次 run 的回收路径覆盖；本次运行禁止。",
		);
	}
	const escaping = firstEscapingPath(input, "subagent", options);
	if (escaping) {
		return blocked(
			`subagent 的参数 ${escaping.field}="${escaping.value}" 指向本次运行的工作区之外；` +
				"子代理只能在本次运行的工作区内读写（宿主只对该工作区做脱敏与边界保护）。",
		);
	}
	// 链式运行的产物目录固定在工作区内（pi-subagents 默认落在 OS 临时目录，
	// 不在宿主收尾脱敏的覆盖范围内）；runId 会由 pi-subagents 追加，不会互相覆盖。
	input.chainDir = path.join(path.resolve(options.workspace), WORKSPACE_CHAIN_DIR_RELATIVE);
	// 前台同步派发：显式 false 同时盖掉 agent frontmatter 的 async 与配置默认值。
	input.async = false;
	return undefined;
}

/**
 * fail-closed 包装：内部异常（含属性访问器抛错）一律转为阻止决定。
 */
export function safeGuardSubagentCall(
	input: unknown,
	options: GuardOptions,
): GuardDecision | undefined {
	try {
		if (input === null || typeof input !== "object" || Array.isArray(input)) {
			return blocked("subagent 的调用参数不是对象；本次运行拒绝无法检查的派发。");
		}
		return guardSubagentCall(input as Record<string, unknown>, options);
	} catch (error) {
		const detail = error instanceof Error ? error.message : String(error);
		return blocked(`subagent 强制前台检查失败（${detail}）；按 fail-closed 阻止本次派发。`);
	}
}

export default function (pi: ExtensionAPI) {
	pi.on("tool_call", (event, ctx) => {
		if (event.toolName === WAIT_TOOL_NAME) {
			return { block: true as const, reason: WAIT_TOOL_BLOCK_REASON };
		}
		if (event.toolName !== SUBAGENT_TOOL_NAME) return undefined;
		return safeGuardSubagentCall(event.input, { workspace: ctx.cwd });
	});
}
