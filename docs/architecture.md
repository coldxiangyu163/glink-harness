# 架构

本仓库把 pi（`@earendil-works/pi-coding-agent`）当作**独立内核进程**接入，宿主只做编排、
事件投影与呈现。本文描述进程拓扑、事件契约、三种执行面的差异、凭据流与目录边界；每条
结论都对应仓库里的实现或已验证的实测记录（见 `docs/pi-integration-notes.md` 与
`docs/evidence/`）。

## 1. 三层结构

| 层 | 位置 | 职责 | 不做什么 |
|---|---|---|---|
| 前端 | `web/` | 工作台外壳、运行时间线、报告预览 | 不解析终端文本，不做业务编排 |
| 宿主 | `host/pi_glink/` | 配置冻结、run 编排、事件总线、HTTP/SSE、存储 | 不导入 pi，不实现研究方法论 |
| 内核接入 | `host/pi_glink/kernel/` | 启动 pi、消费官方 headless 协议、事件归一化 | 不定制内核，不依赖未文档化接口 |
| 内核配置与扩展 | `agent/` | pi 的可版本化配置：settings、研究约定、扩展 | 不放密钥，不放宿主实现 |

依赖方向单向：`前端 → 宿主 HTTP → kernel → pi 进程`；`agent/` 只被 pi 读取。
内核包（node_modules 里的 pi）始终是外部依赖，仓库不持有它的源码。

## 2. 进程拓扑

```
浏览器 ──HTTP/SSE──► host（Python，单进程）
                       │
                       ├─ EventBus（每 run 单调 seq + 有界重放）
                       ├─ RunStore（meta.json / events.jsonl）
                       └─ KernelRunner ──┬─► inproc: pi 子进程（stdio RPC）
                                         ├─► docker: docker run -i（容器内 pi，stdio RPC）
                                         └─► e2b:    云沙箱内 pi --mode json（单向流）
```

三种执行面共享同一套输入契约与同一套产品事件，因此浏览器、存储与报告下载路径完全一致；
差异集中在 `kernel/` 内（`rpc.py` / `docker_env.py` / `e2b_env.py`）。

## 3. 事件契约

pi 的事件词汇只出现在 `kernel/projection.py`；下游（总线、SSE、前端、事件日志）只说产品
事件（`host/pi_glink/events.py`）。

| 产品事件 | 来源（pi） | 前端用途 |
|---|---|---|
| `run_started` / `run_stage` | 宿主 / `agent_start`、`compaction_*`、`auto_retry_*` | 状态条、阶段行 |
| `session_ready` | `get_state`（RPC 面）/ 沙箱元数据 | session id、模型、thinking 档位 |
| `turn_started` / `turn_finished` | `turn_start` / `turn_end` | 轮次分隔 |
| `thinking_delta` | `message_update.thinking_delta` | 思考折叠块（流式） |
| `assistant_delta` | `message_update.text_delta` | 正文流 |
| `assistant_message` | `message_end`（权威全文） | 收口后的 Markdown |
| `tool_started` / `tool_updated` / `tool_finished` | `tool_execution_*` | 工具卡片（含流式输出/错误态） |
| `usage_reported` | `turn_end.message.usage` | token 统计 |
| `kernel_status` | `extension_error` 等 | 警告提示 |
| `run_finished` / `run_failed` / `run_cancelled` | 宿主 | 终态、报告入口 |

传输细节：

* SSE 帧形如 `id: <seq>` / `event: <type>` / `data: {…}`，与既有工作台前端一致；
* `seq` 每 run 单调递增；重连时浏览器发 `Last-Event-ID`，服务端重放之后的帧；
* 前端模型按 `seq` 水位去重（重复帧丢弃、缺口计数可见），因此**迟到/重复事件不会破坏状态**；
* 载荷有界：工具结果与文本按上限裁剪并显式标注 `truncated`。

前端从不解析终端文本：`tests/web/run-stream.test.mjs` 里有对应断言。

## 4. run 生命周期

1. `POST /api/runs {prompt}` → 生成 `runId`，写 `meta.json`（status=running），占用唯一 run 槽；
2. 渲染运行期配置目录（`agent_env.prepare_agent_dir`），按 `LOCAL_WEB_EXECUTOR` 选择执行面；
3. 事件投影 → `EventBus`（实时给 SSE）+ `events.jsonl`（事后可查）；
4. `agent_settled` 是「这一轮完全落定」的判据；超时/取消/失败都显式落到 `stopReason`；
5. 收尾：检查工作区里的 `report.md`（缺失记为 `no_report`，不伪造），写终态与事件数，
   发布终态事件并关闭流。

