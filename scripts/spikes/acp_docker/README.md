# ACP docker 面的 live 探针（GLINK-ACP-05 / M4）

`handshake.py` 用生产装配路径（`build_config` → `prepare_acp_runtime` →
`docker_env.build_acp_plan` → `AcpExecutionAdapter`）在本地 `pi-glink-acp-worker`
镜像里只做 `initialize` + `session/new`（不发 prompt、不消耗模型），并在结束时按
持久化容器名 force-remove、确认无同名容器残留。不是默认检查的一部分。

```bash
# 握手（claude-code-acp 是 M4 新增；dsh / codex-acp 可复跑作回归）
.venv/bin/python scripts/spikes/acp_docker/handshake.py --command claude-code-acp
.venv/bin/python scripts/spikes/acp_docker/handshake.py --command 'dsh --profile acp'
.venv/bin/python scripts/spikes/acp_docker/handshake.py --command codex-acp
```

前置：`docker build -t pi-glink-acp-worker:latest -f docker/Dockerfile.acp-worker .`
（见 Dockerfile 头注释）。容器内没有 macOS Keychain，宿主 `.env` 也没有
`ANTHROPIC_API_KEY`，因此 docker 面的真实模型 run 属预期阻塞；握手不需要凭据。
