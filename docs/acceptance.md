# 验收记录

日期：2026-09-22 · 仓库：`~/AI/git/pi-glink` · 基线 pi：0.86.1（升级演练另用 0.87.0）

本记录汇总本次目标（task-1 ~ task-9）的代码范围、实际命令、真实退出状态、证据文件与
未覆盖项。所有结论都可在本机重跑；证据文件在 `docs/evidence/`。

## 1. 代码范围

| 提交 | 范围 |
|---|---|
| `7d0411a` | task-1 仓库骨架、宿主服务、事件总线、前端基线 |
| `7d0ae2e` | task-2 pi 接入层（RPC 客户端、事件投影、运行期配置渲染） |
| `a3628ec` | task-3 本机执行面端到端（run 编排、run API、冒烟脚本） |
| `4ff3c9e` | task-4 前端事件层重写（run-stream / run-view / workspace） |
| `530bc53` | task-5 搜索接入 pi-web-access |
| `f29c3b3` `8a1bd03` | task-6 Docker 执行面与验收证据 |
| `603c1c4` | task-7 E2B 执行面与验收证据 |
| `e093976` | task-8 架构文档与 pi 边界契约 |

目录边界与各目录职责见 `docs/architecture.md` §7。

## 2. 实际命令、prompt 与退出状态

### 回归

| 命令 | 结果 |
|---|---|
| `scripts/run-tests.sh` | **100 项通过**，exit 0（`test_host_app` 21 · `test_kernel_agent_env` 11 · `test_kernel_docker_env` 7 · `test_kernel_e2b_env` 10 · `test_kernel_rpc` 11 · `test_redaction` 17 · `test_runner` 8 · `test_runs_api` 9 · `test_store` 6） |
| `node --test tests/web/*.test.mjs` | 15 项通过，exit 0 |
| `node tests/browser/run-smoke.mjs --base-url http://127.0.0.1:7861` | 通过（真实 run；思考折叠 / 工具卡片 / 报告预览 + 截图；新增 `toolsRenderingTextTwice=0` 断言），exit 0 |

### 三种执行面的真实 run（完整命令与 prompt）

**本机（inproc）**

```bash
scripts/run-local-web.sh                      # 默认 127.0.0.1:7860；本机 .env 覆盖为 7861
.venv/bin/python scripts/smoke-local-run.py --base-url http://127.0.0.1:7861 \
  --prompt "请阅读 docs/pi-integration-notes.md 与 host/pi_glink/kernel/rpc.py，然后写出 report.md：说明 pi 内核接入的关键约束与三个最容易踩的坑。用中文。"
```
→ finished，45.1s，165 事件，报告 9648B（证据 `local-m1-smoke.txt`）

**Docker**

```bash
docker build -t pi-glink-worker:latest -f docker/Dockerfile.worker .        # 镜像 1.09GB
LOCAL_WEB_EXECUTOR=docker LOCAL_WEB_PORT=7862 .venv/bin/python -m pi_glink.app
.venv/bin/python scripts/smoke-local-run.py --base-url http://127.0.0.1:7862 \
  --prompt "在本工作区创建 notes.md 写入三行关于容器化执行的事实，然后写出 report.md 总结这次运行。用中文。"
```
→ finished，11.5s，136 事件，报告 645B（报告内为容器路径 `/workspace/…`）

**E2B**

```bash
# 模板构建（本机网络需代理；Node CLI 不认 socks5，故用 Python SDK）
E2B_API_KEY=… HTTPS_PROXY=socks5h://127.0.0.1:1080 \
  .venv/bin/python scripts/build-e2b-template.py --logs        # 模板 47s，buildId 2bec2f1d…

# 宿主：E2B_API_KEY 可只放 .env（修复后已实测）；代理是网络配置，不是凭据
HTTPS_PROXY=socks5h://127.0.0.1:1080 LOCAL_WEB_EXECUTOR=e2b LOCAL_WEB_PORT=7863 \
  .venv/bin/python -m pi_glink.app

.venv/bin/python scripts/smoke-local-run.py --base-url http://127.0.0.1:7863 \
  --prompt "先检索 pi-web-access 这个 npm 包的用途（用 web_search），然后在本工作区写出 report.md，给出你实际打开过的来源链接。用中文。"
```
→ finished，68.4s，203 事件（13 种帧类型），报告 7115B / 13 URL（证据 `e2b-run.txt`）

### 边界核验

| 命令 | 结果 |
|---|---|
| `docs/pi-boundary.md` §4 的 6 条交叉核验 | 全部通过（无源码副本 / 只用官方协议 / 无内部引用 / 无密钥入库 / 凭据 600 / 前端无依赖树） |

## 3. 证据文件

