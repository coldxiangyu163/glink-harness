#!/usr/bin/env bash
# 无模型的交付检查；浏览器和真实执行面验收不由此入口触发。
set -euo pipefail

if [[ $# -ne 0 ]]; then
  echo '用法：bash scripts/check.sh（不接受参数；browser/live 验收需单独授权）' >&2
  exit 2
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if [[ -x "$REPO_ROOT/.venv/bin/python" ]]; then
  python="$REPO_ROOT/.venv/bin/python"
else
  python="$(command -v python3 || true)"
fi
node="$(command -v node || true)"
if [[ -z "$python" || -z "$node" ]]; then
  echo '[FAIL] 需要预先安装 Python >=3.12、pytest 和 Node >=24；不会自动安装依赖。' >&2
  exit 2
fi

scratch="$(mktemp -d "${TMPDIR:-/tmp}/pi-glink-check.XXXXXXXX")"
trap 'rm -rf -- "$scratch"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
mkdir -p "$scratch/home" "$scratch/tmp" "$scratch/config" "$scratch/cache" "$scratch/data"

# 仅保留运行程序所需的路径；不继承模型/云端凭据、代理或个人 pi 配置。
# 这不限制绝对路径访问和网络：测试仍须使用可信代码和外部服务替身。
clean_env=(env -i
  "PATH=$PATH"
  "HOME=$scratch/home"
  "TMPDIR=$scratch/tmp"
  "XDG_CONFIG_HOME=$scratch/config"
  "XDG_CACHE_HOME=$scratch/cache"
  "XDG_DATA_HOME=$scratch/data"
  "PYTHONPATH=$REPO_ROOT/host"
  'PYTHONUTF8=1'
  'PYTHONDONTWRITEBYTECODE=1'
  'PYTEST_DISABLE_PLUGIN_AUTOLOAD=1'
  'GIT_CONFIG_NOSYSTEM=1'
  'GIT_CONFIG_GLOBAL=/dev/null'
)

if ! "${clean_env[@]}" "$python" -c 'import sys; assert sys.version_info >= (3, 12), "需要 Python >=3.12"; import pytest; print("Python", sys.version.split()[0], "pytest", pytest.__version__)'; then
  echo '[FAIL] Python/pytest 前置检查失败；请在独立开发环境准备依赖。' >&2
  exit 2
fi
if ! "${clean_env[@]}" "$node" -e 'if (Number(process.versions.node.split(".")[0]) < 24) process.exit(2); console.log("Node", process.versions.node)'; then
  echo '[FAIL] 需要 Node >=24。' >&2
  exit 2
fi

shopt -s nullglob
# 前端事件模型（tests/web）与 agent 扩展的纯逻辑单元测试（tests/extensions；
# GLINK-SUB-02 强制前台守卫）。两者都不启动真实 pi、模型或浏览器。
node_tests=(tests/web/*.test.mjs tests/extensions/*.test.mjs)
if [[ ${#node_tests[@]} -eq 0 ]]; then
  echo '[FAIL] 没有发现 tests/web/*.test.mjs 或 tests/extensions/*.test.mjs；不把空测试集当通过。' >&2
  exit 2
fi
# dsh 的 out-of-tree bundle 单测（GLINK-DSH-02）：用 --import 注册 dsh seam 替身
# （WebError/deadline/credentials/launch-environment/schemastery），不打网络。
node_fetch_tests=(harnesses/dsh/bundles/*/tests/*.test.mjs)
node_fetch_stubs=harnesses/dsh/bundles/dsh-web-fetch-jina/tests/support/register-stubs.mjs
if [[ ${#node_fetch_tests[@]} -eq 0 || ! -f "$node_fetch_stubs" ]]; then
  echo '[FAIL] 没有发现 harnesses/dsh/bundles/*/tests/*.test.mjs 或 dsh seam 替身注册文件；不把空测试集当通过。' >&2
  exit 2
fi

run_check() {
  local name="$1" status
  shift
  printf '\n[RUN] %s\n' "$name"
  printf '命令：'
  printf ' %q' "$@"
  printf '\n'
  if "${clean_env[@]}" "$@"; then
    printf '[PASS] %s（exit 0）\n' "$name"
  else
    status=$?
    printf '[FAIL] %s（exit %s）\n' "$name" "$status" >&2
    exit "$status"
  fi
}

printf '\n[SKIP] 外部标准库逐值对齐：不读取个人标准库，不计入本次通过范围。\n'
# 前端事件常量由 host/pi_glink/event_schema.py 生成；生成物落后于 schema 即失败。
run_check '事件 schema 生成物是最新的' "$python" scripts/gen-event-schema.py --check
run_check 'Python（含仓库边界和检查入口回归）' \
  "$python" -m pytest tests -ra -p no:cacheprovider \
  --basetemp="$scratch/pytest" \
  --deselect=tests/test_ui_tokens.py::test_ui_tokens_match_standard_library
run_check 'Node（前端回归 + 扩展单元测试）' "$node" --test "${node_tests[@]}"
run_check 'Node（dsh fetch bundle 单元测试）' "$node" --import "./$node_fetch_stubs" --test "${node_fetch_tests[@]}"
printf '\n[PASS] 无模型必需检查完成；不包含浏览器、UI 对齐、真实 pi、Docker/E2B 或多租户验收。\n'
