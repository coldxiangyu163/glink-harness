#!/usr/bin/env bash
# GLINK-SUB-01 spike：静态审计 pi-subagents 是否存在“禁止 async/detached、强制前台同步”的配置开关。
#
# 用法：
#   bash scripts/spikes/pi_subagents/audit_async_switch.sh <pi-subagents-root>
#   PI_SUBAGENTS_ROOT=<pi-subagents-root> bash scripts/spikes/pi_subagents/audit_async_switch.sh
#
# <pi-subagents-root> 是安装/解包后的 pi-subagents 目录（含 package.json 与 src/）。
# 只读、离线：不读取个人 pi 配置，不安装依赖，不触碰真实运行数据。
#
# 退出码：
#   0  未发现强制前台/禁用 async 的配置开关（本 spike 的 BLOCKER 结论对该版本成立）
#   1  发现疑似配置开关或环境变量；结论失效，需重开 spike 实测该开关
#   2  用法错误或目录结构不符（上游结构可能已变化）
set -euo pipefail

usage() {
	cat >&2 <<'EOF'
用法：bash scripts/spikes/pi_subagents/audit_async_switch.sh <pi-subagents-root>
   或：PI_SUBAGENTS_ROOT=<pi-subagents-root> bash scripts/spikes/pi_subagents/audit_async_switch.sh

<pi-subagents-root> 是安装/解包后的 pi-subagents 目录（含 package.json 与 src/）。只读、离线。
退出码：0=未发现强制前台开关；1=发现疑似开关（需重开验证）；2=用法/路径错误。
EOF
}

root="${1:-${PI_SUBAGENTS_ROOT:-}}"
if [ -z "$root" ]; then
	usage
	exit 2
fi
if [ ! -f "$root/package.json" ] || [ ! -d "$root/src" ]; then
	echo "错误：'$root' 不像 pi-subagents 根目录（缺 package.json 或 src/）" >&2
	exit 2
fi

cd "$root"

version="$(sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' package.json | head -n 1)"
echo "=== pi-subagents config-surface audit ==="
echo "root:    $root"
echo "version: ${version:-unknown}"
echo

echo "--- 1. ExtensionConfig 全部键（src/shared/types.ts）---"
config_block="$(sed -n '/export interface ExtensionConfig {/,/^}/p' src/shared/types.ts)"
if [ -z "$config_block" ]; then
	echo "FAIL: 未找到 ExtensionConfig（上游结构可能已变，请人工核对）"
	exit 2
fi
printf '%s\n' "$config_block"
keys="$(printf '%s\n' "$config_block" | sed -n 's/^[[:space:]]*\([A-Za-z][A-Za-z0-9_]*\)?\{0,1\}:.*/\1/p')"
echo
echo "键列表: $(printf '%s' "$keys" | tr '\n' ' ')"
echo

echo "--- 2. 疑似“禁止 async / 强制前台”的配置键 ---"
# 已知的 async 方向键：asyncByDefault/forceTopLevelAsync 只能把 launch 推向 async，asyncWidget 只是 TUI 组件开关。
# 其余任何含 async/foreground/detach/sync 的配置键都视为可疑，交给人工复核。
known_async_keys='asyncByDefault|forceTopLevelAsync|asyncWidget'
suspect_keys="$(printf '%s\n' "$keys" | grep -Ei 'async|foreground|detach|\bsync' | grep -vxE "$known_async_keys" || true)"
if [ -n "$suspect_keys" ]; then
	printf 'FOUND: %s\n' "$suspect_keys"
else
	echo "none"
fi
echo

echo "--- 3. 强制 async 方向的键（反向开关，仅说明配置面只能把 launch 推向 async）---"
printf '%s\n' "$keys" | grep -E '^(asyncByDefault|forceTopLevelAsync)$' || true
echo

echo "--- 4. 模型可见的 subagent 工具 async 参数（src/extension/schemas.ts）---"
grep -nE '^[[:space:]]*async: Type\.Optional' src/extension/schemas.ts || echo "MISSING（async 参数已从模型面移除，需重新评估）"
echo

echo "--- 5. 前台/异步分派逻辑（src/runs/foreground/subagent-executor.ts）---"
grep -n 'const runsForeground = ' src/runs/foreground/subagent-executor.ts || echo "MISSING"
grep -n 'applyForceTopLevelAsyncOverride(requestParams' src/runs/foreground/subagent-executor.ts || true
echo

echo "--- 6. async launch 的 detached 进程与父环境继承（src/runs/background/async-execution.ts）---"
grep -n 'detached: true' src/runs/background/async-execution.ts || true
grep -n '\.\.\.process\.env' src/runs/background/async-execution.ts || true
echo

echo "--- 7. 非必需面默认值/开关 ---"
echo "[scheduledRuns] 默认关闭，需显式 enabled:true"
grep -n 'scheduledRuns?.enabled === true' src/runs/background/scheduled-runs.ts || echo "MISSING"
echo "[watchdog] 默认关闭（DEFAULT_WATCHDOG_CONFIG）"
grep -n 'export const DEFAULT_WATCHDOG_CONFIG' -A 2 src/watchdog/settings.ts || echo "MISSING"
echo "[intercomBridge] 未配置时 mode 解析为 always"
grep -n 'export function resolveIntercomBridgeMode' -A 3 src/intercom/intercom-bridge.ts || echo "MISSING"
echo "[artifactDir] project/session/temp 可配"
grep -n 'artifactDir?: ArtifactDirPreference' src/shared/types.ts || true
echo

echo "--- 8. 环境变量 kill switch 扫描（期望 none）---"
env_hits="$(grep -rniE '(NO_ASYNC|DISABLE_ASYNC|FORCE_FOREGROUND|SYNC_ONLY)' src || true)"
if [ -n "$env_hits" ]; then
	printf '%s\n' "$env_hits"
else
	echo "none"
fi
echo

echo "VERDICT: version=$version keys=$(( $(printf '%s\n' "$keys" | grep -c .) ))"
if [ -n "$suspect_keys" ] || [ -n "$env_hits" ]; then
	echo "SWITCH_FOUND：发现疑似强制前台/禁用 async 的开关；BLOCKER 结论对该版本不成立，需重开 spike 实测。"
	exit 1
fi
echo "NO_SWITCH：配置面与环境中均无“禁止 async / 强制前台同步”的开关；模型传入 async:true 即走 detached 路径。"
exit 0
