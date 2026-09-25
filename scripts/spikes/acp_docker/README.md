# ACP docker 面的 live 探针（GLINK-ACP-05 / M4 起，M5/M6 扩展，M9 增续接冒烟）

`handshake.py` 用生产装配路径（`build_config` → `prepare_acp_runtime` →
`docker_env.build_acp_plan` → `AcpExecutionAdapter`）在本地 `pi-glink-acp-worker`
镜像里只做 `initialize` + `session/new`（不发 prompt、不消耗模型），并在结束时按
持久化容器名 force-remove、确认无同名容器残留。不是默认检查的一部分。

`resume_smoke.py`（GLINK-ACP-15 / M9）在同一镜像里跑真实「首轮 + 追问」：
每 run 一个容器，追问经 `parentRunId` 走 `session/resume`；判据含新容器（按 runId
派生的精确容器名）、同 `sessionId`、`resumeMethod`、代号回环（首轮代号只留在会话里）、
两轮 report 200、父/子宿主可见 HOME 的继承文件（含带父 sessionId 的会话文件）、容器
运行期 `docker inspect` 的挂载快照（状态根 rw → `/home/acp`、凭据文件 :ro）与
`confirm_absent` 精确名字对账。它**会消耗两次真实模型调用**。Colima 只共享 home 树，
数据根默认建在 `$HOME` 下（`--data-root` 指到别处时只告警）。

```bash
# M9：docker 面真实续接（首轮 + 追问；消耗两次模型调用；dsh 凭据默认已选好）
.venv/bin/python scripts/spikes/acp_docker/resume_smoke.py --label docker-dsh

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