同一时间只允许一个 run（`409 run_busy`）；取消请求在 pi 尚未启动时也被受理。

## 5. 三种执行面对照

| 维度 | inproc（本机） | docker | e2b |
|---|---|---|---|
| 内核进程位置 | 宿主机器 | 一次性容器 | 云沙箱 |
| 协议 | `--mode rpc`（双向 JSONL） | `--mode rpc`（跨容器 stdio） | `--mode json`（单向流） |
| 事件时序 | 实时 | 实时 | **run 结束后回放**（SDK 无 stdout 回调，见下） |
| 运行时来源 | 宿主安装的 Node/pi | 镜像自包含（`pi-glink-worker:latest`） | 模板自包含（`pi-glink-worker`） |
| 工作区 | `<data>/workspaces/<runId>` | 同左（挂到 `/workspace`） | 沙箱内 `/home/user/workspace`，结束后拉回宿主 |
| 会话文件 | `<data>/sessions`（宿主可查） | 挂到 `/sessions` | 沙箱内，随沙箱销毁（`session_id` 为空） |
| 模型凭据 | 子进程环境变量 | 按变量名转发（值不入 argv） | `Sandbox.create(envs=…)` |
| 搜索凭据 | 运行期 `web-search.json`（600） | 随 `/config` 挂载 | 上传到沙箱 `/config`（随沙箱消失） |
| 隔离强度 | 进程级 | 容器级 | 云端沙箱级 |

**已知差异（如实记录）**：E2B SDK 2.x 的 `commands.run(background=True, on_stdout=…)`
回调实测不触发，因此沙箱把事件写入 `/home/user/events.jsonl`，宿主在 run 结束后一次性
回放——事件的顺序、词汇与另两面一致，差别是**非实时**。详见
`docs/evidence/e2b-run.txt`。

## 6. 凭据流

* 模型：`OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL`（与 FrontierAgent 同名语义）。
  宿主渲染 `models.json`（`baseUrl` 与模型 id 是字面量，pi 不支持它们的 `$VAR` 插值），
  且**不含 `apiKey`**）与 `auth.json`（pi 自己的凭据存储，与 `/login` 写的同一个文件）。
  **模型 key 不进进程环境**：内核的 bash 工具跑 `env` 读不到它（三面真实 run 实测均为 0，见
  `docs/evidence/credential-boundary.txt`）。
* 搜索：pi-web-access 只从 `PI_CODING_AGENT_DIR/web-search.json` 读凭据。宿主按
  「`PI_GLINK_WEB_SEARCH_KEY` → 操作员本机文件」的顺序渲染该文件到运行期目录（权限 600）。
  无凭据不是错误：插件会回退到无密钥的 Exa MCP 检索。
* 浏览器永远拿不到密钥：`/api/config` 只返回模型名与是否已配置。

### 6.1 出站脱敏边界

宿主持有凭据，而 pi 的工具**可以**把它们回显出来：`bash` 能跑 `env`、`read` 能打开运行期
配置文件、模型也可能把值引用回回答。因此每一道产品出口都经过
`host/pi_glink/redaction.py` 的 `SecretRedactor`：

| 出口 | 处理 | 代码 |
|---|---|---|
| 事件投影 | 载荷里每个字符串递归脱敏 | `kernel/projection.py::project_event(frame, redactor)` |
| 事件总线 / `events.jsonl` | 发布前兜底再脱敏一次 | `kernel/runner.py::_emit` |
| 报告文件 | run 收尾时就地脱敏，并记 `redacted=true` | `kernel/runner.py::_collect_report` |
| 会话文件 | pi 自写的 JSONL 在收尾时脱敏（保持合法 JSONL） | `kernel/runner.py::_sanitize_session_file` |
| 报告下载 | 服务前脱敏，响应头 `X-Redacted` 标明是否发生 | `app.py::_send_report` |

