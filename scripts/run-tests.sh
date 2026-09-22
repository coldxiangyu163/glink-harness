#!/usr/bin/env bash
# 跑宿主与内核回归 / run host and kernel regressions.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

export PYTHONPATH="$REPO_ROOT/host${PYTHONPATH:+:$PYTHONPATH}"

if [ -x "$REPO_ROOT/.venv/bin/python" ]; then
  exec "$REPO_ROOT/.venv/bin/python" -m pytest "$@"
fi

if command -v uv >/dev/null 2>&1; then
  exec uv run python -m pytest "$@"
fi

exec python3 -m pytest "$@"
