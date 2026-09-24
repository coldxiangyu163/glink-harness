# 架构

本仓库把 pi（`@earendil-works/pi-coding-agent`）当作**独立内核进程**接入，宿主只做编排、
事件投影与呈现。本文描述进程拓扑、事件契约、三种执行面的差异、凭据流与目录边界；每条
结论都对应仓库里的实现或已验证的实测记录（见 `docs/pi-integration-notes.md` 与
`docs/evidence/`）。

## 产品方向与当前实现

产品的既定方向是多租户研究工作台：通过 Docker / E2B 为研究 run 提供独立执行环境，本地 `inproc` 保留为单 Agent 开发与调试入口。默认 `PI_GLINK_TENANCY=local` 仍是单用户、全局单 run；`PI_GLINK_TENANCY=multitenant` 时启用单宿主多租户实现（§10）：身份入口二选一——本机认证网关注入短时效签名断言（`PI_GLINK_AUTH_MODE=gateway`，默认），或站内邀请注册 + 登录 + 服务端会话（`PI_GLINK_AUTH_MODE=accounts`，§10.6）；两者都只在服务端生成 `TenantContext`，run 带租户归属，模型凭据默认每租户自备（显式 `PI_GLINK_MODEL_CREDENTIALS=shared` 时由宿主统一提供），Docker/E2B 可并发。该实现目前只有**合成验证**（临时密钥/账号库、自签断言、fake 执行面、临时数据根）；真实网关、真实 HTTPS 终止、邮件交付、Docker/E2B live 与生产数据验收未覆盖，不能由单 run smoke 替代。

多租户实现已覆盖服务端可信身份、run/事件/报告/取消的归属过滤、每 run 凭据交付与脱敏边界、显式并发准入与 fail-closed 资源台账（未确认释放的持有计入 owner 租户与全局槽位，可由在线自动对账或离线人工裁决释放）；仍未覆盖多宿主租约、共享存储、真实网关与云环境的资源上限执行。单独容器/沙箱提供执行隔离，但不自动解决宿主 API 授权、存储隔离或凭据访问，也不构成本仓库对容器/沙箱实际隔离强度的保证。

具体接口与迁移在对应任务中决定，不预建无消费者的租户抽象；修改这些承诺遵循 [工程约束](engineering.md)。现有单 run 测试继续保护当前行为，将来的多租户变更在取得决定后同步替换相关断言，而不是把当前串行测试当作永久需求。

## 1. 三层结构

| 层 | 位置 | 职责 | 不做什么 |
|---|---|---|---|
| 前端 | `web/` | 工作台外壳、运行时间线、报告预览 | 不解析终端文本，不做业务编排 |
| 宿主 | `host/pi_glink/` | 配置冻结、run 编排、事件总线、HTTP/SSE、存储 | 不导入 pi，不实现研究方法论 |
| 内核接入 | `host/pi_glink/kernel/` | 启动 pi、消费官方 headless 协议、事件归一化 | 不定制内核，不依赖未文档化接口 |
| 内核配置与扩展 | `agent/` | pi 的可版本化配置：settings、工作约定、扩展 | 不放密钥，不放宿主实现 |

依赖方向单向：`前端 → 宿主 HTTP → kernel → pi 进程`；`agent/` 只被 pi 读取。
内核包（node_modules 里的 pi）始终是外部依赖，仓库不持有它的源码。

## 2. 进程拓扑