检测同时覆盖**值**（宿主知道的 key）与**形状**（`sk-…`、`e2b_…`、`exa-…`、`NAME=value`、
`Bearer …`），所以宿主从未见过的第三方 token 也会被掉包。
`scripts/check-no-secrets.py` 用同一套规则扫描运行态产物，并把**文档占位符**
（如 README 里的 `sk-ant-...`）与真实值分开计数。

### 6.2 输入侧最小权限

pi 子进程拿到的不是宿主 `os.environ` 的副本：`kernel/agent_env.py` 只放行
PATH / HOME / 代理 / 证书等运行必需项，再注入指向运行期配置目录的 `PI_*`。宿主 shell 里其他
凭据（例如 `ANTHROPIC_API_KEY`、`AWS_SECRET_ACCESS_KEY`）不会进入 pi 进程，模型 key 也不在
环境里（回归：`tests/test_redaction.py::test_minimal_child_environment_does_not_inherit_host_credentials`、
`tests/test_kernel_agent_env.py::test_model_credential_goes_to_auth_json_not_the_environment`）。

**残留风险（如实记录）**：key 仍以文件形式存在于运行期 `auth.json`（pi 官方凭据机制、权限 0600、
仓库外），理论上可被工具用 `read` 打开并回显——这正是 §6.1 出站脱敏覆盖的场景（回归：
`tests/test_redaction.py::test_runner_redacts_a_tool_echo_before_the_log_and_the_bus`）。需要更强
隔离时应使用 Docker / E2B 执行面，并为内核配专用、限额的凭据。三面的凭据交付方式与已知差异见
`docs/evidence/{credential-boundary,docker-run,e2b-run}.txt`。

## 7. 目录边界

| 路径 | 属于 | 说明 |
|---|---|---|
| `host/pi_glink/` | 宿主 | `config` / `events` / `store` / `static_files` / `app` |
| `host/pi_glink/kernel/` | 接入层 | `rpc` / `projection` / `agent_env` / `docker_env` / `e2b_env` / `runner` |
| `agent/` | 内核配置模板 | `settings.json`、`AGENTS.md`、`research-prompt.md`、扩展位 |
| `web/` | 前端 | 外壳与事件视图（无打包步骤，原生 ESM） |
| `docker/` | 执行面 | `Dockerfile.worker`（容器）、`e2b/Dockerfile.e2b`（模板） |
| `scripts/` | 运维 | 启动、测试、真实 run 冒烟、E2B 模板构建 |
| `docs/` | 文档与证据 | 架构、边界、集成笔记、`evidence/` |
| `tests/` | 回归 | `test_*.py`（宿主/内核）、`web/*.test.mjs`（前端模型）、`browser/`（真实浏览器） |
| `<data_root>`（默认 `~/.pi-glink`） | 运行态 | `runs/<id>/{meta.json,events.jsonl}`、`workspaces/`、`sessions/`、`pi-agent/`（运行期配置） |

运行期配置目录 `pi-agent/` 是 pi 的 `PI_CODING_AGENT_DIR`：宿主在其中渲染 `models.json`
与 `web-search.json`，复制 `agent/` 的可版本化配置，pi 自己往里写 `auth.json` /
`models-store.json`。仓库里的 `agent/` 只是模板，永远不作为运行目录使用。

## 8. 可重复验证入口

```bash
scripts/run-tests.sh                                   # 宿主 + 内核回归（Python）
node --test tests/web/*.test.mjs                       # 前端事件模型回归
node tests/browser/run-smoke.mjs --base-url …          # 真实浏览器 run（截图 + DOM 断言）
.venv/bin/python scripts/smoke-local-run.py --base-url … --prompt "…"   # 真实 run 冒烟
scripts/run-local-web.sh                               # 本机执行面
LOCAL_WEB_EXECUTOR=docker scripts/run-local-web.sh     # 容器执行面
LOCAL_WEB_EXECUTOR=e2b scripts/run-local-web.sh        # 云沙箱执行面
```

三面的验收证据：`docs/evidence/local-m1-smoke.txt`、`docker-run.txt`、`e2b-run.txt`、
`local-web-search.txt`、`browser-run.{png,json}`。

## 9. 本阶段不做（边界）

场景能力包与档位、多账户与分享、资料库与上传/OCR、DOCX/PDF 导出与图表、报告审阅工作区、
多角色编排、上下文压缩调优、成本治理、E2B 模板的生产级硬化。这些都可以在不改内核的前提下
按同一套事件契约增量接入。