| 文件 | 内容 |
|---|---|
| `docs/evidence/local-m1-smoke.txt` | 本机执行面真实 run：45.1s、165 事件、80 段 thinking、session/workspace 对应关系 |
| `docs/evidence/browser-run.png` `browser-run.json` | 浏览器真实 run 的截图与 DOM 统计 |
| `docs/evidence/local-web-search.txt` | pi-web-access 检索 run：web_search ×1 + fetch_content ×3，报告 10 URL |
| `docs/evidence/docker-run.txt` | 镜像体积、构建命令、容器内 run、`/workspace/` 路径证据 |
| `docs/evidence/e2b-run.txt` | 沙箱内 node/pi 版本、凭据注入方式、203 事件 run、三处差异 |
| `docs/evidence/upgrade-drill.txt` | pi 0.87.0 升级演练与隔离安装做法 |
| `docs/pi-integration-notes.md` | 每条实测事实（插值范围、thinkingFormat、pi-web-access 配置目录、RPC 帧语义） |

## 4. 对照成功标准

| 标准 | 状态 | 证据 |
|---|---|---|
| 1) 本机一条命令启动 → 实时 thinking/工具/文本 → 报告落盘 | ✅ | local-m1-smoke.txt、browser-run.png |
| 2) 模型与 E2B 配置与 FrontierAgent 同源，密钥不进前端/不落仓库 | ✅ | `OPENAI_*`/`E2B_*` 同名语义；`models.json` 不含 apiKey，凭据写运行期 `auth.json`（0600，仓库外）；交叉核验 ④ |
| 3) 搜索由 pi-web-access 完成，报告含可核验 URL，无 Serper/Jina | ✅ | local-web-search.txt（web_search + 10 URL；仓库无 Serper/Jina 依赖） |
| 4) Docker 与 E2B 各跑通一次真实 run（含 Node + pi 与凭据注入） | ✅ | docker-run.txt（e23d3ae9b640 11.5s/136 事件；返工后 824fa1975211 会话路径映射脱敏、4c589e0989fa env=0）、e2b-run.txt（99c3367c2a15 33.1s/148 事件，node v24.21.0） |
| 5) 前端无 Glink 特有依赖、不解析终端文本、断线/迟到事件不破坏状态 | ✅ | run-stream.test.mjs（seq 去重/缺口/终态）、源码无终端解析、终态后主动关闭流 |
| 6) architecture.md + pi-boundary.md + 可重复 smoke 命令 | ✅ | 两份文档 + `scripts/smoke-local-run.py`、`tests/browser/run-smoke.mjs` |
| 7) 验收记录含范围/命令/退出状态/证据/未覆盖项 | ✅ | 本文档 |

## 5. 未覆盖项与已知限制

1. **E2B 面事件实时性**：原文「E2B SDK 2.x 的 stdout 回调实测不触发，事件在 run 结束后
   一次性回放」是用法误判；GLINK-E2B-LIVE-01 已更正（流式接口是 `commands.run(background=True)`
   返回的 `CommandHandle`）：宿主迭代 handle 实时投递，断流时按 `events.jsonl` 补齐。宿主侧
   离线验证完成，真实 E2B live 实时性尚未验收。见 `e2b-run.txt` 末尾注记与
   architecture.md §5.2。
2. **E2B 面会话不持久**：会话文件留在沙箱内，销毁即失，`session_id` 为空。
3. **版本 pin 差异**：inproc 面已在 pi 0.87.0 验证；Docker 镜像与 E2B 模板仍 pin 0.86.1
   （已实测），按 `docs/pi-boundary.md` §3 重建即可升级，本次未重建。
4. **串行执行**：三种执行面都只允许同时一个 run（`409 run_busy`）。
5. **报告无强制校验**：是否产出 `report.md` 取决于 pi 遵循工作约定（需要交付文档时才写报告，
   直接回答不写报告）；缺失记为 `no_report`，宿主不伪造产物。
6. **思考内容依赖上游**：thinking 事件仅在模型返回 reasoning 时出现（同一任务在不同
   prompt 下可能没有思考块），前端能正常接受"这一轮没有思考"。
7. **未做**：多用户/权限、成本与配额治理、移动端适配验证、E2B 模板生产硬化、并发调度。
8. **代理前提**：本机访问 E2B 需要代理（`socks5h://127.0.0.1:1080`），已记入 `e2b-run.txt`。

## 6. 边界与环境的如实说明

1. **FrontierAgent 仓库未被本任务改动**：本任务对它只有读操作（读 `.env` 取 `OPENAI_*` 值、
   复制 `deploy/local_web/static/` 作为前端基线）。检查时它工作区里存在未提交改动
   （`config/tiers/*`、`deploy/local_web/*` 等），那是使用者自己的在制品，与本任务无关；
   本仓库也没有引用它的任何模块。
