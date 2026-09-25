# ACP inproc 面的 live 探针（GLINK-ACP-05 / M4 起，M5 扩展，M7 增续接冒烟）

这几个脚本操作**本机 inproc 面**，不是默认检查的一部分（`bash scripts/check.sh`
不运行它们）。凭据源只读，值不打印、不落证据；`smoke_host.py` 与
`resume_smoke.py` 会消耗真实模型调用，只在获得授权时使用。

| 脚本 | 用途 | 证据 |
|---|---|---|
| `handshake.py` | 生产装配路径（`build_config` → `prepare_acp_runtime` → `AcpExecutionAdapter`）只做 `initialize` + `session/new`（不发 prompt、不消耗模型）：能力矩阵、`session/new` 选项、每 run HOME 落点、凭据源 sha256 前后一致、agent 子进程命令行 | `docs/evidence/acp-m4-claude-handshake.txt`、`docs/evidence/acp-m6-migration-handshake.txt` |
| `smoke_host.py` | 一次最小真实 run：起真实宿主进程（随机端口、临时数据根、有界超时）→ `POST /api/runs` → 同时订阅 SSE → 终态 → `/report` → 采样宿主后代 PID 并只按 PID 收尾 | `docs/evidence/acp-m4-claude-smoke.txt`、`docs/evidence/acp-m6-migration-smoke.txt` |
| `resume_smoke.py`（M7；M8 实测 dsh/codex-acp） | 续接冒烟：同一 agent 数据根先跑「首轮」（约定随机代号、不写入 report.md），再以 `parentRunId` 跑「追问」（复述代号并写入新 report.md）；判据含同 sessionId、`session_ready.resumed`、连续性代号、状态继承、报告回收与只按 PID 清理。`--parent-run-id` + `--codename` 可只跑追问 | `docs/evidence/acp-m7-resume-claude-agent-acp.txt`、`docs/evidence/acp-m7-resume-opencode.txt`、`docs/evidence/acp-m8-resume-dsh.txt`、`docs/evidence/acp-m8-resume-codex-acp.txt` |

复现命令（仓库根目录；`npx` 钉版调用不全局安装，`--npm-cache` 让每 run 的新
HOME 共享一份 npx 缓存，避免冷下载超过 initialize 的 60s 上限）：

```bash
# 1) 握手（不消耗模型）——claude-agent-acp（M6 起为 claude-code-acp 的后继包）
.venv/bin/python scripts/spikes/acp_inproc/handshake.py \
  --npm-cache /tmp/glink-acp-npx \
  --derive-settings-from ~/.claude/settings.json

# 1a) 可选：驱动宿主 claude CLI（CLAUDE_CODE_EXECUTABLE；M4 的通道，M6 实测仍可用）
.venv/bin/python scripts/spikes/acp_inproc/handshake.py \
  --npm-cache /tmp/glink-acp-npx \
  --derive-settings-from ~/.claude/settings.json \
  --env "CLAUDE_CODE_EXECUTABLE=$(command -v claude)"

# 1b) 握手（不消耗模型）——opencode（M5）；--session-config 可实测 model=… 的选项联动
.venv/bin/python scripts/spikes/acp_inproc/handshake.py \
  --command "npx -y opencode-ai@1.18.32 acp" \
  --npm-cache /tmp/glink-acp-npx \
  --credential ~/.local/share/opencode/auth.json=.local/share/opencode/auth.json \
  --session-config "model=openai/gpt-5.6-sol-fast" \
  --watch opencode

# 2) 最小真实 run（会消耗一次模型调用；运行期 settings 只取 env 块，0600）
.venv/bin/python scripts/spikes/acp_inproc/smoke_host.py \
  --npm-cache /tmp/glink-acp-npx \
  --derive-settings-from ~/.claude/settings.json \
  --permission-allow allow_once,read,search,fetch,edit \
  --timeout 300

# 2b) 同形冒烟——opencode（M5；--session-config 选具体模型）
.venv/bin/python scripts/spikes/acp_inproc/smoke_host.py \
  --agent "npx -y opencode-ai@1.18.32 acp" \
  --npm-cache /tmp/glink-acp-npx \
  --credential ~/.local/share/opencode/auth.json=.local/share/opencode/auth.json \
  --session-config "model=openai/gpt-6-astra-fast" \
  --timeout 300

# 3) 真实续接：首轮 + 追问（会消耗两次模型调用）
.venv/bin/python scripts/spikes/acp_inproc/resume_smoke.py \
  --npm-cache /tmp/glink-acp-npx \
  --derive-settings-from ~/.claude/settings.json \
  --permission-allow allow_once,read,search,fetch,edit \
  --timeout 300

# 3a) 续接冒烟——dsh（M8 实测：真实 `session/resume`，同 sessionId + 代号回环）
.venv/bin/python scripts/spikes/acp_inproc/resume_smoke.py \
  --label dsh \
  --agent "npx -y @deepseek-ai/dsh@0.1.5-rc.3 --profile acp" \
  --credential ~/.dsh/.credentials.yaml=.dsh/.credentials.yaml \
  --npm-cache "$HOME/.npm" \
  --timeout 300

# 3b) 续接冒烟——codex-acp（M8 实测：真实 `session/resume`；本钉版 mode 取值为
#     read-only/agent/agent-full-access，默认 agent，不用旧版的 mode=auto）
.venv/bin/python scripts/spikes/acp_inproc/resume_smoke.py \
  --label codex-acp \
  --agent "npx -y @agentclientprotocol/codex-acp@1.13.1" \
  --credential ~/.codex/auth.json=.codex/auth.json \
  --npm-cache "$HOME/.npm" \
  --timeout 300

# 3c) 续接冒烟——opencode（M7 实测：OAuth 复制件过期且 refresh 被 401 拒绝，
#     改用无凭据的 OpenCode Zen 免费模型，即默认 opencode/big-pickle，不传 credential）
.venv/bin/python scripts/spikes/acp_inproc/resume_smoke.py \
  --agent "npx -y opencode-ai@1.18.32 acp" \
  --npm-cache /tmp/glink-acp-npx \
  --timeout 300
```

说明：

* `--derive-settings-from` 只把宿主 `claude` settings 的 `env` 块抽成临时数据根里
  的 `settings.json`（0600），因为完整设置文件里的 hooks/plugins/statusLine 会在
  run 内执行并写操作员的真实状态；真实文件始终只读（脚本核对 sha256）。
* claude-agent-acp 0.81.2 自带原生 claude CLI（2.1.280）：同一份运行期 settings 下
  它的 `auth status` 报 `loggedIn=true`（空 HOME 报 false），所以 M4 的
  `CLAUDE_CODE_EXECUTABLE` 不再是必选项（继续支持）；M6 用原生通道与
  `CLAUDE_CODE_EXECUTABLE` 通道各跑了一次最小真实 run。
* opencode 不需要派生配置：凭据就是 `--credential` 复制进 run HOME 的
  `auth.json`；`--watch opencode` 只影响子进程命令行采样关键字。
* `--credential` 走 `PI_GLINK_ACP_CREDENTIAL_FILES`（只读复制进每 run HOME，
  父目录 0700、文件 0600，注入值并入该 run 的 redactor）。
* `--session-config` 走 `PI_GLINK_ACP_SESSION_CONFIG`（`;` 分隔的 `id=value`，
  应用在 `session/new` 之后；agent 未声明该选项或取值时 run 失败闭环）。
* `--permission-allow` 是显式白名单门控；缺省留空保持 fail-closed 原行为。
