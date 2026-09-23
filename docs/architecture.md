# 架构

本仓库把 pi（`@earendil-works/pi-coding-agent`）当作**独立内核进程**接入，宿主只做编排、
事件投影与呈现。本文描述进程拓扑、事件契约、三种执行面的差异、凭据流与目录边界；每条
结论都对应仓库里的实现或已验证的实测记录（见 `docs/pi-integration-notes.md` 与
`docs/evidence/`）。

## 产品方向与当前实现

产品的既定方向是多租户研究工作台：通过 Docker / E2B 为研究 run 提供独立执行环境，本地 `inproc` 保留为单 Agent 开发与调试入口。当前三种执行面都受单 run 槽限制，尚无租户身份和授权模型；这只是现阶段实现，不是把产品永久限定为单用户的设计决定。

多租户扩展必须同时明确并验证：服务端可信租户身份；run、工作区、事件、会话和产物的归属；列表、SSE、报告下载与取消的跨租户拒绝；运行期配置和凭据的独立交付；并发、超时、配额及资源回收。单独容器/沙箱提供执行隔离，但不自动解决宿主 API 授权、存储隔离或凭据访问。当前共享的配置目录和单 run 调度不能仅通过移除并发锁就升级为多租户实现。

具体接口与迁移在对应任务中决定，不预建无消费者的租户抽象；修改这些承诺遵循 [工程约束](engineering.md)。现有单 run 测试继续保护当前行为，将来的多租户变更在取得决定后同步替换相关断言，而不是把当前串行测试当作永久需求。

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
                       ├─ EventBus（每 run 单调 seq；先落盘再发布；有界实时队列）
                       ├─ RunStore（meta.json / events.jsonl / artifacts）
                       └─ KernelRunner ──┬─► inproc 适配器: pi 子进程（stdio RPC）
                                         ├─► docker 适配器: docker run -i（容器内 pi，stdio RPC）
                                         └─► e2b 适配器:    云沙箱内 pi --mode json（单向流）