2. **全局 pi 的时间线**：task-2 的实测基线是 `pi 0.86.1`；任务进行中（当日 10:03）
   全局安装被外部升级到 `0.87.0`（非本任务所为）。这反而让 task-9 的升级演练成为
   真实场景：宿主、接入层、投影与前端在更高版本下无需改动即通过
   （证据 `docs/evidence/upgrade-drill.txt`）。
3. **未触发需要暂停的条件**：本次创建的是**新** E2B 模板（`pi-glink-worker`），
   没有重建或覆盖 FrontierAgent 已有的 `glink-frontier-e2b` 模板；沙箱内的
   `web-search.json` 是临时的（随沙箱销毁），不存在凭据长期驻留。

## 7. 审计后的返工与复验

首次完成请求被独立审计驳回并列出缺口，以下是逐条返工与复验证据。

| 审计缺口 | 返工 | 复验 |
|---|---|---|
| E2B `.env` 凭据链路不成立（`Sandbox.create()` 未收到 `api_key`） | `config.py` 新增 `e2b_api_key`（`repr=False`），`e2b_env.start()` 经 `**opts` 传给 `Sandbox.create(api_key=…)` | 单测 `test_api_key_from_env_reaches_sandbox_create`；**真实 run**：启动命令不含 `E2B_API_KEY`、仅 `.env` 提供 → `/api/health` 报 `configured.e2b: true` 且 run 通过 |
| E2B 依赖不可复现（`pyproject.toml` 无依赖声明） | 增 `e2b` extra：`e2b>=2.0` + `httpx[socks]>=0.27`；README 增「依赖与版本」表 | `pyproject.toml` `[project.optional-dependencies]`；安装命令 `uv pip install -e ".[e2b]"` |
| 取消时发 `run_finished` 而非 `run_cancelled` | `runner.py` 终态事件按 status 映射 | 回归：取消 run 的事件日志末帧为 `run_cancelled` |
| E2B 非零退出但存在报告时被记为 finished | `runner._run_in_sandbox`：非零退出即失败 | `test_nonzero_exit_fails_the_run_even_with_a_report` |
| smoke 默认 prompt 硬编码本机绝对路径 | 改为与仓库无关的描述 | `scripts/smoke-local-run.py` |
| 验收记录数字不准确（声称 73） | 用 `--collect-only` 实测重算：**78 项**（8 个测试文件） | 本文件 §2 |
| Docker / E2B 完整命令与 prompt 未记录 | 本文件 §2 补齐三面完整命令与 prompt；两份证据文件同步补充 | `docs/evidence/docker-run.txt`、`e2b-run.txt` |
| README 与 `harnesses/pi/README.md` 仍称「task-1 骨架阶段」 | 更新为交付状态与内容清单 | README、`harnesses/pi/README.md` |
| `styles.css` 保留大量 Glink 特有规则 | 保守裁剪：删除「选择器中所有类名均已无引用」的规则（565 条 / 254 个类），93 KB → 30 KB | 裁剪后重跑浏览器回归通过（思考折叠 / 工具卡片 / 报告预览 + 截图） |

返工后的全量重跑：Python **78 项通过**、Node **11 项通过**、浏览器回归通过。

### 第二轮审计返工（前端与文档）

| 审计缺口 | 返工 | 复验 |
|---|---|---|
| `styles.css` 仍含 scenario / tier / task-board / upload / review 规则 | 第三轮裁剪：含 `:has()`/`:not()` 的选择器同样逐类判定，并清理注释里的过时引用 | 实测：`scenario` / `tier` / `task-board` / `upload` / `user-row` / `artifact` / `library` / 独立 `review-*` 选择器均为 0（`review` 的字面命中仅来自 `preview-*` 子串）；93 KB → 21.6 KB；浏览器回归通过 |
| 历史 run 重放被终态误判截断 | `workspace.mjs` 改按**终态帧类型**关闭 SSE（新增 `TERMINAL_EVENT_TYPES`），不再看模型状态 | 浏览器断言：历史 run 重放 `items=12, tools=3, status=finished` |
| 报告抽屉缺 `preview-open`，第三列 0px | `openReport` 加类、`closeReport`/`closeCurrent` 移除 | 浏览器断言：`previewOpen=true, previewWidth=563px, scrollWidth==clientWidth`（无横向溢出） |
| 升级文档把 `PI_GLINK_PI_BIN` 放在 smoke 命令上 | 改为放在宿主启动命令上，并说明宿主与 smoke 客户端的区别 | `docs/pi-boundary.md` §3；宿主启动横幅打印实际 pi 路径 |
| README 无效命令、不存在的顶层 `kernel/`、Node 版本未固定 | 修正为 `uv pip install -e ".[dev]"`、`host/pi_glink/kernel/`、Node **v24.16.0** + nvm 安装方式 | README |
| `.env-only` E2B run 无 runId / 证据 / 退出码 | 记录 runId `3fc5c4734713`（26.4s / 143 事件 / sandbox `ipy0uzzdsg4e` / finished / smoke exit 0）与结构化日志路径 `~/.pi-glink/runs/3fc5c4734713/events.jsonl` | `docs/evidence/e2b-run.txt`、`docker-run.txt` |

