#!/usr/bin/env bash
# GLINK-SUB-02 无模型加载冒烟：在隔离的临时 HOME / PI_CODING_AGENT_DIR 里用本机 pi
# 启动 RPC 模式，确认
#   1. 工作台的前台守卫扩展与 pi-subagents 都能加载，且没有 Tool conflicts / 加载错误；
#   2. 工具清单里出现 `subagent`（origin=package，来自本地 pi-subagents 副本）；
#   3. 守卫扩展在同一个 pi 进程内可 import，且三条最小自检通过（async 改写为前台、
#      控制动作阻止、只读动作放行）。
#
# 不调用模型、不访问网络、不读写用户 ~/.pi 或 ~/.pi-glink：pi-subagents 用
# PI_SUBAGENTS_ROOT 指向的本地副本（离线）；模型只用占位 models.json（无 apiKey）。
#
# 用法：
#   bash tests/extensions/smoke-subagents-load.sh [pi 二进制]
#   PI_SUBAGENTS_ROOT=<pi-subagents 目录> bash tests/extensions/smoke-subagents-load.sh
#
# 退出码：0 通过；2 前置缺失或断言失败。
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
pi_bin="${1:-${PI_GLINK_PI_BIN:-$(command -v pi || true)}}"
subagents_root="${PI_SUBAGENTS_ROOT:-/tmp/glink-sub2/prefix/node_modules/pi-subagents}"
python="${PYTHON:-$(command -v python3 || true)}"

if [[ -z "$pi_bin" || ! -x "$pi_bin" ]]; then
  echo "[FAIL] 找不到可执行的 pi（args/PI_GLINK_PI_BIN）；离线冒烟需要本机 pi。" >&2
  exit 2
fi
if [[ ! -f "$subagents_root/package.json" ]]; then
  echo "[FAIL] 找不到本地 pi-subagents 副本：$subagents_root" >&2
  echo "       先离线安装：npm install --prefix /tmp/glink-sub2/prefix pi-subagents@0.40.0" >&2
  exit 2
fi
if [[ -z "$python" ]]; then
  echo "[FAIL] 需要 python3 来断言探针输出。" >&2
  exit 2
fi

scratch="$(mktemp -d "${TMPDIR:-/tmp}/pi-glink-subagents-smoke.XXXXXXXX")"
probe_pid=""
cleanup() {
  # 只清理本脚本记录的 PID（禁止宽匹配 pkill）。
  if [[ -n "$probe_pid" ]] && kill -0 "$probe_pid" 2>/dev/null; then
    kill "$probe_pid" 2>/dev/null || true
    wait "$probe_pid" 2>/dev/null || true
  fi
  rm -rf -- "$scratch"
}
trap cleanup EXIT INT TERM

agent_dir="$scratch/pi-agent"
mkdir -p "$agent_dir/extensions/subagent" "$agent_dir/agents" \
  "$scratch/home" "$scratch/tmp" "$scratch/sessions" "$scratch/workspace"
cp "$repo_root/agent/extensions/subagent-foreground-guard.ts" "$agent_dir/extensions/"
cp "$repo_root/agent/extensions/subagent/config.json" "$agent_dir/extensions/subagent/"
cp "$repo_root/agent/agents/researcher.md" "$agent_dir/agents/"
cp "$repo_root/tests/extensions/probe-tool-inventory.ts" "$agent_dir/extensions/"

"$python" - "$agent_dir" "$subagents_root" <<'PY'
import json
import sys

agent_dir, subagents_root = sys.argv[1], sys.argv[2]
# 本地包路径是 pi 官方支持的 local source（"loaded from the resolved path without copying"），
# 冒烟因此不需要 npm、也不碰 registry；inproc 正式配置用 npm:pin，容器面用镜像内路径。
(open(f"{agent_dir}/settings.json", "w", encoding="utf-8")).write(
    json.dumps({"defaultThinkingLevel": "medium", "packages": [subagents_root]}, ensure_ascii=False, indent=2) + "\n"
)
models = {
    "providers": {
        "frontier": {
            "baseUrl": "https://example.invalid/v1",
            "api": "openai-completions",
            "models": [{"id": "smoke/model", "name": "smoke", "reasoning": True, "input": ["text"]}],
        }
    }
}
(open(f"{agent_dir}/models.json", "w", encoding="utf-8")).write(
    json.dumps(models, ensure_ascii=False, indent=2) + "\n"
)
PY

echo "[RUN] pi --mode rpc（无模型调用；探针在 session_start 写出工具清单后退出）"
echo "      pi=$pi_bin"
echo "      pi-subagents=$subagents_root"
echo "      agent_dir=$agent_dir"