```

三种执行面共享同一套输入契约与同一套产品事件，因此浏览器、存储与报告下载路径完全一致；
差异集中在 `kernel/` 内的适配器后面（`execution.py` + `rpc.py` / `docker_env.py` / `e2b_env.py`），
Run 管理不承担任何单面的收尾逻辑（§5.1）。

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
| `usage_reported` | `turn_end.message.usage` | token 统计（`usage` 为 run 累计，`requestUsage` 为本次请求快照） |
| `kernel_status` | `extension_error` 等 | 警告提示 |
| `run_finished` / `run_failed` / `run_cancelled` | 宿主 | 终态、报告入口 |

传输细节：

* SSE 帧形如 `id: <seq>` / `event: <type>` / `data: {…}`，与既有工作台前端一致；
* `seq` 每 run 单调递增；重连时浏览器发 `Last-Event-ID`，服务端重放之后的帧；
* 前端模型按 `seq` 水位去重（重复帧丢弃、缺口计数可见），因此**迟到/重复事件不会破坏状态**；
* 载荷有界：工具结果与文本按上限裁剪并显式标注 `truncated`；工具参数里的数组
  （`queries`/`urls` 等）按「字符串保持字符串、数组保持**有界**字符串数组」的契约投影，
  不压成单串，前端 `collectSources` 因此能拿到多查询/多 URL 语义。

### 3.1 提交先于发布

`EventBus.commit` 是唯一写入路径：分配 `seq` → 调用持久化回调追加 `events.jsonl` →
才写入内存环形窗口并通知实时订阅者。因此：

* **已提交的持久事件是重放事实源**。写盘失败的那一帧在浏览器、SSE 与日志里都不存在，
  并且不消耗 `seq`（不会制造缺口）；运行会以 `event_log_failed` 诚实失败，而不是让
  浏览器看到一条日志里没有的帧。
* `meta.json` 的 `event_count` 在终帧落盘后从日志实际行数回读，与读者看到的一致。
* 环形窗口只是「读完日志 → 挂上实时尾」之间的交接缓冲，不是历史来源；run 关闭时即释放，
  不会让所有 run 的历史常驻内存。用户历史（`events.jsonl`）不因任何 TTL 被删除。

### 3.2 重放与慢消费者

`GET /api/runs/{id}/events` 的顺序是：**先订阅实时尾，再读持久日志，最后按 `seq` 合并去重**。

* 活跃 run：日志里的已提交帧先写出，实时帧按 `seq > 已写出水位` 继续；读取期间关闭的 run
  也不会丢终帧（终帧已在日志里，或在订阅队列里）。
* 已结束/上次进程的 run：只读 `events.jsonl`，因此重放不受内存窗口容量影响，也无需重新启动 pi。
* 每个实时订阅者有一个**有界队列**（默认 2000 帧）。停止读取的客户端会被主动断开，而不是让
  队列无限增长；浏览器按 `Last-Event-ID` 重连并从日志补齐，因此断开不丢已提交历史。

### 3.3 流式凭据回显保护

投影与 `_emit` 对每个载荷做值 + 形状脱敏，但**整值匹配对逐帧流式文本不够**：同一个 known
secret 被模型分成两个 `assistant_delta` 时，两半都不会命中。因此跑批在 delta 通道上维护一个
尾部暂存（`redaction.StreamRedactor`）：

* 暂存「已知值的前缀、形状令牌未达最小长度的前缀、尚未闭合的 `NAME=value` / 引号值」；
* 在 `turn_end` / `message_end` / `agent_end` / `agent_settled` 边界 flush，因此在流内
  **任意分割位置**都不能拼回完整凭据，而合法相邻文本只是被推迟、不会被改写；
* 这只是**回显保护**，不承诺阻止 base64、跨工具输出拆分或网络外传等任意编码外泄；
  它不是安全沙箱，也不能替代凭据最小化。

### 3.4 用量语义

* pi 在 `turn_end.message.usage` 里给出**本次请求**的累计快照（input/output 等本身已是该次
  请求的累计，不是增量）；
* run 的用量是**跨请求求和**：每帧 `usage_reported.usage` 是当前 run 累计值（含 `requests`
  计数），`requestUsage` 保留该帧原始快照；同一 message 身份（`timestamp`/`id`）重复到达时
  只计一次，避免重试帧或重复事件双计；
* 宿主不内置模型价格表（`models.json` 不写 `cost`）：pi 没报 `cost` 时 run 记录也不伪造计费。

事件语义细节（前端消费前必须知道）：

* **`tool_updated.output` 是累计快照，不是增量**：pi `docs/rpc.md` 原文是
  “the accumulated output so far (not just the delta)”，客户端每次**替换**显示；
  投影层原样传递该快照，前端若按增量拼接会重复展示前缀（真实 run `ebf75f590601`：
  bash 更新长度 `0 → 53 → 495`，见 `docs/evidence/tool-stream-semantics.txt`）。
* **工具终态跟随 run**：未收到自身 `tool_finished` 的工具不会默认标 `done`——
  run 正常收尾为 `done`，失败为 `interrupted`，取消为 `cancelled`；已拿到终态帧的工具保持原状态。

前端从不解析终端文本：`tests/web/run-stream.test.mjs` 里有对应断言。

## 4. run 生命周期

1. `POST /api/runs {prompt}` → 生成 `runId`，写 `meta.json`（status=running），占用唯一 run 槽；
2. 渲染**本 run 的**运行期配置目录与 session 目录（`runs/<id>/pi-agent`、会话根下的 `<id>/`），
   按 `LOCAL_WEB_EXECUTOR` 选择执行面（见 §5）；
3. 事件投影 → `EventBus.commit`：先落 `events.jsonl`，再实时给 SSE（见 §3.1）；
4. 执行面把冻结输入交给内核，等待「落定」或超时/取消/进程退出；
5. 收尾分四步，互不覆盖：

   1. **结果判定**：把执行面结果与最终模型结果合成 run 终态（见下）；
   2. **产物回收**：先向执行面回收产物（E2B 从沙箱拉到工作区），再销毁执行面；随后把
      `report.md` 经安全读取（拒绝符号链接/非普通文件/超限/越界）复制到宿主拥有的
      `runs/<id>/artifacts/report.md` 并脱敏。拒绝的产物记为 `report_rejected` 并发出
      `kernel_status` 警告，不伪造报告；缺失记为 `no_report`；
   3. **凭据清扫**：工作区文本产物与会话文件各过一遍脱敏（符号链接一律跳过，改写用原子替换，
      绝不写穿链接）；
   4. **落定与释放**：写终帧、回读 `event_count`、关闭流、释放 run 槽；执行面的
      `close()` 结果（`released`/`failures`）单独记录，清理失败不谎称已释放。

**终态判定**（`agent_settled` 不是成功信号）：

| 事实 | 判定 |
|---|---|
| 用户取消 | `cancelled` / `stop_reason=abort` |
| 执行超时、内核在接受指令后退出、协议/执行错误 | `failed`（`timeout` / `kernel_exit` / `execution_error`） |
| 事件日志写盘失败 | `failed` / `event_log_failed`（该帧不在任何读取面出现） |
| 最终 assistant `stopReason=error`（重试后仍失败） | `failed` / `model_error`，错误取自 `errorMessage`/`finalError` |
| 最终 assistant `stopReason=aborted`（非用户取消） | `failed` / `aborted` |
| 最终 assistant `stopReason=length` | `finished`，但 `model_stop_reason=length`、`stop_reason=length`，明确标注交付不完整 |
| 中间一次错误后重试成功 | `finished`（只看最终消息，不把可恢复错误永久判失败） |
| 正常结束但没有 `report.md` | `finished` / `stop_reason=no_report` |

**重启对账与关闭**：宿主启动时 `RunManager.reconcile()` 扫描遗留 `running` 记录（本进程不持有），
追加 `run_failed(interrupted)` 终帧并写回元数据；容器的 `container_name` 与沙箱的 `sandbox_id`
在拿到身份时即持久化，`orphaned_resources()` 供操作员做有界对账，**不自动操作真实遗留资源**。
进程退出时 `RunManager.shutdown()` 取消并等待本进程 run，未在限时内释放的 run id 原样报告
（进程返回码非零），不谎称已清理。

同一时间只允许一个 run（`409 run_busy`）；取消请求在 pi 尚未启动时也被受理。

## 5. 三种执行面对照

| 维度 | inproc（本机） | docker | e2b |
|---|---|---|---|
| 内核进程位置 | 宿主机器 | 一次性容器 | 云沙箱 |
| 协议 | `--mode rpc`（双向 JSONL） | `--mode rpc`（跨容器 stdio） | `--mode json`（单向流） |
| 事件时序 | 实时 | 实时 | **run 结束后回放**（SDK 无 stdout 回调，见下） |
| 运行时来源 | 宿主安装的 Node v24.16.0 / pi | 镜像自包含（`pi-glink-worker:latest`，node v24.21.0 + pi 0.86.1；镜像名在启动时冻结） | 模板自包含（`pi-glink-worker`，node v24.21.0 + pi 0.86.1） |
| 运行期配置 | `runs/<id>/pi-agent`（本 run 独占） | 同目录挂到 `/config` | 同一目录整棵上传到沙箱 `/home/user/config` |
| 工作区 | `<data>/workspaces/<runId>` | 同左（挂到 `/workspace`） | 沙箱内 `/home/user/workspace`，结束后拉回宿主 |
| 会话文件 | 会话根下的 `<id>/`（宿主可查） | 该目录挂到 `/sessions`；宿主按挂载映射回本 run 目录后脱敏 | 沙箱内，随沙箱销毁（`session_id` 为空） |
| 模型凭据 | 本 run 的 `auth.json`（0600，不进进程环境） | 随 `/config` 挂载 | 随运行期配置上传（不进沙箱进程环境） |
| 搜索凭据 | 本 run 的 `web-search.json`（600） | 随 `/config` 挂载 | 上传到沙箱 `/config`（随沙箱消失） |
| 隔离强度 | 进程级 | 容器级 | 云端沙箱级 |

### 5.1 统一执行 Interface

三种面只在「怎么把内核拉起来、怎么说话、怎么取消、怎么回收、怎么释放」上不同；这些差异全在
`kernel/execution.py` 的 `ExecutionAdapter` 后面。Run 管理只驱动一个固定顺序：

```
start() → session_info() → deliver_input() → wait(cancel, deadline)
        → recover_artifacts(destination) → close() → CleanupReport