### 第三次审计返工（凭据边界）

审计指出：模型 key 注入 pi 进程环境（工具可继承）、事件投影与报告下载未脱敏、用虚拟密钥构造的
`tool_execution_end` 帧能原样穿过投影。逐条返工如下。

| 审计缺口 | 返工 | 复验 |
|---|---|---|
| 事件投影与事件日志未脱敏 | 新增 `host/pi_glink/redaction.py`（值检测 + 形状检测 + 递归载荷脱敏）；`project_event(frame, redactor)` 投影即脱敏；`runner._emit` 在发布前再兜底一次 | `tests/test_redaction.py` 13 项，含审计复现用例 `test_audit_reproduction_tool_output_is_redacted`；`events.jsonl` 与 SSE 载荷均断言不含 key |
| 报告下载无脱敏 | `app._send_report` 服务前脱敏并回 `X-Redacted`；`runner._collect_report` 在 run 收尾就地脱敏报告文件，meta 记 `redacted=true` | `test_report_download_is_redacted_and_flagged`、`test_runner_redacts_the_report_in_place_and_marks_the_run` |
| pi 自写的会话文件可能含工具输出 | `runner._sanitize_session_file` 收尾脱敏（保持合法 JSONL） | `test_session_file_is_redacted_on_the_way_out` |
| 子进程继承宿主整份环境 | `agent_env._PASSTHROUGH_ENV` 白名单：只放行运行必需项，再显式注入 `PI_*` 与模型凭据 | `test_minimal_child_environment_does_not_inherit_host_credentials`（ANTHROPIC_API_KEY / AWS_SECRET_ACCESS_KEY 不进入子进程） |
| 仓库缺少防泄漏检查 | 新增 `scripts/check-no-secrets.py`：真实值命中判失败，文档占位符单独计数（`--show-placeholders`） | 实测：真实命中 **0 处** · 占位符 2 处（pi README 中的 `sk-ant-...`） |

**残留风险（§8 修复后的状态；已被 §9 取代）**：该轮之后 key 仍在进程环境里。§9 改为把 key 写入
pi 官方凭据存储 `auth.json`（0600，运行期目录），进程环境不再包含它——三面真实 run 由内核自己
跑 `env | grep -c OPENAI_API_KEY` 均得到 0。

### 第四次审计返工（key 不进进程环境）

审计第一条指控是「`OPENAI_API_KEY` 被注入 pi 进程环境，bash 工具会继承」。输出脱敏只能保证
“不回显”，不能消除“工具可读”；本次改为让 key **根本不进进程环境**。

| 审计缺口 | 返工 | 复验 |
|---|---|---|
| 模型 key 经环境变量进入 pi 进程 | 弃用 `$OPENAI_API_KEY` 插值：`models.json` 不写 `apiKey`；key 写入运行期 `auth.json`（pi 官方凭据存储，0600，仓库外）；`build_pi_env` 不再注入该变量；docker 面去掉 `-e OPENAI_API_KEY`；e2b 面改为上传 `auth.json` | 三面真实 run：inproc `a6f67fde8e51`、docker `1e367674ed08`、e2b `046a51ac09d0` 全部 finished；inproc 与 e2b 让内核自己跑 `env \| grep -c OPENAI_API_KEY`，报告首行为 **0** |
| 子进程环境可能带入宿主其他凭据 | `PASSTHROUGH_ENV` 白名单（PATH/HOME/代理/证书等）+ 显式 `PI_*` | `test_minimal_child_environment_does_not_inherit_host_credentials` |

证据：`docs/evidence/credential-boundary.txt`（含 auth.json 结构/权限、三面 runId 与 `env` 检查输出）。
残留风险已在 architecture.md §6.2 如实记录：工具理论上仍可 `read` 打开 `auth.json` 并回显，
该场景由 §6.1 的六层出站脱敏覆盖。

**最终收口（同轮）**：除上表两项外，本轮还补齐了审计可能继续追踪的四处覆盖缺口，逐项均有回归：

