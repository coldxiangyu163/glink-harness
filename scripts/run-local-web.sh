#!/usr/bin/env bash
# 启动本机工作台宿主 / start the local workbench host.
#
# 用法 / usage:
#   scripts/run-local-web.sh                 # 读取 .env，监听 127.0.0.1:7860
#   scripts/run-local-web.sh --port 7861
#   scripts/run-local-web.sh --print-config
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# 源码目录始终在导入路径上：即使包未安装到环境里，脚本也能直接启动。
export PYTHONPATH="$REPO_ROOT/host${PYTHONPATH:+:$PYTHONPATH}"

if [ -x "$REPO_ROOT/.venv/bin/python" ]; then
  exec "$REPO_ROOT/.venv/bin/python" -m pi_glink.app "$@"
fi

if command -v uv >/dev/null 2>&1; then
  exec uv run python -m pi_glink.app "$@"
fi

exec python3 -m pi_glink.app "$@"
