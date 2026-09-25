# ACP docker 面的 live 探针（GLINK-ACP-05 / M4 起，M5/M6 扩展）

`handshake.py` 用生产装配路径（`build_config` → `prepare_acp_runtime` →
`docker_env.build_acp_plan` → `AcpExecutionAdapter`）在本地 `pi-glink-acp-worker`
镜像里只做 `initialize` + `session/new`（不发 prompt、不消耗模型），并在结束时按
持久化容器名 force-remove、确认无同名容器残留。不是默认检查的一部分。

```bash
# 握手（claude-agent-acp 是 M6 起的后继包名；opencode 是 M5 新增；其余可复跑作回归）
.venv/bin/python scripts/spikes/acp_docker/handshake.py --command claude-agent-acp
.venv/bin/python scripts/spikes/acp_docker/handshake.py --command 'opencode acp'
.venv/bin/python scripts/spikes/acp_docker/handshake.py --command 'dsh --profile acp'
.venv/bin/python scripts/spikes/acp_docker/handshake.py --command codex-acp

# M5：同时验证每 run HOME 的凭据与容器内嵌套只读挂载（不打印凭据值）
.venv/bin/python scripts/spikes/acp_docker/handshake.py --command 'opencode acp' \
  --credential ~/.local/share/opencode/auth.json=.local/share/opencode/auth.json \
  --session-config model=openai/gpt-5.6-sol-fast
```

前置：`docker build -t pi-glink-acp-worker:latest -f docker/Dockerfile.acp-worker .`
（见 Dockerfile 头注释）。容器内没有 macOS Keychain，宿主 `.env` 也没有
`ANTHROPIC_API_KEY`，因此 docker 面的真实模型 run 属预期阻塞；握手不需要凭据。