| 缺口 | 修复 | 回归 |
|---|---|---|
| 只清点 `report.md`，而 `write` 工具可把回显写进任意产物 | `runner._sanitize_workspace` 收尾扫描 workspace 全部文本文件（≤2 MiB、跳过二进制/符号链接），命中即就地脱敏并置 `redacted=true` | `test_workspace_artifacts_are_swept_for_credentials` |
| `meta.json` 的 `error` 字段可能带内核错误里的回显 | 落盘前过 redactor | `test_run_error_in_metadata_is_redacted` |
| 投影被调用时若忘记传 redactor 会原样放行（正是审计复现的最窄形态） | `project_event` 改为 fail-closed：未传时使用模块级形状检测 redactor | `test_projection_without_an_explicit_redactor_is_fail_closed` |
| 用户可把凭据粘进 prompt，随 `meta.json` 落盘 | 落盘/展示副本先脱敏，原始指令经 `_Execution.kernel_prompt` 交给内核（RPC 与 E2B 面一致） | `test_pasted_credential_in_the_prompt_is_not_stored_in_metadata` |

最终代码下的三面复验（均由内核自己执行 `env | grep -c OPENAI_API_KEY` 并写入报告首行）：
inproc `a6f67fde8e51` / docker `03be2909302e` / e2b `db6a9e83c0b7` 均为 **0**；
另一常规 inproc run `9ad186ea1928` 的 `notes.md` 与 `report.md` 亦不含真实 key。
回归：Python 95 项 + Node 11 项全绿；`scripts/check-no-secrets.py` 真实命中 0 / 占位符 2。



### 第五次审计返工（会话文件路径 / 文档一致 / Node 固定）

| 审计缺口 | 返工 | 复验 |
|---|---|---|
| Docker `sessionFile` 是容器路径 `/sessions/…`，宿主按原样读取不存在 → 收尾脱敏静默跳过，工具回显的密钥可能永久留在会话文件 | 新增 `kernel/docker_env.py::host_session_path / translate_container_path`（纯词法映射，拒绝相对路径、前缀伪装与 `..` 穿越）；`runner._publish_session_ready` 在落库/发事件前映射为宿主路径；`_sanitize_session_file` 读不到文件时显式告警而非静默返回 | 回归：`test_container_session_paths_map_back_to_the_host_mount`、`test_docker_session_file_is_mapped_back_to_the_host_and_sanitized`；真实 Docker run `824fa1975211`（假密钥写入 notes.md）：`meta.redacted=true`，notes.md / events.jsonl / 宿主会话 JSONL 三处均无假密钥原文且含 `[REDACTED]`；宿主日志出现 `session file contained credentials and was redacted` |
| 文档仍写过时的凭据流（env 注入 / `-e OPENAI_API_KEY` / `Sandbox.create(envs=…)`） | 逐处订正 README、architecture、pi-boundary、验收表与两份证据文件为 `auth.json` 交付：Docker 随 `/config` 挂载、E2B 随运行期配置上传 | 全仓库 grep：`envs=`、`-e OPENAI_API_KEY`、`按变量名转发`、`apiKey: "$OPENAI_API_KEY"` 均无残留（历史审计表除外） |
| Node 版本未固定（浮动 `node:24-bookworm-slim`；README 本机 v24.16.0 与 E2B 证据 v24.21.0 不一致） | 两个 Dockerfile 固定为 `FROM node:24.21.0-bookworm-slim`；README 明确宿主开发 v24.16.0、镜像/沙箱 v24.21.0 | 重建镜像：`docker run --rm pi-glink-worker:latest node -v` → v24.21.0、`pi --version` → 0.86.1；新镜像真实 run `4c589e0989fa`：报告首行 env 计数 = 0；E2B 沙箱 `99c3367c2a15`：node v24.21.0 / env 计数 0。E2B 模板未重建（覆盖既有 tag 需先确认）；现有模板实测 Node 已与固定版本一致 |

回归：Python 97 项 + Node 11 项全绿；`scripts/check-no-secrets.py` 真实命中 0。

### 第六次审计返工（工具流累计快照 / 工具终态 / 文档一致）

