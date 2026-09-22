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
| `scripts/run-tests.sh` | **78 项通过**，exit 0（`test_host_app` 21 · `test_kernel_agent_env` 11 · `test_kernel_docker_env` 6 · `test_kernel_e2b_env` 7 · `test_kernel_rpc` 11 · `test_runner` 7 · `test_runs_api` 9 · `test_store` 6） |
| `node --test tests/web/*.test.mjs` | 11 项通过，exit 0 |
| `node tests/browser/run-smoke.mjs --base-url http://127.0.0.1:7861` | 通过（真实 run；思考折叠 / 工具卡片 / 报告预览 + 截图），exit 0 |

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
| 2) 模型与 E2B 配置与 FrontierAgent 同源，密钥不进前端/不落仓库 | ✅ | `OPENAI_*`/`E2B_*` 同名语义；`models.json` 用 `$OPENAI_API_KEY` 占位；交叉核验 ④ |
| 3) 搜索由 pi-web-access 完成，报告含可核验 URL，无 Serper/Jina | ✅ | local-web-search.txt（web_search + 10 URL；仓库无 Serper/Jina 依赖） |
| 4) Docker 与 E2B 各跑通一次真实 run（含 Node + pi 与凭据注入） | ✅ | docker-run.txt（11.5s/136 事件）、e2b-run.txt（68.4s/203 事件） |
| 5) 前端无 Glink 特有依赖、不解析终端文本、断线/迟到事件不破坏状态 | ✅ | run-stream.test.mjs（seq 去重/缺口/终态）、源码无终端解析、终态后主动关闭流 |
| 6) architecture.md + pi-boundary.md + 可重复 smoke 命令 | ✅ | 两份文档 + `scripts/smoke-local-run.py`、`tests/browser/run-smoke.mjs` |
| 7) 验收记录含范围/命令/退出状态/证据/未覆盖项 | ✅ | 本文档 |

## 5. 未覆盖项与已知限制

1. **E2B 面事件非实时**：E2B SDK 2.x 的 stdout 回调实测不触发，事件在 run 结束后一次性
   回放（顺序与词汇正确）。见 `e2b-run.txt`。
2. **E2B 面会话不持久**：会话文件留在沙箱内，销毁即失，`session_id` 为空。
3. **版本 pin 差异**：inproc 面已在 pi 0.87.0 验证；Docker 镜像与 E2B 模板仍 pin 0.86.1
   （已实测），按 `docs/pi-boundary.md` §3 重建即可升级，本次未重建。
4. **串行执行**：三种执行面都只允许同时一个 run（`409 run_busy`）。
5. **报告无强制校验**：是否产出 `report.md` 取决于 pi 遵循研究约定；缺失记为 `no_report`，
   宿主不伪造产物。
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
| README 与 `agent/README.md` 仍称「task-1 骨架阶段」 | 更新为交付状态与内容清单 | README、`agent/README.md` |
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