```

* **冻结输入**：`ExecutionSpec` 携带 run id、prompt、system prompt、本 run 工作区与运行期配置、
  timeout；适配器不在运行时重读全局环境（Docker 镜像、E2B 模板、凭据与路径均在配置冻结时确定）。
* **事件**：三个适配器都只把内核帧交给同一个 `on_frame` 回调（投影 + 持久化在宿主侧），
  因此浏览器看到同一套产品事件。
* **最终 Outcome**：`ExecutionOutcome` 只携带执行层事实（是否落定、exit code、取消、超时、错误）；
  最终终态由宿主结合模型结果与产物策略判定（§4）。
* **取消/截止时间**：取消是一个 flag（run 管理拥有），`cancel()` 只负责提前结束阻塞；超时由
  `wait(deadline)` 的适配器实现处理，返回 `timed_out` 而不是抛异常。
* **产物回收与关闭**：`recover_artifacts` 先把该面的产物放到宿主工作区（inproc/Docker 本来就是
  宿主目录，是 no-op；E2B 从沙箱拉回，逐文件限幅并拒绝路径分隔符），然后 `close()` 才销毁
  进程/容器/沙箱并报告 `CleanupReport(released, failures)`。清理失败会被记入 run 记录，不谎报已释放。
* **持久执行身份**：`identity()` 给出容器名或沙箱 id，run 管理在拿到时即写入 `meta.json`，
  供重启对账（§4）使用；它也是唯一的资源归属凭据，不靠猜名字。

### 5.2 每 run 独立可写环境（B2）

一个 run 拥有自己的运行期配置目录、session 目录与工作区：

```
<data_root>/runs/<runId>/
├── meta.json / events.jsonl      run 记录与持久事件
├── pi-agent/                     PI_CODING_AGENT_DIR（models.json、auth.json、模板副本）
└── artifacts/report.md           宿主拥有的脱敏产物副本
<data_root>/sessions/<runId>/      PI_CODING_AGENT_SESSION_DIR（基目录可配置，每 run 一个子目录）
<data_root>/workspaces/<runId>/   内核工作目录
```

渲染每个模板条目都是「先删再拷」，并且每次渲染都会重建（或删除）`auth.json` /
`web-search.json`；因此上一 run 的扩展、skill 或凭据不会因增量拷贝而残留。共享项只限于
仓库 `agent/` 模板目录（只读输入）和 `data_root` 本身。

**已知差异（如实记录）**：E2B SDK 2.x 的 `commands.run(background=True, on_stdout=…)`
回调实测不触发，因此沙箱把事件写入 `/home/user/events.jsonl`，宿主在 run 结束后一次性
回放——事件的顺序、词汇与另两面一致，差别是**非实时**。沙箱内的 JSONL 按 LF 分帧
（与 RPC 面共用同一语料验证：合法 JSON 字符串里的 U+2028/U+2029 不得拆帧）；配置交付带有
`(相对路径, sha256)` 清单，`AGENTS.md`、`settings.json`、`extensions/`、`skills/`、`prompts/`
与生成文件都按同一路径语义送达。详见 `docs/evidence/e2b-run.txt`。

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
| 事件投影 | 载荷里每个字符串递归脱敏；delta 走尾部暂存（§3.3） | `kernel/projection.py::project_event(frame, redactor)`、`redaction.py::StreamRedactor` |
| 事件总线 / `events.jsonl` | 提交（先落盘）前再脱敏一次 | `kernel/runner.py::_emit` |
| 报告文件 | 安全读取、脱敏、复制到宿主拥有的 `runs/<id>/artifacts/report.md`，并记 `redacted=true` | `kernel/runner.py::_recover_report` |
| 会话文件 | pi 自写的 JSONL 在收尾时脱敏；只允许本次 run 会话子目录内的普通文件（拒绝链接/越界），Docker 面先把容器路径 `/sessions/…` 映射回该挂载源 | `kernel/runner.py::_sanitize_session_file`、`docker_env.host_session_path` |
| 报告下载 | 只服务 run 目录内校验过的普通文件，服务前再脱敏；响应头 `X-Redacted` 标明是否发生 | `app.py::_send_report`、`artifacts.py` |

检测同时覆盖**值**（宿主知道的 key）与**形状**（`sk-…`、`e2b_…`、`exa-…`、`NAME=value`、
`Bearer …`），所以宿主从未见过的第三方 token 也会被掉包；跨 delta 的分割由 §3.3 的暂存覆盖。
`scripts/check-no-secrets.py` 用同一套规则扫描运行态产物，并把**文档占位符**
（如 README 里的 `sk-ant-...`）与真实值分开计数。

**脱敏不是安全沙箱**：它处理的是「宿主已能把值交付给内核、内核又把它回显到产品面」这一层的
回显保护；任意编码、分拆到不同工具输出、写入二进制或直接网络外传不在承诺范围。需要更强隔离
时应使用 Docker / E2B 执行面，并为内核配专用、限额的凭据（完整多租户设计见
`docs/multitenancy-plan.md`）。

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
| `host/pi_glink/kernel/` | 接入层 | `rpc` / `framing` / `projection` / `agent_env` / `execution` / `docker_env` / `e2b_env` / `runner` |
| `host/pi_glink/artifacts.py` | 宿主 | 产物与内核返回文件的安全读取/写入（不跟随链接、限幅、原子替换） |
| `agent/` | 内核配置模板 | `settings.json`、`AGENTS.md`、`research-prompt.md`、扩展位 |
| `web/` | 前端 | 外壳与事件视图（无打包步骤，原生 ESM）；视觉按百鉴 UI 标准库逐值对齐，见 `docs/ui-alignment.md` |
| `docker/` | 执行面 | `Dockerfile.worker`（容器）、`e2b/Dockerfile.e2b`（模板） |
| `scripts/` | 运维 | 启动、测试、真实 run 冒烟、E2B 模板构建 |
| `docs/` | 文档与证据 | 架构、边界、集成笔记、`evidence/` |
| `tests/` | 回归 | `test_*.py`（宿主/内核）、`web/*.test.mjs`（前端模型）、`browser/`（真实浏览器） |
| `<data_root>`（默认 `~/.pi-glink`） | 运行态 | `runs/<id>/{meta.json,events.jsonl,pi-agent/,artifacts/}`、`sessions/<id>/`、`workspaces/<id>/` |

运行期配置目录 `runs/<id>/pi-agent/` 是 pi 的 `PI_CODING_AGENT_DIR`：宿主在其中渲染
`models.json` 与 `web-search.json`，复制 `agent/` 的可版本化配置，pi 自己往里写 `auth.json` /
`models-store.json`。仓库里的 `agent/` 只是模板，永远不作为运行目录使用；`pi-agent/` 不共享，
所以并发 run 不会互写凭据或扩展。

## 8. 可重复验证入口

```bash
bash scripts/check.sh                                  # 默认无模型交付检查（Python + Node）
scripts/run-tests.sh                                   # 仅 Python 的低层入口
node --test tests/web/*.test.mjs                       # 前端事件模型 + 导航生命周期回归
scripts/check-ui-tokens.py                             # token 层与百鉴标准库逐值比对
node tests/browser/ui-parity.mjs --base-url …          # 26 个角色的计算样式与标准库逐值比对
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
多角色编排、上下文压缩调优、成本治理、E2B 模板的生产级硬化。多租户身份、授权和并发隔离亦尚未实现，但属于上述既定产品方向，不是永久排除项；具体接口、准入、配额、迁移与验收见 [多租户方案](multitenancy-plan.md)，相关工作在不改内核的前提下按明确契约增量接入。