| 审计缺口 | 返工 | 复验 |
|---|---|---|
| `run-stream.mjs::updateTool` 把 `tool_execution_update.partialResult` 当增量拼接，重复展示前缀（真实 run `ebf75f590601`：`0 → 53 → 495`，前缀包含） | 改为替换语义：`const output = typeof payload.output === 'string' ? payload.output : item.output`；模块头与函数内注释注明 pi `docs/rpc.md` 的累计快照原文 | 新增 Node 回归 `cumulative tool snapshots replace the display instead of duplicating prefixes`（复刻 0→53→495 形状：替换 = 495 字符，拼接会得 548 字符且前缀重复）；原 `tool cards track streaming output and final status` 改为累计快照形状；证据 `docs/evidence/tool-stream-semantics.txt` |
| `finishRun()` 把失败/取消时仍运行的工具统一标成 `done` | 新增 `INTERRUPTED_TOOL_STATUS`：finished→done、failed→interrupted、cancelled→cancelled；已拿到自身终态帧的工具保持原状态；`run-view.mjs` 新增 `TOOL_STATE_LABELS`（执行中/完成/失败/已取消/已中断）、`styles.css` 为 interrupted/cancelled 补配色 | 新增 Node 回归 `a failed or cancelled run never leaves a running tool looking done`（两种终态 + 已终态工具不受影响的对照） |
| 文档陈旧：`harnesses/pi/README.md` 称配置目录直接指向仓库、凭据「只经环境变量传入」；`README.md` 目录表同样表述 | `harnesses/pi/README.md` 改为「本目录是模板，渲染到 `<data_root>/pi-agent/`」，凭据写明经 `auth.json`（0600，仓库外）交付；`models.json/auth.json/web-search.json` 的归属一并订正；README 目录表同步 | 全仓库 grep：`指向这里`、`只经环境变量传入` 无残留 |
| `docs/pi-integration-notes.md` §6 仍列 Docker/E2B 注入为「待补测」 | 改写为实测结论表（`PI_CODING_AGENT_DIR` / 会话目录 / 工作区 / 凭据载体 / `env` 计数），并新增 §7 工具流语义（含 `docs/rpc.md` 原文与 `ebf75f590601` 帧长） | 与 `docs/evidence/docker-run.txt`、`e2b-run.txt`、`credential-boundary.txt` 交叉核对一致 |

回归：Python 97 项 + Node 14 项全绿；`scripts/check-no-secrets.py` 真实命中 0。

### 第七次审计返工（前端报告层剥离 / 工具终帧去重 / E2B 取消 / 文档订正）