```
浏览器 ──HTTP/SSE──► host（Python，单进程）
                       │
                       ├─ DataRootOwnership（data_root/host.lock 独占；先证明所有权再对账）
                       ├─ EventBus（每 run 单调 seq；先落盘再发布；有界实时队列）
                       ├─ RunStore（meta.json / events.jsonl / artifacts；终帧即终态提交）
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
才写入内存环形窗口并通知实时订阅者。同一 run 的提交由每 run 的提交锁串行；持久化回调
（可能等待磁盘同步）不持有总线全局锁，所以 `is_open`/`last_seq` 与新订阅永远不等磁盘，
且只反映持久化已经返回的帧。因此：

* **已提交的持久事件是重放事实源**。写盘失败的那一帧在浏览器、SSE 与日志里都不存在，
  并且不消耗 `seq`（不会制造缺口）；运行会以 `event_log_failed` 诚实失败，而不是让
  浏览器看到一条日志里没有的帧。
* **读者只看已提交前缀**（`store.py::_CommitWatermarks`）：持有数据目录的宿主进程为每个它
  追加的日志维护提交水位；水位之外的字节正在写入，对进程内所有读者（详情、列表、报告、
  SSE 重放、重启对账）都不可见。普通帧整行写完后水位前移；写入失败截回原长度，崩溃留下的
  半行先补换行再追加，读者不会看到半帧。重启后没有水位，文件本身就是已提交记录。
* **终帧就是终态提交**：`RunStore.commit_terminal()` 先写入并 `fsync` 终帧，**成功之后**水位才
  越过它，此时 run 才算终态，日志随即封存（终帧之后不能再追加）；写入或 `fsync` 失败则截回、
  抛错，任何读者从未看到过该终帧，run 保持同一个未提交状态。普通帧不能经 `append_event`
  写终帧。终帧载荷就是终态记录（结论、报告、清理结果）。`meta.json` 只保存受理与运行中事实，
  终态字段是提交**之后**写入的落定副本。`RunStore.get()` 在 `meta.json` 仍为 `running` 时投影
  已提交的终帧，因此详情、列表、报告下载与 SSE 重放在终帧发布的瞬间、落定副本写失败之后、
  重启之后都读到同一个已提交事实，且不会先出现再撤回；`event_count` 取终帧 `seq`。
  耐久性以日志文件的 `fsync` 为界（不额外做目录 `fsync` 或 macOS `F_FULLFSYNC`）。
* **进程内索引只是加速器**：宿主为每个 data_root 维护一份 run 记录索引，列表、按租户列表、
  `unsettled` 与资源台账都从它读取，准入在 manager 锁内不再遍历 run 目录。它不是第二条事实源：
  终帧只在 `fsync` 与提交水位越过之后才进入索引，写入失败时不产生任何可见投影；`scan_all()`
  保留全量扫描作为对照；重启时先从磁盘重建，语义与逐目录读取完全一致。
* 环形窗口只是「读完日志 → 挂上实时尾」之间的交接缓冲，不是历史来源；run 关闭时即释放，
  不会让所有 run 的历史常驻内存。用户历史（`events.jsonl`）不因任何 TTL 被删除。

### 3.2 重放与慢消费者

`GET /api/runs/{id}/events` 的顺序是：**先订阅实时尾，再读持久日志，最后按 `seq` 合并去重**。

* 活跃 run：日志里的已提交帧先写出，实时帧按 `seq > 已写出水位` 继续；读取期间关闭的 run
  也不会丢终帧（终帧已在日志里，或在订阅队列里）。
* 已结束/上次进程的 run：只读 `events.jsonl`，因此重放不受内存窗口容量影响，也无需重新启动 pi。
* 每个实时订阅者有一个**有界队列**（默认 2000 帧）。停止读取的客户端会被主动断开，而不是让
  队列无限增长；浏览器按 `Last-Event-ID` 重连并从日志补齐，因此断开不丢已提交历史。
* **连接数有界**：全局上限 `PI_GLINK_MAX_GLOBAL_STREAMS`（默认 64），多租户下每个租户另有
  `PI_GLINK_MAX_TENANT_STREAMS`（默认 16；local 只用全局上限）。额度在查看 run 之前预留，超额
  在写出任何流响应头之前返回 `429 {"error":"stream_limit","scope","limit"}`（附
  `Retry-After`，不含 run id，也不透露他租户是否存在）；流的每条结束路径（正常结束、客户端断开、
  断言/会话过期、慢消费者断开）都会归还额度。前端对非 200 响应做有界重连（1.5/4/10 秒三次后报
  失败，连上后重置）。两个已知取舍如实记录：浏览器读不到该 429 的 JSON 体，受拒后重建的
  `EventSource` 不带 `after`，会从 `seq 0` 重新全量重放（模型按 `seq` 去重，语义正确但有额外
  流量）；确实不存在的 run（404）也要等三次重试（约 15 秒）才显示失败。

### 3.3 流式凭据回显保护

整值匹配对逐帧流式文本不够：同一个 known secret 被模型分成两个 `assistant_delta` 时，两半都
不会命中。`redaction.py` 因此只有一个检测函数 `SecretRedactor.spans()`，给出需要遮盖的字符
区间（已知值的**全部**出现位置，含重叠出现；`NAME=value` / JSON 凭据字段的值；形状令牌），
重叠区间合并后各替换一次。流式与整段脱敏共用它：

* 每个检测器同时声明「开放形式」——末尾仍可能被后续文本补全或延长的起点（已知值的真前缀、
  字面前缀、未达阈值或仍触及末尾的令牌、未闭合的赋值/引号值、未闭合的带引号标识符）；
* `StreamRedactor` 对「暂存 + 新 delta」计算区间与最早开放起点，只释放其之前的文本；若该
  起点落在某个完整命中内部，则退到该命中的起点，**从不切开一个完整命中**（R1 的前后缀重叠
  凭据正是被旧的「先截尾再脱敏」切开的）；释放的文本按整段区间渲染，并保留一个字符的左侧
  上下文，使词边界规则与整段一致；
* 因而**流式释放的拼接等于整段脱敏的结果**（`tests/test_redaction.py` 对语料的所有单点分割、
  随机多帧与逐字符分割逐一断言）；合法相邻文本只被推迟、不被改写；
* `kernel/projection.py::Projector` 是每 run 的投影边界：delta 以**原文**进入同一个流，
  其他载荷逐帧脱敏；在 `turn_end` / `message_end` / `agent_end` / `agent_settled` 边界及 run
  收尾时释放暂存，使尾部归属原内容块；
* 暂存有界且失败即闭合：任何时刻最多暂存 `HOLD_CAP`（8192 字符）未判定文本；若未判定
  尾部将超过该界（超长已知值、重叠重复链、未终止的赋值或令牌），先释放已判定部分，未判定
  部分只输出一个 `[REDACTED]`，该类通道在本内容块边界前不再输出。未判定文本**从不**以原文
  释放；块末权威的 `assistant_message` 仍给出整段脱敏后的完整回答；
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
* **权威回答帧可能被截断，截断时会声明**：`assistant_message.text` 只保留
  `ASSISTANT_TEXT_LIMIT`（16000 字符）并带上 `truncated: true`；保留上限而不取消，是因为回答、
  脚本与工具输出都可能很长，单帧不受限会让 `events.jsonl` 与重放环形窗口膨胀（事件日志是
  历史事实源，不因 TTL 删除），而流式 delta 已经把完整正文交给浏览器。前端因此只在
  **非截断**的权威帧上用 pi 的副本覆盖流式正文；截断帧且流式正文更长时保留流式正文，
  仅关闭该内容块并标记 `truncated`（重放历史亦然），避免“终帧比流式更短”把回答改短。
* **`stopReason=length` 是交付不完整**：run 仍为 `finished`，但前端在结论区显示“交付不完整”
  提示而不是当作普通完成（§4 的终态判定表）。
* **工具终态跟随 run**：未收到自身 `tool_finished` 的工具不会默认标 `done`——
  run 正常收尾为 `done`，失败为 `interrupted`，取消为 `cancelled`；已拿到终态帧的工具保持原状态。

前端从不解析终端文本：`tests/web/run-stream.test.mjs` 里有对应断言。

## 4. run 生命周期

1. `POST /api/runs {prompt, parentRunId?}` → 准入（见下）→ 生成 `runId`，写 `meta.json`（status=running）。`parentRunId` 只接受已有会话的最新已结束轮次：从父 run 专属目录安全读取脱敏后的会话文件，复制到新 run 专属目录，用 pi 官方 `--session` 续接；会话不可用或已存在后续轮次时拒绝，不冒充追问。**多租户模式下父 run 还必须属于调用者自己的租户**：未知/异租户/无 owner 的父 run 在读取它的会话、报告、状态或子 run 计数之前就按未知 run 拒绝（404）。前端按父链展示连续对话，侧栏只列最新一轮；
2. 渲染**本 run 的**运行期配置目录与 session 目录（`runs/<id>/pi-agent`、会话根下的 `<id>/`），
   按 `LOCAL_WEB_EXECUTOR` 选择执行面（见 §5）；
3. 事件投影 → `EventBus.commit`：先落 `events.jsonl`，再实时给 SSE（见 §3.1）；
4. 执行面把冻结输入交给内核，等待「落定」或超时/取消/进程退出；
5. 收尾把四类事实分开，各有唯一 owner 与提交点（`kernel/runner.py`）：

   1. **执行事实**：执行面 outcome、最终模型结果、事件日志健康度，run 处于 open 阶段时收集；
   2. **产物事实**：先向执行面回收产物（E2B 从沙箱拉到工作区），再 `close()` 执行面；随后经
      目录句柄安全读取 `report.md`（见 §6.1），脱敏后写入宿主拥有的
      `runs/<id>/artifacts/report.md`。拒绝的产物记为 `report_rejected` 并发出 `kernel_status`
      警告，缺失记为 `no_report`；工作区与会话文件各过一遍脱敏；
   3. **结论（verdict）**：在 run 锁内**一次性**由全部事实与取消标志判定，并立即以同步追加
      终帧提交（§3.1，`fsync` 成功才可见）。这是 run 的**线性化点**：之前受理的取消必然体现为 `cancelled`；之后的
      取消被拒绝并返回已提交状态；之后到达的内核帧被丢弃，终帧之后不再有任何帧；
   4. **资源释放**：`close()` 的 `CleanupReport`（`released: true/false`，异常视为 `false`）写在
      终态记录里。它独立于结论，也独立于线程是否结束（见下「准入与资源」）。

   提交成功后写落定副本、关闭实时流、移出执行表。若终帧写入或 `fsync` 失败（如磁盘满、I/O 错误），**没有任何读面
   显示终态**：run 保持 `running`、保留执行槽与实时流，停止请求返回 `finalizing`，
   `shutdown()` 报告该 run，下次启动由新 owner 按中断收口。

**终态判定**（`agent_settled` 不是成功信号）：

| 事实 | 判定 |
|---|---|
| 提交点之前受理的用户取消 | `cancelled` / `stop_reason=abort`（已回收的报告保留；事件日志错误记入 `error`） |
| 事件日志写盘失败 | `failed` / `event_log_failed`（该帧不在任何读取面出现） |
| 执行超时、内核在接受指令后退出、协议/执行错误 | `failed`（`timeout` / `kernel_exit` / `execution_error`） |
| 最终 assistant `stopReason=error`（重试后仍失败） | `failed` / `model_error`，错误取自 `errorMessage`/`finalError` |
| 最终 assistant `stopReason=aborted`（非用户取消） | `failed` / `aborted` |
| 最终 assistant `stopReason=length` | `finished`，但 `model_stop_reason=length`、`stop_reason=length`，明确标注交付不完整（前端以结论区的“交付不完整”提示呈现，不当作普通完成） |
| 中间一次错误后重试成功 | `finished`（只看最终消息，不把可恢复错误永久判失败） |
| 正常结束但没有 `report.md` | `finished` / `stop_reason=no_report`（被拒为 `report_rejected`） |

**取消**：`POST /api/runs/{id}/stop` 与结论在同一把锁上裁决。受理返回 `200 {"requested": true}`，
结论必为 `cancelled`（执行面自身的 cancel 只缩短等待，失败不改变受理）；已越过提交点返回
`409 run_not_cancellable`，带已提交的 `status` 与 `reason`（`terminal`，或提交失败时的
`finalizing`）；未知 run 返回 `404 unknown_run`。取消请求在 pi 尚未启动时也被受理。

**准入与资源**：新 run 只在「本进程没有执行中的 run（仅 local 模式；否则 `409 run_busy`）」且容量未满时
受理。资源台账直接由持久记录导出：未终态的 run、`cleanup.released` 不为 `true`（`false` 为已知失败，
`null` 为未知）且没有裁决的终态 run 都占用容量；早于资源跟踪的历史记录（无 `cleanup`）不参与。重启不会
清空台账。

* **local 单用户**：任一持有未确认释放即 `409 resources_unreleased`（附 run id），且保持全局单 run 槽位。
* **multitenant**：持有计入其 owner 租户的并发槽位**和**全局槽位（无 owner 的 legacy 记录只计全局），
  与 `PI_GLINK_MAX_TENANT_RUNS` / `PI_GLINK_MAX_GLOBAL_RUNS` 一同比较，超额返回
  `429 quota_exceeded`（scope/limit，不含 run id）；持有不会被遗忘，达到全局上限时所有新准入都被拒绝
  （fail-closed）。
* **损坏记录**：run 目录存在但 `meta.json` 不可读/不可解析时，列表、详情与重放一律隐藏它（不向租户
  暴露），但资源台账把它计为**全局持有**（owner 未知），`--unreleased` 列出并标注 `corrupt`；裁决写入
  该 run 目录内的 `resource-adjudication.json` 侧车，损坏的 `meta.json` 字节不变。

释放容量有三条路：执行面清理确认 `released: true`、在线自动对账确认，或操作员在宿主**停止时**以
`python -m pi_glink.app --mark-released <runId> --note "<核对说明>"` 记录人工裁决（`--unreleased`
列出待核对项）。人工裁决写出 `resource_adjudication{decision, source: "operator", note, at}`，自动对账
写出 `source: "auto-verified"` 与 `method`；两者都保留原清理事实与已提交的终态。离线运维命令不接触
Docker/E2B，也不删除任何资源。

**在线自动对账**：宿主持有数据目录时，按 `PI_GLINK_RESOURCE_RECONCILE_SECONDS`（默认 60，0 关闭）在
后台线程周期性确认未释放的持有。探测只使用 run 记录里**持久化的执行身份**，且按记录选面：有
`container_name` 就按名字 `docker inspect`（不存在即确认；仍存在则先按 `stop_argv` 语义幂等
`docker rm -f` 后再次确认），有 `sandbox_id` 就调用 E2B SDK 按 id 终止/查询（凭据取自冻结配置，
不依赖进程环境）。不按名字模式猜测，不列举或批量操作任何资源；两种身份都有的记录必须都确认。
「已不存在」只认执行面的契约（inspect 的 no-such、SDK 的 `False` 返回或 `SandboxNotFoundException`
类型）：探测失败、超时、鉴权错误或无法判定的返回一律保持持有。对账不在准入锁内执行，关闭宿主时
停止该线程；`inproc` 宿主不启用它（不取得外部资源；遗留的外部身份仍走离线人工裁决）。

**所有权、重启对账与关闭**：宿主先取得 `data_root/host.lock` 的独占 `flock`（进程存活期间
持有、退出即释放；同进程二次获取也被拒），**再**对账和绑定端口。同一 data_root 的第二个宿主
——无论同端口还是不同端口——在读任何记录前以 `DataRootBusy` 退出，不改动活跃 owner 的 run。
`RunManager.reconcile()` 要求持有所有权：遗留 `running` 记录若已有提交的终帧，只写落定副本；
否则提交 `run_failed(interrupted)`，资源状态为 `released: null`（未知），并持续占用容量直至裁决。
容器名/沙箱 id 在拿到时即持久化，`unreleased_resources()` 供操作员做有界对账。除在线自动对账按已持久化
身份执行的那一次幂等删除/终止（上文）之外，宿主不操作任何其他遗留资源：没有身份、探测不确定或
不属于本记录的容器/沙箱永远不会被触碰。进程退出时 `RunManager.shutdown()` 取消并等待本进程 run，
返回所有「终态未提交或资源未确认释放」的 run id（包括更早遗留的持有与不可读记录），进程返回码非零。
`flock` 只覆盖单机本地文件系统，不是多宿主租约（见 [多租户方案](multitenancy-plan.md) §3.2）。

## 5. 三种执行面对照

| 维度 | inproc（本机） | docker | e2b |
|---|---|---|---|
| 内核进程位置 | 宿主机器 | 一次性容器 | 云沙箱 |
| 协议 | `--mode rpc`（双向 JSONL） | `--mode rpc`（跨容器 stdio） | `--mode json`（单向流） |
| 事件时序 | 实时 | 实时 | 实时（命令 stdout `tee` 到 JSONL 后逐 LF 行投递；断流按文件补齐，见 §5.2） |
| 运行时来源 | 宿主安装的 Node v24.16.0 / pi | 镜像自包含（`pi-glink-worker:latest`，node v24.21.0 + pi 0.86.1 + `/opt/pi-packages` 预装 pi 包；镜像名在启动时冻结） | 模板自包含（`pi-glink-worker`，node v24.21.0 + pi 0.86.1 + `/opt/pi-packages` 预装 pi 包） |
| 运行期配置 | `runs/<id>/pi-agent`（本 run 独占） | 同目录挂到 `/config` | 同一目录整棵上传到沙箱 `/home/user/config` |
| 工作区 | `<data>/workspaces/<runId>` | 同左（挂到 `/workspace`） | 沙箱内 `/home/user/workspace`，结束后拉回宿主 |
| 会话文件 | 会话根下的 `<id>/`（宿主可查） | 该目录挂到 `/sessions`；宿主按挂载映射回本 run 目录后脱敏 | 销毁前回收单个有界会话文件到本 run 目录，供下一轮使用；多文件或回收失败不可续接 |
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
  进程/容器/沙箱并报告 `CleanupReport(released, failures)`。清理失败会被记入 run 记录并继续占用容量（§4），不谎报已释放。
* **持久执行身份**：`identity()` 给出容器名或沙箱 id。身份在资源一存在时就写入 `meta.json`：
  Docker 面在 `docker run` 之前的容器名确定后写入，E2B 面在拿到 `sandbox_id` 后、上传配置/续接之前
  写入；创建后的失败清理不会抹掉已拿到的身份，终态 `cleanup.external` 也带上它。它是重启对账（§4）
  与在线自动对账唯一的资源归属凭据，不靠猜名字。

### 5.2 每 run 独立可写环境

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

**多轮续接**：run 的事件日志与终态仍不可变；追问是有 `parent_run_id` 的新 run，不修改上轮。每轮独立配置、工作区和会话目录，复制已脱敏会话（≤32 MiB）与上一轮安全报告 `report.md`（不复制其他中间文件）；仅作上下文的报告若本轮未改动，不冒充本轮新产物，旧报告仍在历史轮次。Docker 用容器内 `/sessions/continued.jsonl`，E2B 先回收并清扫会话文件，下一轮上传副本再续接。历史 E2B 运行未回收的会话不能自动续接。多租户模式下父 run 先过租户门禁（§10.2），否则不读它的任何字段；子 run 继承自己的 owner、凭据与私有工作区。三面 live 续接尚未验收，隔离替身测试不能替代镜像/模板验证。

**事件时序**：E2B 的流式接口是 `commands.run(background=True, …)` 返回的 `CommandHandle`
（`background=True` 的重载不消费 `run()` 自身的回调）。沙箱命令把 pi 的 stdout 同时 `tee` 到
`/home/user/events.jsonl`，宿主在命令运行期间迭代 handle，按 LF 拆出完整行立即经
`parse_json_stream_line` → `on_frame`（沿用 `strip_trailing_cr`；合法 JSON 字符串里的
U+2028/U+2029 不拆帧，与 RPC 面共用 `tests/fixtures/lf-framing.json` 语料）。「已送出」以
**完整行数**计：正常结束时文件与流一致、不重复投递；流中途断开（未收到 end）不算 run 失败，
宿主等命令结束后（命令在管道后把退出码写入 `/home/user/pi-exit-code.txt`）只从文件补发该计数
之后的帧。非零退出码由 SDK `CommandExitException` 映射为 `kernel_exit` + stderr tail，不退化成
`execution_error`。**补读是 fail-closed 的**：流已中断（或本来就没有流式接口）时，文件是唯一完整的
证据，读不到、不存在或超过事件文件上限（`EVENTS_MAX_BYTES` = 32 MiB）都以 `execution_error` 失败，
不得判 `finished`；流自己正常结束并拿到退出标记时，已交付行数与退出标记就是完整性证明，文件读取失败
不改变结论。沙箱文件读取（事件流、stderr 尾部、产物）都先按 SDK 条目元数据或分块读取在**读取前**
限幅，SDK 既不提供大小也不支持分块时拒绝整份载入，不将沙箱内容无界读入宿主内存。宿主侧离线验证已完成
（fake handle 分块推送：行跨块/UTF-8 跨块、断流补齐、取消、非零退出码、命令字符串）；**真实 E2B 沙箱的
实时性尚未验收**。沙箱内的 JSONL 按 LF 分帧；
配置交付带有 `(相对路径, sha256)` 清单，`AGENTS.md`、`settings.json`、`extensions/`、`skills/`、
`prompts/` 与生成文件都按同一路径语义送达。该接口此前的误读与更正过程记录在
`docs/evidence/e2b-run.txt` 末尾。

同一个适配器显式传 `commands.run(..., timeout=<沙箱寿命>)`：SDK 默认值是命令累计运行 60 秒，
而真实研究 run 是分钟级，遗留默认会把内核在沙箱仍存活时杀掉（run 以 `execution_error` 结束于
`TimeoutException`，报告与产物丢失）。命令上限与创建沙箱用的同一个计算值绑定，不会比沙箱寿命更长，
也不会更短。

### 5.3 容器面预装 pi 包

`agent/settings.json` 是唯一的包声明真源（`packages` 里的 `npm:` 声明带精确版本）。三个面在渲染
本 run 的 `pi-agent/settings.json` 时按面向分流：

* **inproc** 保留 `npm:<name>@<pin>`，pi 启动时把它装到本 run 的运行期目录（行为不变，只是不再漂版本）；
* **docker / e2b** 把每个命中预装名单的 `npm:` 声明改写为镜像内**本地包路径**
  `/opt/pi-packages/node_modules/<name>`（pi 官方 local package，"Loaded from the resolved path
  without copying"），因此容器/沙箱 run **不再访问 registry、不再每 run 生成 `npm/`**；
  未列入预装名单的声明原样保留（本仓当前只有 `pi-web-access`；`pi-subagents` 只预装不加载）。

版本一致性由发布物与仓库测试共同约束：两个 Dockerfile 用 `ARG PI_WEB_ACCESS_VERSION` /
`ARG PI_SUBAGENTS_VERSION` 精确 pin 并安装到 `/opt/pi-packages`，
`tests/test_repository_boundaries.py` 卡住两个 Dockerfile、模板 pin 三者一致（与 `PI_VERSION`
同一口径）。预装安装用 `--legacy-peer-deps`，与 pi 自己的 npm 托管安装一致：否则 npm 会自动装入
一份版本可能不同的 `@earendil-works/pi-*` peer，扩展会从副本解析宿主 API 并改变工具的激活方式。

**包缺失必须是明确失败。** pi 对不存在的本地包路径完全静默，所以两个镜像都带
`/opt/pi-packages/check-packages.sh`：Docker 面把它作为容器的 `--entrypoint`（镜像早于宿主时
容器起不来，退出 127；包被删时 exit 78），E2B 面在命令链里先验证守卫存在再交给守卫执行。崩溃
语义与其它内核启动失败一致：Docker 面为 `kernel_exit`（退出码 78），E2B 面为 `kernel_exit` +
stderr tail 里的中文原因。构建、候选 tag、回滚句柄、实测耗时与研究记录的实测事实见
`docs/evidence/prebaked-packages.txt` 与 `docs/pi-integration-notes.md` §9。

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
| 报告文件 | 经工作区目录句柄读取、脱敏、在 run 目录句柄内原子写入 `runs/<id>/artifacts/report.md`，并记 `redacted=true` | `kernel/runner.py::_recover_report` |
| 会话文件 | pi 自写的 JSONL 在收尾时脱敏；只允许本次 run 会话子目录内的普通文件（拒绝链接/越界），Docker 面先把容器路径 `/sessions/…` 映射回该挂载源 | `kernel/runner.py::_sanitize_session_file`、`docker_env.host_session_path` |
| 报告下载 | `report_path` 不是下载能力：只认两个固定位置——宿主产物 `runs/<id>/artifacts/report.md`，以及旧版本写下的本 run 工作区 `workspaces/<id>/report.md`（只读，从不改写历史）；其他路径一律视为无报告。服务前再脱敏，响应头 `X-Redacted` 标明是否发生；`hasReport` 用同一位置规则与同一拒绝条件计算 | `store.py::report_location`、`web_routes.py::_send_report`、`artifacts.py` |

**产物路径安全**（`artifacts.py::ArtifactTree`）：宿主从不「先 realpath 校验、再按路径打开」。
每次操作持有 run 根目录的目录句柄（根的父目录是宿主配置，可信；根本身及其下每一级都以
`O_NOFOLLOW` 逐级打开），叶子只接受普通文件（FIFO 以非阻塞方式打开后拒绝），打开前后身份须
一致，打开后再从根重新核对每一级目录项；写入在已核对的父目录句柄内建临时文件并 `renameat`
发布。任一祖先在校验与打开之间被换成链接，读写都会被拒绝，外部文件既不被读取也不被改写。

检测同时覆盖**值**（宿主知道的 key）与**形状**（`sk-…`、`e2b_…`、`exa-…`、`NAME=value`、
`Bearer …`），所以宿主从未见过的第三方 token 也会被掉包；跨 delta 的分割由 §3.3 的暂存覆盖。
`scripts/check-no-secrets.py` 用同一套规则扫描运行态产物，并把**文档占位符**
（如 README 里的 `sk-ant-...`）与真实值分开计数。

**脱敏不是安全沙箱**：它处理的是「宿主已能把值交付给内核、内核又把它回显到产品面」这一层的
回显保护；任意编码、分拆到不同工具输出、写入二进制或直接网络外传不在承诺范围。需要更强隔离
时应使用 Docker / E2B 执行面，并为内核配专用、限额的凭据；多租户的每 run 凭据渲染与脱敏边界
见 §10.3，配置与迁移见 `docs/multitenancy-plan.md`。

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
| `host/pi_glink/` | 宿主 | `config` / `events` / `store` / `tenancy` / `ownership` / `static_files`；组成根 `app`（服务器与 `create_server`） |
| `host/pi_glink/web_*.py` | 宿主 HTTP 层 | `web_routes`（路由与 run/health/报告 API）/ `web_sse`（事件流与连接额度）/ `web_security`（身份、会话与请求防护）/ `web_http`（请求体、响应、静态资源、脱敏访问日志）；一个 `HostRequestHandler` 由它们组合而成 |
| `host/pi_glink/cli.py` | 宿主 CLI | 参数与 `main`、离线运维子命令（`--unreleased` / `--mark-released` / `--create-invite`） |
| `host/pi_glink/kernel/` | 接入层 | `rpc` / `framing` / `projection` / `agent_env` / `execution` / `docker_env` / `e2b_env` / `runner` |
| `host/pi_glink/artifacts.py` | 宿主 | 内核可达文件的目录句柄锚定读写（逐级不跟随链接、打开后复核、限幅、目录内原子替换） |
| `agent/` | 内核配置模板 | `settings.json`、`AGENTS.md`、`research-prompt.md`（通用工作约定）、`prompts/deepresearch.md`、扩展位 |
| `web/` | 前端 | 外壳与事件视图（无打包步骤，原生 ESM）；视觉按百鉴 UI 标准库逐值对齐，见 `docs/ui-alignment.md` |
| `docker/` | 执行面 | `Dockerfile.worker`（容器）、`e2b/Dockerfile.e2b`（模板） |
| `scripts/` | 运维 | 启动、测试、真实 run 冒烟、E2B 模板构建 |
| `docs/` | 文档与证据 | 架构、边界、集成笔记、`evidence/` |
| `tests/` | 回归 | `test_*.py`（宿主/内核）、`web/*.test.mjs`（前端模型）、`browser/`（真实浏览器） |
| `<data_root>`（默认 `~/.pi-glink`） | 运行态 | `host.lock`（宿主独占所有权）、`runs/<id>/{meta.json,events.jsonl,pi-agent/,artifacts/}`、`sessions/<id>/`、`workspaces/<id>/` |

运行期配置目录 `runs/<id>/pi-agent/` 是 pi 的 `PI_CODING_AGENT_DIR`：宿主在其中渲染
`models.json` 与 `web-search.json`，复制 `agent/` 的可版本化配置，pi 自己往里写 `auth.json` /
`models-store.json`。仓库里的 `agent/` 只是模板，永远不作为运行目录使用；`pi-agent/` 不共享，
所以并发 run 不会互写凭据或扩展。

宿主 HTTP 层的职责边界：路由只决定请求含义（`web_routes`），身份与请求防护集中在
`web_security`，事件流与连接额度在 `web_sse`，字节读写与静态资源在 `web_http`；`app.py` 只做
组合与生命周期（数据目录锁、租户材料、run 对账、绑定端口、资源台账扫描线程），CLI 入口与
离线运维子命令在 `cli.py`。拆分是纯结构调整：路由、状态码、载荷与既有导入路径（含
`pi_glink.app.main` / `load_config` / `_resource_command` 再导出）保持不变。

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
多角色编排、上下文压缩调优、成本治理、E2B 模板的生产级硬化。单宿主多租户已按 §10 实现（合成验证）；
多宿主、共享存储、真实网关接入、BYOK 自助与短期凭据代理、计费、上线部署与真实数据迁移仍不在
本期范围，具体接口、迁移与上线验收见 [多租户方案](multitenancy-plan.md)。

## 10. 单宿主多租户（已实现，合成验证）

默认 `PI_GLINK_TENANCY=local`；宿主任一模式都只绑定 loopback（见 §10.5 的行为迁移）。开启
`multitenant` 后身份入口由 `PI_GLINK_AUTH_MODE` 选择：`gateway`（默认）部署在本机认证网关之后，
由网关注入身份断言；`accounts`（§10.6）在宿主内完成邀请注册、登录与服务端会话。模型凭据来源另由
`PI_GLINK_MODEL_CREDENTIALS` 显式选择：`tenant`（默认，每租户自己的 key 文件）或 `shared`
（SaaS 策略，宿主冻结一套模型凭据供所有租户使用，见 §10.3）。`local` 模式没有身份入口，行为完全不变。

### 10.1 身份与请求授权

两种身份入口（gateway / accounts）都只从服务端事实推导 `TenantContext`，下游的
owner 校验、配额、凭据与脱敏边界完全共用；客户端在任一模式下都不能用请求体、查询参数或自造
请求头指定租户。

- 断言：`X-Pi-Glink-Identity: v1.<base64url(payload)>.<base64url(hmac-sha256)>`，payload 至少含
  `tenantId`、`userId`、`role`、`audience`、`iat`、`exp`；宿主用仓库外 0600 密钥文件做 constant-time
  HMAC 校验，并检查 audience、过期与寿命上限（默认 300 秒）。重复断言头直接拒绝——这是宿主侧的
  “网关必须清除客户端同名头”。
- 请求体、查询参数与其它请求头不参与身份推导：多租户下除 `/api/health` 外所有 `/api/*` 先验签；
  未认证 401，权限不足 403，未知/异租户/无 owner 的 legacy 记录统一同形 404。
- 事件流在写出任何响应头之前用同一 owner 规则验权；断言过期在**重放与实时两个阶段**都按帧检查：
  持久 `events.jsonl` 的每一帧、实时订阅的每次等待都先确认断言未过期，过期即干净结束该流（实时
  等待用 `EventBus.subscribe(idle_poll=…)` 返回控制流，不改变持久事件语义）。过期没有宽限窗口：
  通过验证的上下文绝不会已经过期，`clock_skew` 只容忍签发时间略快于宿主时钟的网关。
- POST 检查 `Origin` / `Referer` / `Sec-Fetch-Site`：任何表示跨站的元数据都会在启动执行面之前被拒绝；
  无这些头的非浏览器客户端（网关、运维脚本）不受影响。

### 10.2 owner 与历史数据

`RunStore.create` 在受理时用同一次 `meta.json` 原子写 `tenant_id` / `creator_user_id`；`get_for` /
`list_for` 按 tenant 过滤。没有 owner 的旧记录不匹配任何租户，因此在多租户读面上始终不可见/不可下载/
不可 SSE/不可取消，也不会被迁移或改写；`local` 模式仍按旧口径可读。取消在同一把 run 锁内先做
owner/role 判定再受理：其他租户得到与未知 run 相同的 404 且无副作用，同租户非创建者的 member 得到 403。

多轮追问遵循同一规则：`parentRunId` 的父 run 必须先通过租户门禁，未知/异租户/无 owner 的 parent 统一
返回 `unknown_run`（404），且在所有者得到确认之前不读取它的状态、会话文件、报告或子 run 计数；
先读后判会让“运行中/已有后续轮次”的父 run 以 `run_busy`/`stale_run` 泄露存在性。同租户父 run 的
`session_unavailable`、`stale_run`、`report_unavailable` 等具体拒绝保持可解释；子 run 始终继承调用者
自己的 owner、凭据与私有会话/工作区，复制的是已脱敏的会话副本与上一轮报告。

### 10.3 凭据与脱敏边界

租户凭据文件（仓库外、0600）在默认 `PI_GLINK_MODEL_CREDENTIALS=tenant` 策略下为每个租户提供
`modelApiKey`（必需）与 `webSearchKey`（可选）。渲染时 `RunCredentials(origin="tenant")` 只允许本租户
的值：缺模型 key 拒绝准入，绝不回退宿主 `.env`、操作员 `~/.pi/web-search.json` 或别的租户；缺搜索 key
时按现有无 key 检索行为渲染（删除 `web-search.json`）。E2B 平台 key 属于宿主资源管理，不是租户模型 key。
每 run 构造一个只包含“宿主已知秘密 + 本 run 凭据”的 `SecretRedactor`，事件投影、提交、报告拷贝、
工作区/会话清扫与报告下载都用它，因此不同租户的秘密不会互相写入对方的边界或产物。工具能读到自己
本 run 的 `auth.json` 仍属于既有残留风险（§6.2），脱敏只做回显保护，不承诺阻止任意编码或网络外传。

显式 `PI_GLINK_MODEL_CREDENTIALS=shared` 是 SaaS 部署的**显式放宽**，不是任何模式的默认兜底：

* 宿主必须在启动时冻结 `OPENAI_API_KEY`、`OPENAI_BASE_URL`、`OPENAI_MODEL`，任一缺失即拒绝启动；
* 每个 run 渲染 `RunCredentials(origin="shared")`，即同一套宿主共享凭据；共享模式不读取任何
  租户配置：不得配置 `PI_GLINK_TENANT_CONFIG_FILE`（不设置或留空即为无该入口），设置即拒绝
  启动，也不会构造或读取 `TenantCredentialStore`；缺共享 key 时也不会回退到 `tenant` 或 local；
* 可选搜索 key 只来自宿主级 `PI_GLINK_WEB_SEARCH_KEY`，不可能用操作员 `~/.pi/web-search.json`
  顶替；
* 租户授权仍完全由身份入口决定（网关注入的断言或账号库会话）；共享模式没有租户凭据文件入口，
  也不存在一个可以准入或拒绝租户的文件名单；
* 每 run 脱敏边界仍包含共享 key（它属于宿主已知秘密）；`redactor_for_record` 在 shared 下使用
  宿主/共享边界，不会因为共享模式没有租户凭据条目而漏脱敏，也不会拿其他租户的凭据去清洗共享内容；
* `tenant` 与 `shared` 的差异只限于“凭据从哪里来”：owner 过滤、每租户配额、审计可见性与
  `tenant` 模式完全一致；run 详情/列表/事件/报告从不回显模型凭据。

### 10.4 准入与并发

只有多租户 + Docker/E2B 可并发：默认全局上限 4、单租户上限 2；准入检查与槽位预留（写入执行表 +
打开事件流）在同一把 manager 锁内完成，超额立即返回 `429 quota_exceeded`（带 scope 与 limit，不含
任何 run id），不排队。未确认释放的资源持有**计入槽位**（owner 租户 + 全局；无 owner 的 legacy 与
损坏记录只计全局），因此一个租户的泄漏不会挡住别的租户，但会持续吃掉额度；持有达到全局上限时任何
新准入都被拒绝（fail-closed）。恢复额度靠宿主在线自动对账或离线 `--unreleased` / `--mark-released`
（§4）。已受理的取消仍与终帧提交在同一线性化点上裁决。`inproc` 在两种模式下都维持全局单 run；
多租户配置直接拒绝 `inproc`（它共享宿主文件系统）。

SSE 连接同样有界：全局 `PI_GLINK_MAX_GLOBAL_STREAMS`（默认 64）与每租户
`PI_GLINK_MAX_TENANT_STREAMS`（默认 16），超额在写出流响应头之前返回 `429 stream_limit`（§3.2）。

Docker 面的每个容器实例额外带 `--cpus` / `--memory` / `--pids-limit`（默认 2 / 2g / 512，可配置）与
`--cap-drop ALL` / `--security-opt no-new-privileges`，镜像与模板不变。E2B 面只能限制沙箱生命周期
（`LOCAL_WEB_E2B_TIMEOUT_SECONDS`，默认 task timeout + 300 秒）；SDK 不暴露 CPU/内存控制，因此宿主
只声明自己实际传给云端的限制，不把未请求的限制当作已执行。两者都未在本次交付中做 live 验证。

### 10.5 行为迁移、回退与未覆盖范围

- **迁移**：任何模式都不再允许绑定非 loopback：无认证公网模式被移除，历史的 `LOCAL_WEB_ALLOW_REMOTE`
  不再生效；远端访问必须经过本机认证网关注入身份（`PI_GLINK_TENANCY=multitenant`）。这是本次交付
  明确的行为收紧。
- **回退**：关闭 `PI_GLINK_TENANCY` 回到单用户模式；新旧记录都保留，不需要删除、迁移或改写；
  多租户密钥文件也不再被读取。
- **合并窗口兼容（前端）**：静态模块由宿主从 `web/` 逐次读盘，所以先合并前端、后重启宿主时，旧
  `local` 宿主会立刻执行新前端，而它没有 `GET /api/me`。前端因此只在**同时满足**「`/api/me` 明确
  404」与「随后 `GET /api/config` 成功且 `multiTenant === false`（严格布尔）」时按旧 local 流程
  初始化；401、网络错误、multitenant/gateway/accounts、缺失或非布尔的配置值一律 fail-closed
  （显示不可达/登录面），本地工作台绝不会在身份不明时被揭示，也不存在借它绕过账号身份的路径。
  旧 local 历史与报告照常显示。宿主重启后该分支自然失效，属于临时兼容，可在后续清理。
- **未覆盖**：真实网关注入、密钥轮换与撤销；真实 Docker/E2B live 资源上限、沙箱模板的实际隔离强度；
  多宿主租约与共享存储；BYOK、短期凭据代理、计费与上线运维。合成矩阵通过不等于多租户已经安全上线。

### 10.6 站内账号身份入口（`PI_GLINK_AUTH_MODE=accounts`）

与网关模式并存但互斥：多租户部署显式选择 `gateway`（默认，语义不变）或 `accounts`。账号模式在
宿主内完成邀请注册与登录，仍只监听 loopback（生产由 TLS 终止代理提供 HTTPS），并且**只在服务端**
生成 `TenantContext`：

- **存储**：`<data_root>/accounts/accounts.db`（SQLite，stdlib；WAL、`BEGIN IMMEDIATE` 事务、单连接加锁），
  表只有 `tenants` / `users` / `invitations` / `sessions`。账号数据与 run/events 文件存储分离，互不
  迁移、互不共享；历史 run 不因账号模式而改变归属。邮箱全局唯一（canonical 唯一索引），一个用户
  只属于一个租户，角色为 `member` / `admin`。文件必须在仓库外，并且**必须位于本 data_root 之内**
  （`realpath` 判定，符号链接不能指向共享库）：单宿主独占性是 `data_root` 上的锁，库在别处会让两个
  各自持有不同锁的宿主写同一份账号数据。
- **权限**：账号库在首次创建前先建立 0700 的专用目录并以 `O_CREAT|O_EXCL|O_NOFOLLOW`、0600 创建
  库文件，SQLite 才被允许打开它；WAL/SHM/journal 侧车必须是属主独占（宽权限的侧车会被收紧，因为
  它是宿主自己的库的派生文件）。已有库若是符号链接、非普通文件、非属主所有或带组/其他人权限位，
  一律 fail-closed（在读取与迁移之前拒绝启动，文件原样保留，不自动 chmod）；操作员需停止宿主后
  把库移入 0700 目录并 `chmod 600`，无法确认来源时先备份。
- **初始化顺序**：账号库的创建、建表迁移与 WAL 切换都是 data-root 写入，因此
  `create_server` 与 `--create-invite` 一律**先取得 `host.lock`，再打开账号库，最后才绑定端口**；
  第二宿主在打开库之前就被 `DataRootBusy` 拒绝，`accounts.db` 及其 `-wal`/`-shm` 字节不会被改写。
  `--print-config` 只做材料校验，不打开、不创建账号库。
- **口令与会话**：口令用 `hashlib.scrypt`（随机盐、逐行记录 `n/r/p/dklen`、`hmac.compare_digest`
  校验）；未知账号与错误密码执行同量的散列工作并返回同一个 401。登录成功后签发高熵会话令牌，
  数据库只存摘要；浏览器只持 `HttpOnly` + `SameSite=Lax`（HTTPS 下 `Secure`）cookie。会话同时有绝对
  有效期与空闲有效期，登录/注册/邀请校验都有固定窗口限速。登录有两层限速：**先**按 socket peer
  （`client_address`，绝不读 `X-Forwarded-For`）计一个全局预算，**再**按规范化邮箱计账号预算；前者
  保证轮换邮箱也不能无限触发口令散列（拒绝发生在 `scrypt` 之前）。经 TLS 反代时所有用户共享代理
  这一个 peer，该预算实际是全部署的登录上限（默认 120 次/60 秒，可配置）——这是为了保护单机 CPU
  的可用性取舍，不表示能识别真实用户来源。
- **失效**：每个受权请求都从服务端记录重新解析用户与租户状态，停用用户/租户、注销、过期立即生效。
  SSE 在重放与实时两个阶段都按轮询检查会话（撤销/停用/空闲/绝对过期），失效即干净结束该流，
  但不会取消正在运行的 run：取消仍按原归属规则单独受理。
- **邀请**：邀请码只存摘要、短期一次性、绑定租户+邮箱+角色；它只出现在**浏览器 URL fragment**、
  一次性下发链接与 JSON body 里，永远不进查询串、请求行、Referer 或日志（日志里的请求行查询串被
  统一剥掉；`/api/invitations/check` 只接受 POST JSON，GET/查询形式返回 405）。注册只创建账号，
  不创建租户、不授予模型凭据，也不静默下发会话（注册成功后回到登录）。首位管理员由操作员在宿主停止时以
  `python -m pi_glink.app --create-invite …` 离线签发（默认策略要求该租户已有模型凭据；`shared`
  策略只校验宿主冻结的共享模型配置，不配置租户凭据文件）；后续管理员通过
  `POST /api/admin/invitations` 只给本租户签发，邀请码原文只在成功响应里出现一次。
- **请求防护**：状态变更 POST 要求 `application/json`、同源 `Origin`/`Referer`/`Sec-Fetch-Site`；
  已登录会话还必须携带与 cookie 绑定的 `X-Pi-Glink-CSRF`（`GET /api/me` 下发，只存在页面内存）。
  `gateway` 模式忽略会话 cookie，`accounts` 模式忽略断言头，登录失败不会回退到 gateway/local；
  缺本租户模型 key 仍拒绝启动 run（§10.3）。
- **未验证**：真实 HTTPS/TLS 代理与外部 Origin、邮件交付、密码重置、账号/租户停用运维入口、生产
  数据迁移与多宿主。合成矩阵（临时账号库、随机 loopback 端口、fake 执行面、真实浏览器手动验收）
  不等于账号模式已通过生产安全验收。
