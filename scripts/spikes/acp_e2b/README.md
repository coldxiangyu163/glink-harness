# ACP E2B 面的 live 探针（GLINK-ACP-04 / M3b）

这四个脚本都是**真实 E2B 沙箱**探针，不是默认检查的一部分；它们只创建并操作自己创建的
沙箱，结束时按 `sandbox_id` 终止（不做模式匹配/批量清理），不打印任何凭据值。
`E2B_API_KEY` 从进程环境或 `--env-file` 读取，绝不写入证据/日志。

| 脚本 | 用途 | 证据 |
|---|---|---|
| `rpc_echo_agent.py` + `frame_integrity_probe.py` | 双向 stdio 帧完整性实测：`background=True, stdin=True` 的后台进程 + `send_stdin`，用仓库 `kernel/framing.py` 分帧；保序、字节一致、半帧/多帧、CRLF、U+2028/U+2029、64 KiB–1 MiB 大帧、`close_stdin` | `docs/evidence/acp-m3-e2b-framing.txt` |
| `template_selfcheck.py` | `glink-acp-worker` dev 模板的真实沙箱自检（用户/家目录/工作目录、node 与各组件版本、每 run HOME 可写） | `docs/evidence/acp-m3-e2b-template.txt` |
| `sandbox_handshake.py` | 生产装配路径（`build_config` → `prepare_acp_runtime` → `E2BAcpSandboxClient` → `build_adapter`）在沙箱内只做 `initialize` + `session/new`（不发 prompt、不消耗模型）；dsh / codex-acp 需凭据，claude-agent-acp（M6 起为后继包名）在全新 HOME 下无需凭据。`--recover-state`（M8）在握手后直接实测状态根回收：真实 SDK 的递归 listing/entry path/size、有界读取与宿主镜像，只记录文件数/字节数/路径样本 | `docs/evidence/acp-m3-e2b-agent-check.txt`、`docs/evidence/acp-m4-claude-e2b-handshake.txt`、`docs/evidence/acp-m6-migration-e2b-handshake.txt`、`docs/evidence/acp-m8-resume-e2b-handshake.txt` |
| `smoke_host.py` | 一次最小真实 run：起真实宿主进程（随机端口、临时数据根、有界超时）→ `POST /api/runs` → 同时订阅 SSE → 终态 → `/report` → `confirm_sandbox_absent` 按 id 对账 | `docs/evidence/acp-m3-e2b-smoke.txt` |
| `resume_smoke.py`（M8） | 跨沙箱续接冒烟：同一 agent 数据根先跑「首轮」（约定随机代号、不写入 report.md），再以 `parentRunId` 跑「追问」（复述代号并追加写入 report.md）；判据含新沙箱（sandbox id 不同）、同 sessionId、`session/resume`、两侧 `acp-agent/recovered` 状态根与内容一致的继承文件、代号回环，以及只按宿主 PID + 持久化 sandbox id 清理 | `docs/evidence/acp-m8-resume-e2b-dsh.txt` |
| `recover_limit_probe.py`（M9） | 状态根回收上限触顶（**无模型**）：每个轴一个真实沙箱，先 `upload_home()` 再用沙箱内命令写合成状态（不走上传通道，所以 8 MiB 以上的单文件也能落进状态根），然后调生产 `recover_state()`；判据 = 整份拒绝（failures 指明对应上限）/ 不留半份（`recovered` 与 `.recovered.staging` 都不存在）/ 沙箱状态源不被改写 / 按 `sandbox_id` 终止；`control` 轴（小文件）证明回收本身可用，`single`/`total`/`count` 三轴任选，默认 `control,single,total` | `docs/evidence/acp-m9-e2b-recover-limits.txt` |
| `recover_refusal_admission.py`（M9） | 回收失败 → 追问准入拒绝：真实宿主 + 真实沙箱跑一次父 run，在其回合内用 SDK 直连同一 sandbox 往状态根写合成超限文件，让真实 finalize 里的回收真的失败；判据 = 末帧 `stateRootRecovered=false`、终态帧 `artifactFailures` 记录上限、宿主无 `recovered`/staging、追问 POST 409 `session_unavailable`、不新建 run 与沙箱、按 id 终止。**消耗一次真实模型调用** | `docs/evidence/acp-m9-e2b-recover-refusal.txt` |

复现命令（在仓库根目录；`--env-file` 指向含 `E2B_API_KEY` 的冻结配置）：

```bash
# 1) 帧完整性（base 模板，不碰任何产品模板）
.venv/bin/python scripts/spikes/acp_e2b/frame_integrity_probe.py --env-file .env

# 2) 模板自检（先用 E2B CLI 构建，见 docker/e2b/Dockerfile.acp-e2b 头部注释）
.venv/bin/python scripts/spikes/acp_e2b/template_selfcheck.py --env-file .env

# 3) 沙箱内握手（三个 agent，均不发 prompt）
.venv/bin/python scripts/spikes/acp_e2b/sandbox_handshake.py --agent dsh --env-file .env
.venv/bin/python scripts/spikes/acp_e2b/sandbox_handshake.py --agent codex-acp --env-file .env
.venv/bin/python scripts/spikes/acp_e2b/sandbox_handshake.py --agent claude-agent-acp --env-file .env

# 3a) 状态根回收自检（M8；握手后直接回收，不发 prompt、不消耗模型）
.venv/bin/python scripts/spikes/acp_e2b/sandbox_handshake.py --agent dsh --recover-state --env-file .env

# 4) 最小真实 run（会消耗一次模型调用）
.venv/bin/python scripts/spikes/acp_e2b/smoke_host.py --env-file .env

# 5) 跨沙箱续接：首轮 + 追问（M8；会消耗两次模型调用、两个沙箱）
.venv/bin/python scripts/spikes/acp_e2b/resume_smoke.py \
  --credential ~/.dsh/.credentials.yaml=.dsh/.credentials.yaml \
  --env-file .env --timeout 300

# 6) 状态根回收上限触顶（M9；真实沙箱、无模型；count 轴约 13–15 分钟）
.venv/bin/python scripts/spikes/acp_e2b/recover_limit_probe.py \
  --axes control,single,total --env-file .env
.venv/bin/python scripts/spikes/acp_e2b/recover_limit_probe.py --axes count --env-file .env

# 7) 回收失败 → 追问准入拒绝（M9；会消耗一次模型调用、一个沙箱）
.venv/bin/python scripts/spikes/acp_e2b/recover_refusal_admission.py --env-file .env \
  --credential ~/.dsh/.credentials.yaml=.dsh/.credentials.yaml
```

模板构建（E2B CLI，非 SDK；`--cpu-count/--memory-mb` 按需）：

```bash
E2B_API_KEY=… npx -y @e2b/cli template create glink-acp-worker \
  --dockerfile docker/e2b/Dockerfile.acp-e2b --cpu-count 2 --memory-mb 4096
```

`glink-acp-worker` 是独立 dev 模板：生产模板 `glink-frontier-e2b` 与其任何 tag、
pi 面模板 `pi-glink-worker` 都不在本任务的触碰范围内。