| 审计缺口 | 返工 | 复验 |
|---|---|---|
| 前端仍是 Glink 报告渲染层：`markdown.mjs` 852 行与 FrontierAgent 逐字相同（chart/flow、artifact、引用、事实标签）+ 相关 CSS | 重写为项目自有最小渲染器（标题/段落/强调/链接/图片/代码围栏/列表/引用/分隔线/管道表格）；删除 `i18n.mjs`；`ui.mjs` 130→36 行；`styles.css` 957→454 行（移除 `.citation`/`.qualifier-badge`/`.report-chart*`/`.report-source-entry`/`source-flash` 等）；```chart 围栏按普通代码块渲染 | `docs/evidence/frontend-boundary.txt`：行数与 FrontierAgent 比对（三文件均非逐字相同）、禁止 token 扫描 0 命中；新增 Node 回归 `the front-end carries no Glink-specific report layer`；浏览器回归 `toolsRenderingTextTwice=0` |
| 工具终帧重复渲染：真实 fixture 中 `tool_updated.output` 与 `tool_finished.result` 同为 495 字符，卡片同时渲染 output 与 result | `run-stream.mjs::finishTool` 在 `result === item.output` 时清空 output（终帧权威）；两者不同时仍保留两份；fixture 测试断言 `output=''`、渲染总长 495、前缀仅出现一次 | `docs/evidence/tool-stream-semantics.txt`（新增终帧段落）；Node 回归 `the audited run ebf75f590601 replays with replacement semantics (real fixture)`；浏览器 DOM 断言 `toolsRenderingTextTwice=0`（证据 `browser-run.json`） |
| E2B 取消语义错误：分支从不检查 `cancel_requested`；停止时关沙箱异常落 `run_failed`；正常返回仍 `run_finished` | `runner.py`：`_run_in_sandbox` 4 个检查点（创建前/start 后/upload 后/run_prompt 后）；`_execute` e2b 分支返回后复核 `cancel_requested`；`except` 先判取消并记为 `run cancelled run=… (…)`，不落 failed | 新增 3 个 E2B 取消测试（Python 现 100 项，含参数化异常/正常返回两条路径 + 沙箱创建期间停止）；真实 E2B 取消演练 run `c62e014c772d`：stop → `status=cancelled`、末尾 `run_cancelled`，宿主日志 `run cancelled run=c62e014c772d (TimeoutException)`，证据 `docs/evidence/e2b-cancel.txt` |
| 文档事实冲突：`pi-integration-notes.md` 称 Docker `/config` 只读（代码无 `:ro`）；称 E2B 工作区 `/home/user`；`local-web-search.txt` 称 `auth.json` 为 pi 写入空对象 | 逐一订正：`/config` 为读写 bind mount（pi 需向其中 `npm/` 装插件）；E2B 工作区 `/home/user/workspace`；`auth.json` 为宿主运行期渲染的模型凭据（0600），`models-store.json`/`npm/` 才是 pi 运行时状态；§7 同步补充终帧去重语义 | 全仓库 grep `空对象`/`只读 bind` 无残留；与 `e2b_env.SANDBOX_WORKSPACE`、`docker_env.py` 挂载参数、`agent_env.prepare_agent_dir` 交叉核对一致 |

回归：Python 100 项 + Node 15 项全绿；`scripts/check-no-secrets.py` 真实命中 0。


## 前端 UI 与百鉴 UI 标准库对齐（2026-09-22）

目标：把工作台前端「像素级」对齐 `/Users/coldxiangyu/work/BR/glink-ui-library`（百鉴 UI 标准库），
且页面功能一项不减。前端仍是**无打包链的静态 ESM**，所以对齐靠**逐值抄写 + 逐值测量**落地，
口径与逐层对照表见 [ui-alignment.md](ui-alignment.md)。

### 代码范围

| 文件 | 变化 |
|---|---|
| `web/styles.css` | 454 → 1327 行：token 层改为抄标准库 `src/styles/globals.css`（调色板 / 语义 / 尺度 / 排版 / 海拔 / 动效），组件层按 `src/components/*.tsx` 的实测度量重写；外壳改成「8px 沟槽 + 左侧导航卡 + 右侧主工作区」 |
| `web/index.html` | 87 → 167 行：52px 栏头 + 路径段、导航分组、120px 输入器（发送键 / 状态 / 计数）、报告右槽 |
| `web/modules/workspace.mjs` | 导航行改为标准库 NavItem 形态；新增侧栏折叠（`data-sidebar` + 记忆）、右槽开合（`preview-open` + `aria-expanded`）、输入器自增高度（68–240） |
| `web/modules/run-view.mjs` | 思考折叠与工具调用行改为标准库 Thinking / ToolChips 形态（行高 28 / 圆角 8·6 / chip 11px）；工具行尾新增状态字；详情默认折叠、运行中自动展开 |
| `web/icons/`（新增） | 22 个 24×24 SVG，直接取自标准库 `public/icons/baijian/`，用同一种 mask + `currentColor` 渲染 |
| `scripts/check-ui-tokens.py`（新增） | token 合同门禁：173 项与标准库逐值比对 |
| `tests/browser/ui-parity.mjs`（新增） | 像素级对齐校验：26 个角色 × 计算样式逐值比对（几何 ≤1px） |
| `tests/test_ui_tokens.py`（新增） | 上面那把门禁的 pytest 入口（标准库不在场时 skip） |

### 实测

| 命令 | 结果 |
|---|---|
| `scripts/check-ui-tokens.py` | **173 项 token 逐值一致**，exit 0 |
| `node tests/browser/ui-parity.mjs --base-url http://127.0.0.1:7861` | **26 个角色 / 0 处不一致**（三块屏：首页、纯文字对话、进度与上下文），exit 0；证据 `docs/evidence/ui-parity.txt`、`ui-parity.json` |
| `node tests/browser/run-smoke.mjs --base-url http://127.0.0.1:7861` | 通过：真实 run 45.1s，`tools=2 toolsWithText=2 toolsRenderingTextTwice=0 texts=1 reportButton=true bannerHidden=true`，报告右槽 435px 且无横向溢出，历史重放 9 项，截图 `browser-run.png`，exit 0 |
| `.venv/bin/python -m pytest` | **102 项通过**（新增 2 项 token 门禁） |
| `node --test tests/web/*.test.mjs` | 15 项通过 |
| `scripts/check-no-secrets.py` | 扫描 132 文件，真实命中 0 / 占位符 8 |

### 逐项行为复验（真实浏览器，1512×900）

| 项 | 实测 |
|---|---|
| 外壳几何 | 视口 1512 时 shell=1512、sidebar=240、main=1248、栏头 52px（与参考页逐值相同） |
| 断点 | 1512/1250/1050/900/721/600 六档：侧栏 240→212→64、首页双列→单列、留白 104→64→48、输入器 680→贴边；六档 `scrollWidth == clientWidth`（无横向溢出） |
| 侧栏折叠 | 点击 → 64px 图标条，`aria-expanded=false`、可访问名切「展开侧栏」；刷新后保持；再点回 240 |
| 输入器激活态 | 聚焦时 1px 近黑描边（`--foreground`），不叠焦点环（UC-04） |
| 运行态 | 提交 → 状态行「运行中」+ 图标微光、`#run-banner` 可见、停止键可见；点击停止 → 「已取消 · 9.159s」、banner 收起、toast「已请求停止，等待 pi 收尾」 |
| 工具行折叠 | 运行中自动展开（看得到流式输出），终态回落为 chip 行；用户手动开合后不再被自动改写 |
| 报告右槽 | `preview-open` 时右槽 clamp(380,34vw,620)；≤900 转覆盖浮层；1512/1100/900/721 四档均无横向溢出；下载链接与关闭键可用 |
| 启动失败面 | 恢复卡与「重试」按钮取到设计 token（r16 / 1px `--border` / `--elev-surface` / 40 高主按钮） |

### 对齐口径与刻意偏离

- 比对口径：几何 ≤1px 取整差，颜色 / 圆角 / 排版 / 描边 / 海拔必须逐值相同；文本宽度等
  内容驱动属性按角色白名单跳过，参考屏没有的角色记为 SKIPPED 而不是默认通过。
- 刻意偏离 7 项（品牌标识、暗色主题、轨迹展开区宽度、右槽内容、首页信息架构、输入器能力、
  工具行尾状态字）逐条登记在 [ui-alignment.md](ui-alignment.md) §5。

### 未覆盖项

1. **提示条（toast）与自绘 tooltip** 不做逐值比对：标准库对应实现（sonner / Radix Portal）
   在参考页没有稳定可测的等效节点，按 token 落地（`--popover` / `--elev-surface` /
   `--corner-lg` / `--text-body`）。
2. **滚动条**不参与 `getComputedStyle` 比对；宽度 8px 与「滚动时才显色」的行为照抄。
3. **暗色主题**未实现（前端是亮色单主题），标准库 `.dark` 段未复制；
   `scripts/check-ui-tokens.py` 只比亮色块，这一点在脚本与文档里都写明。


### 返修（2026-09-23）：品牌标识 / 首页结构 / 报告栏两个缺陷 + 全功能自检

用户复验意见：「仔细对照 5300 的 index.html；logo 也替换；进行浏览器全功能测试，不要遗漏；
现在打开报告以后，实际正文无法滚动了」。

| 项 | 处理 | 证据 |
|---|---|---|
| 品牌标识 | `web/logo-icon.svg` 与标准库 `public/logo-icon.svg` **逐字节相同**（SHA-256 `b5f3409b71ff14378e04d81adafc575b1045f92f1972d3c01026cf78089873c0`），favicon 与侧栏 24×24 同一份；旧的 `glink-mark.svg` 删除 | 对齐校验新增 `brandMark` 角色（盒 24×24 + 资产名），27 角色 0 处不一致 |
| 首页结构 | 输入器从「贴底 dock」改为**落在正文流里**（标题 → 输入器 → 双列卡片），对话屏仍贴底；历史行右簇改相对时间、第二行写状态与耗时 | 对齐校验的 `composer` 角色在首页量到 680×120（与参考页同值） |
| 输入器工具行 | 左侧补键盘提示 `⌘/Ctrl` + `↵` + 「提交」（Kbd 形态抄自 `src/components/kbd.tsx`，非 macOS 自动显示 `Ctrl`） | 功能自检「输入器」4 项通过 |
| **报告正文滚不动** | 根因：`.shell.preview-open .surface` 只声明列不声明行，隐式 auto 行被内容撑高后被 `overflow:hidden` 裁掉 | 修复后 `preview-body scrollHeight 11061 / clientHeight 832`，`scrollTop` 可驱动；`ui-features` 两条断言 |
| **输入器压在报告上** | 根因：`.composer-dock` 绝对定位而 `.pane` 无定位，包含块落到 `.surface` | 修复后 `dockRight == paneRight`（990 == 990）；`ui-features` 断言 |

新增 `tests/browser/ui-features.mjs`（32 项全功能自检，Playwright，自带浏览器实例）：
外壳几何 / 品牌资产 / 宿主状态 / 首页 / 输入器 4 项 / 运行中 2 项 / 停止 / 历史重放 /
思考折叠 / 工具行 / 报告 6 项 / 侧栏 3 项 / 断点 3 项 / 跳转链接 / 控制台 / 空态 /
模块加载失败恢复面 / 注入失败只产生预期内报错。

实测：`ui-features` **32/32 通过**；`ui-parity` **27 角色 0 处不一致**（新增 brandMark）；
`run-smoke` 真实 run 通过；pytest 102 项、Node 15 项、token 173 项、`check-no-secrets` 真实命中 0。

证据：`docs/evidence/ui-features.txt` / `ui-features.json`、`ui-parity.txt` / `ui-parity.json`、
`browser-run.png`，以及并排对照截图（`sbs-ref-home.png` / `sbs-mine-home.png` 等，按需重跑
`node tests/browser/ui-parity.mjs` 与 `ui-features.mjs` 复现）。