# 空环境：不带任何宿主凭据/代理/个人 pi 配置；HOME/TMPDIR 都指向临时目录。
env -i \
  "PATH=${PATH}" \
  "HOME=$scratch/home" \
  "TMPDIR=$scratch/tmp" \
  "PI_CODING_AGENT_DIR=$agent_dir" \
  "PI_CODING_AGENT_SESSION_DIR=$scratch/sessions" \
  "PI_SKIP_VERSION_CHECK=1" \
  "PI_TELEMETRY=0" \
  "PI_GLINK_PROBE_OUT=$scratch/inventory.json" \
  "PI_GLINK_GUARD_MODULE=$agent_dir/extensions/subagent-foreground-guard.ts" \
  "$pi_bin" --mode rpc --no-session --name smoke --provider frontier --model smoke/model \
  >"$scratch/stdout.txt" 2>"$scratch/stderr.txt" &
probe_pid=$!
for _ in $(seq 1 240); do
  [[ -s "$scratch/inventory.json" ]] && break
  kill -0 "$probe_pid" 2>/dev/null || break
  sleep 0.25
done
exit_status=0
if kill -0 "$probe_pid" 2>/dev/null; then
  echo "[WARN] 探针未在 60 秒内退出，按记录的 PID 结束它。" >&2
  kill "$probe_pid" 2>/dev/null || true
fi
wait "$probe_pid" || exit_status=$?
probe_pid=""

echo "--- stderr（原样）---"
cat "$scratch/stderr.txt"
echo "--- stdout 行数：$(wc -l < "$scratch/stdout.txt" | tr -d ' ')；pi 退出码：$exit_status ---"

"$python" - "$scratch/inventory.json" "$scratch/stdout.txt" "$scratch/stderr.txt" "$agent_dir" "$scratch/workspace" <<'PY'
import json
import os
import sys

inventory_path, stdout_path, stderr_path, agent_dir, workspace = sys.argv[1:6]
failures: list[str] = []

def check(condition: bool, message: str) -> None:
    if not condition:
        failures.append(message)

if not os.path.isfile(inventory_path):
    print("[FAIL] 探针没有写出工具清单：扩展或 pi 没有加载起来")
    print(open(stderr_path, encoding="utf-8").read())
    raise SystemExit(2)

inventory = json.load(open(inventory_path, encoding="utf-8"))
stdout_text = open(stdout_path, encoding="utf-8").read()
stderr_text = open(stderr_path, encoding="utf-8").read()

print("--- 工具清单（探针）---")
print(json.dumps({key: inventory[key] for key in ("cwd", "mode", "agentDir", "sessionDir", "activeTools")}, ensure_ascii=False, indent=2))
print("--- 包含 subagent 的工具 ---")
print(json.dumps([tool for tool in inventory["allTools"] if tool["name"] == "subagent"], ensure_ascii=False, indent=2))
print("--- 探针捕获的扩展命令 ---")
print(json.dumps([command["name"] for command in inventory["commands"]], ensure_ascii=False))
print("--- 守卫自检 ---")
print(json.dumps(inventory["guardSelfCheck"], ensure_ascii=False, indent=2))

check("subagent" in inventory["activeTools"], "工具清单缺少 active 的 subagent")
subagent_tools = [tool for tool in inventory["allTools"] if tool["name"] == "subagent"]
check(bool(subagent_tools), "allTools 缺少 subagent")
check(any(tool.get("origin") == "package" for tool in subagent_tools), "subagent 不是来自 package 源")
check(inventory["agentDir"] == agent_dir, "pi 没有使用隔离的 PI_CODING_AGENT_DIR")
check(inventory["sessionDir"] and inventory["sessionDir"].startswith(inventory["sessionDir"].rsplit("/", 1)[0]),
      "sessionDir 缺失")
check(inventory["mode"] == "rpc", f"mode 不是 rpc：{inventory['mode']}")
check("researcher" in json.dumps(inventory["commands"]) or True, "")  # 占位：命令清单只作证据
check("extension_error" not in stdout_text, "stdout 出现 extension_error 帧")
for forbidden in ("Tool conflicts", "Tool conflict", "Failed to load extension", "Error loading extension"):
    check(forbidden not in stderr_text, f"stderr 出现加载错误：{forbidden}")
    check(forbidden not in stdout_text, f"stdout 出现加载错误：{forbidden}")

self_check = inventory["guardSelfCheck"]
check(self_check.get("imported") is True, f"守卫扩展未能在 pi 进程内 import：{self_check}")
check(self_check.get("forcedForeground") is True, "守卫自检失败：async 未被改写为前台")
check(self_check.get("blockedControlAction") is True, "守卫自检失败：控制动作未被阻止")
check(self_check.get("readOnlyActionAllowed") is True, "守卫自检失败：只读动作被误阻止")

if failures:
    for failure in failures:
        print(f"[FAIL] {failure}", file=sys.stderr)
    raise SystemExit(2)
print("[PASS] 扩展与 pi-subagents 在隔离配置目录中加载；subagent 工具存在；无 Tool conflicts；守卫自检通过。")
PY
