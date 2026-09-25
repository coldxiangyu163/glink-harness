# ACP E2B 面的 live 探针（GLINK-ACP-04 / M3b）

这四个脚本都是**真实 E2B 沙箱**探针，不是默认检查的一部分；它们只创建并操作自己创建的
沙箱，结束时按 `sandbox_id` 终止（不做模式匹配/批量清理），不打印任何凭据值。
`E2B_API_KEY` 从进程环境或 `--env-file` 读取，绝不写入证据/日志。

| 脚本 | 用途 | 证据 |
|---|---|---|
| `rpc_echo_agent.py` + `frame_integrity_probe.py` | 双向 stdio 帧完整性实测：`background=True, stdin=True` 的后台进程 + `send_stdin`，用仓库 `kernel/framing.py` 分帧；保序、字节一致、半帧/多帧、CRLF、U+2028/U+2029、64 KiB–1 MiB 大帧、`close_stdin` | `docs/evidence/acp-m3-e2b-framing.txt` |
| `template_selfcheck.py` | `glink-acp-worker` dev 模板的真实沙箱自检（用户/家目录/工作目录、node 与两个 agent 版本、每 run HOME 可写） | `docs/evidence/acp-m3-e2b-template.txt` |
| `sandbox_handshake.py` | 生产装配路径（`build_config` → `prepare_acp_runtime` → `E2BAcpSandboxClient` → `build_adapter`）在沙箱内只做 `initialize` + `session/new`（不发 prompt、不消耗模型） | `docs/evidence/acp-m3-e2b-agent-check.txt` |
| `smoke_host.py` | 一次最小真实 run：起真实宿主进程（随机端口、临时数据根、有界超时）→ `POST /api/runs` → 同时订阅 SSE → 终态 → `/report` → `confirm_sandbox_absent` 按 id 对账 | `docs/evidence/acp-m3-e2b-smoke.txt` |

复现命令（在仓库根目录；`--env-file` 指向含 `E2B_API_KEY` 的冻结配置）：

```bash
# 1) 帧完整性（base 模板，不碰任何产品模板）
.venv/bin/python scripts/spikes/acp_e2b/frame_integrity_probe.py --env-file .env

# 2) 模板自检（先用 E2B CLI 构建，见 docker/e2b/Dockerfile.acp-e2b 头部注释）
.venv/bin/python scripts/spikes/acp_e2b/template_selfcheck.py --env-file .env

# 3) 沙箱内握手（两个 agent，均不发 prompt）
.venv/bin/python scripts/spikes/acp_e2b/sandbox_handshake.py --agent dsh --env-file .env
.venv/bin/python scripts/spikes/acp_e2b/sandbox_handshake.py --agent codex-acp --env-file .env

# 4) 最小真实 run（会消耗一次模型调用）
.venv/bin/python scripts/spikes/acp_e2b/smoke_host.py --env-file .env
```

模板构建（E2B CLI，非 SDK；`--cpu-count/--memory-mb` 按需）：

```bash
E2B_API_KEY=… npx -y @e2b/cli template create glink-acp-worker \
  --dockerfile docker/e2b/Dockerfile.acp-e2b --cpu-count 2 --memory-mb 4096
```

`glink-acp-worker` 是独立 dev 模板：生产模板 `glink-frontier-e2b` 与其任何 tag、
pi 面模板 `pi-glink-worker` 都不在本任务的触碰范围内。
