# 架构

本仓库把 pi（`@earendil-works/pi-coding-agent`）等原生 harness 当作独立进程接入。
本文先说明已确认的长期目标，再描述当前宿主的进程拓扑、事件契约、执行面、凭据流与目录边界。
目标尚未实施；当前实现与历史实测的区别见各节及 `docs/pi-integration-notes.md`、`docs/evidence/`。

## 产品方向与当前实现

### 长期目标（已确认，待实施）

**ULegal 是产品与业务的唯一后端，glink 收敛为原生 harness 的薄执行适配层。**
这一决定替代继续扩建 glink 独立多租户研究工作台的方向；多租户产品能力仍需实现，但归 ULegal，
不再在两个仓库各建一套账号、业务状态和历史管理。职责划分以本节为准；适配层的迁移顺序见
[多 Harness 计划 §6](multi-harness-plan.md#6-里程碑)。ULegal 侧的承接约定与待实施事项记录在
其 `docs/architecture.md` 和 `docs/open-items.md`，不依赖本机仓库路径。

| 层 | 目标职责 | 不承担的职责 |
|---|---|---|
| 原生 pi / dsh 等 | Agent loop、工具、上下文管理、原生会话与各自的扩展、子代理机制 | 产品账号、项目权限、订阅和业务成果状态 |
| glink 执行适配层 | E2B 承载、RPC/ACP 通信、执行输入与配置交付、协议事实归一化、原生会话回收与恢复、取消、产物回收、资源身份与清理结果回报 | 独立产品前端、登录与邀请、业务历史、订阅计费、第二套权威执行数据库 |
| ULegal 后端 | 身份与项目/来源授权、业务会话、执行准入、事件与状态持久化、资源台账和重启对账、订阅权益、原始用量归属、成果验证与保存、产品 HTTP 接口 | 重写 harness 的 Agent loop，直接读取其私有格式来推断权威状态 |

目标调用方向为 `ULegal 前端 → ULegal 后端及 PostgreSQL → glink 执行适配层 → E2B 中的原生 harness`。
适配层只接收服务端冻结的执行输入并回报事实，不直接操作 ULegal 的业务数据库；接口承载方式在
实施设计中确定，不因保留内部执行接口而恢复独立产品后端。内核执行结束、业务成果验证完成和
资源释放是不同事实，由 ULegal 在同一执行的权威记录中分别表达，不能互相代替。

首发保持 **shared SaaS**：运营方提供模型凭据，产品按订阅收费；BYOK 是后续可选能力，不作为
首发前提。凭据与额度政策由产品后端负责，适配层按每次执行的最小权限交付；模型或检索 key 不进入
镜像、仓库、浏览器和业务记录。实际模型与推理档位须经协议核验，不能把配置文本当成生效证明。

保留原生 harness 不等于直接暴露裸 CLI：不 fork、patch 或 vendor 内核；pi 的 packages/extensions
与 dsh 的 profiles/bundles 等机制继续由各包承载，不为了统一协议而抹平。研究工作约定与蓝图作为
应用选择的执行输入，产品特有的报告格式由应用定义；已确认的“引用来源前打开原文核实”、外部
内容是数据、来源授权、凭据保护和可靠清理继续成立。

### 迁移约束

- **先由 ULegal 接管职责，再退出 SQLite。** 当前 `runs/runs.db` 仍是执行记录、事件重放、终态原子提交和
  未释放资源台账的事实源。只有 ULegal 可靠接管这些职责并通过失败场景验收，才可停止新执行的
  SQLite 写入并退出旧宿主；不以内存状态、终端文本或遥测替代持久化。
- PostgreSQL 接管结构化执行记录不等于取消原生会话文件。会话、状态归档和产物仍须由产品拥有
  明确的私有存储与访问边界；不能把原件存储目录或整个项目目录直接挂给 harness。
- 一个执行只有一个权威状态拥有者。迁移期间按明确的执行路径与切换点区分 owner，不把两套库的
  终态互相覆盖；持久化失败、断流、重复投递和迟到结果不能制造已完成或已释放的假象。
- 保留已有 Docker 与本地调试路径，当前契约不因目标更新自动变化；服务及真实 harness 验收使用
  E2B，并单独取得模型、云端和模板操作授权。
- 不自动迁移账号或按邮箱回填历史归属，不删除旧数据库、会话或报告。历史读取、真实数据迁移与
  回退窗口需另行决定；回退新请求路径时仍须跟踪已受理执行和未释放资源。

### 当前实现（迁移前）

当前宿主仍提供研究工作台、HTTP/SSE、站内账号和 SQLite 存储，通过 Docker / E2B 为 run 提供
独立执行环境，本地 `inproc` 保留为单 Agent 开发与调试入口。默认 `GLINK_TENANCY=local`
仍是单用户、全局单 run；`GLINK_TENANCY=multitenant` 时启用单宿主多租户实现（§10）。
身份入口为本机认证网关注入短时效签名断言（`GLINK_AUTH_MODE=gateway`，默认），或站内邀请
注册、登录与服务端会话（`GLINK_AUTH_MODE=accounts`，§10.6）；两者只在服务端生成
`TenantContext`，run 带租户归属。当前配置默认 `tenant` 政策，每租户自备模型凭据；显式
`GLINK_MODEL_CREDENTIALS=shared` 时由宿主统一提供，这是首发产品选择，不表示本次已修改
运行配置。ACP 注入通道服从同一政策：tenant 下拒绝、shared 下为具名凭据源
（[ADR 0015](adr/0015-acp-credential-channels-under-model-credential-policy.md)）。
Docker/E2B 可并发；多租户实现目前只有合成验证，真实网关、HTTPS 终止、邮件交付和生产数据验收
未覆盖，不能由单 run smoke 或已有执行面实测替代。

多租户实现已覆盖服务端可信身份、run/事件/报告/取消的归属过滤、每 run 凭据交付与脱敏边界、显式并发准入与 fail-closed 资源台账（未确认释放的持有计入 owner 租户与全局槽位，可由在线自动对账或离线人工裁决释放）；仍未覆盖多宿主租约、共享存储、真实网关与云环境的资源上限执行。单独容器/沙箱提供执行隔离，但不自动解决宿主 API 授权、存储隔离或凭据访问，也不构成本仓库对容器/沙箱实际隔离强度的保证。

**旧数据不可读**：本次架构重设计（2026-09-28）起不再为旧 run 记录保留兼容分支。run 记录、事件与资源台账迁入 `runs/runs.db`（SQLite，[ADR 0009](adr/0009-sqlite-run-store.md)）后，此前的 `runs/<id>/meta.json` / `events.jsonl` 文件与 `<数据根>/sessions/<id>/` 会话目录**一律不再被读取**：旧 run 不出现在列表、详情、重放与资源台账中，也不能被追问；`runs.db` 的 schema 版本不符时宿主拒绝打开。不提供迁移，需要旧历史的操作员自行保留旧数据根。决策史统一记在 [`docs/adr/`](adr/)。

具体接口与数据迁移在对应实施设计中决定，不预建无消费者的租户抽象；修改当前承诺遵循
[工程约束](engineering.md)。现有测试继续保护迁移前行为，取得行为替代决定后才同步修改实现、
消费者与断言；目标变更不等于可以提前删除安全和持久化门禁。

当前多 harness 接入已落地 pi 与 ACP 两个族：pi 三个执行面均使用 `--mode rpc`，ACP 族的
dsh/codex/opencode 经 stdio JSON-RPC 桥接。开源 harness 名单与包机制继续保留，接入事实见
[多 Harness 方案](multi-harness-plan.md) 与 [ACP 集成笔记](acp-integration-notes.md)。
下文 §1 至 §10 描述迁移前实现，不代表 ULegal 已经接管。

## 1. 分层结构

| 层 | 位置 | 职责 | 不做什么 |
|---|---|---|---|
| 前端 | `web/` | 工作台外壳、运行时间线、报告预览 | 不解析终端文本，不做业务编排 |
| 宿主 | `host/glink_harness/` | 配置冻结、事件总线、HTTP/SSE、存储 | 不导入 pi，不实现研究方法论 |
| 编排层 | `host/glink_harness/runs/` | run 准入、执行骨架与收尾顺序、终态提交、外部资源台账 | 不解析内核协议帧，不定制内核 |
| 内核接入 | `host/glink_harness/kernel/` | 启动 pi、消费官方 headless 协议、事件归一化 | 不导入编排层或宿主编排模块，不定制内核 |
| Harness 包 | `harnesses/<pack>/` | 各 harness 的可版本化配置与扩展（清单 `harness.toml`、pi 的 settings/扩展、ACP 的 home 覆盖层） | 不放密钥，不放宿主实现 |
| 方法论 | `methodology/` | 研究工作约定与会话蓝图，由宿主拼入系统提示 | 不放 harness 专有配置 |

依赖方向单向：`前端 → 宿主 HTTP → 编排层 → kernel → pi 进程`；`harnesses/<pack>/` 与 `methodology/` 只作为只读模板被复制或拼入每 run 的运行期配置。
`host/glink_harness/kernel/` 不得反向导入 `runs/`、`tenancy`、`ownership`、`blueprints`、
`web_*`、`app`、`cli`；`store` 只在族注入接线点 `harness.py`（`select_family` 把 RunStore
包成适配器注入）与窄协议适配器 `run_views.py` 中导入——pi 与 ACP 两族均已经
SessionSource 协议访问记录与会话来源，不直接持有具体 store；
由 `tests/test_repository_boundaries.py` 的导入方向规则卡住。
内核包（node_modules 里的 pi）始终是外部依赖，仓库不持有它的源码。

## 2. 进程拓扑

```
浏览器 ──HTTP/SSE──► host（Python，单进程）
                       │
                       ├─ DataRootOwnership（data_root/host.lock 独占；先证明所有权再对账）
                       ├─ EventBus（每 run 单调 seq；先落盘再发布；有界实时队列）
                       ├─ RunStore（runs/runs.db：run 记录 + 事件，SQLite WAL；artifacts 等仍是文件；终帧即终态提交）
                       └─ RunManager（`runs/`：准入 / 执行 / 收尾 / 终态提交）
                          └─ 准入：RunSpec{family, pack, executor, blueprint, credential_origin}
                             随 RunRecord 在同一事务内写入 runs 行的 "spec"
                              └─ adapter = harness.driver(sandbox.spawn(harness.launch(spec)))
                                  ├─ Harness（what）：pi 族 stdio RPC / ACP 族 stdio JSON-RPC
                                  └─ Sandbox（where）：inproc 子进程 / docker run -i /
                                                      e2b 后台命令 stdin=True + send_stdin
```

**RunSpec（准入冻结点）**：`store.RunSpec` 是一个 run「用什么执行」的全部事实——harness 族
（`family`：`pi`/`acp`）、harness 包（`pack`：`harnesses/<pack>/`）、执行面（`executor`：
`inproc`/`docker`/`e2b`）、会话蓝图名（`blueprint`，无蓝图为 `null`）与凭据来源
（`credential_origin`：`host`/`tenant`/`shared`，只记来源，不含凭据值）。RunManager 在准入时
由冻结配置解析一次（`RunAdmission.spec`），与 run id、owner 在同一个数据库事务内写入 run 记录的 `"spec"`
对象；此后族、执行面、追问门禁与 ACP 状态根继承一律读记录里的 spec，不读宿主当前配置。
`family`/`pack`/`executor`/`credential_origin` 是必填非空字段；缺 `"spec"` 或缺字段的行按不可解析
处理，读取即 fail-closed（见 §4「准入与资源」的不可解析行）。
**旧数据不再可读**：没有 `"spec"` 的记录不被解析，也没有迁移（[ADR 0010](adr/0010-harness-sandbox-runspec.md)）。

两个 harness 族 × 三种执行面共享同一套输入契约与同一套产品事件，因此浏览器、存储与报告下载
路径完全一致。两个维度正交，各自只有一处实现：

* **Sandbox**（`kernel/sandbox.py`：`InprocSandbox` / `DockerSandbox` / `E2BSandbox`，由
  `open_sandbox(spec.executor, ...)` 选出）只负责资源与传输——创建/销毁资源、把 `Launch` 变成
  该载体上的 `Spawn`（本机子进程、`docker run -i` argv、沙箱后台命令）、路径放置与映射
  （`place` / `host_path`）、销毁前回收工作区、持久执行身份（`planned_identity` / `identity`）
  与 `close()` 的 `CleanupReport`。它不知道自己承载哪个 harness。
* **Harness**（`kernel/harness.py` 的 `HarnessFamily`，实现为 `harness_pi.py` /
  `harness_acp.py`）只负责 launch 描述（argv、环境、挂载、包守卫、镜像/模板名，路径一律经
  `sandbox.place` 得到）、协议驱动（pi：`execution.PiExecutionAdapter` + `rpc.py` /
  `projection.py`；ACP：`acp_execution.AcpExecutionAdapter` + `acp_rpc.py` / `acp_projection.py`）、
  终态词汇、续接、子代理增强与 usage。远程面的文件交付由族的 driver 经传输器完成
  （pi：`e2b_env.PiSandboxTransfer`；ACP：`acp_e2b_env.AcpSandboxTransfer`），只问 sandbox 的
  `remote` / `handle()` 事实，不按执行面名分支。

组合只有一条路径：`HarnessFamily.build_adapter(spec, sandbox, ...)` 即
`driver(sandbox.spawn(launch(spec)))`，不按执行面名字符串分派（[ADR 0010](adr/0010-harness-sandbox-runspec.md)）。`runs/` 编排层（RunManager 与生命周期管线）只依赖抽象
（`HarnessFamily` / `HarnessRuntime` / `FrameProjector` / `Sandbox` / `ExecutionAdapter`），不导入
任何具体族、执行面或 runtime 类型，也不承担任何单面的收尾逻辑（§5.1）；该规则由
`tests/test_repository_boundaries.py` 的编排层导入白名单卡住。
ACP 族的协议事实、能力矩阵与冒烟证据见 [ACP 集成笔记](acp-integration-notes.md) 与
[多 harness 方案](multi-harness-plan.md)。

## 3. 事件契约

> 面向产品后端的服务契约（受理字段与拒绝映射、SSE 续传与 429、终帧集合与消费纪律、
> report/deliverables 端点、取消两态）固化在 [exec-contract.md](exec-contract.md)，与
> `tests/test_exec_contract.py` 逐条对应；本节与 §4 描述其实现语义。

pi 的事件词汇只出现在 `kernel/projection.py`；下游（总线、SSE、前端、事件日志）只说产品
事件（`host/glink_harness/events.py`）。

| 产品事件 | 来源（pi） | 前端用途 |
|---|---|---|
| `run_started` / `run_stage` | 宿主 / `agent_start`、`compaction_*`、`auto_retry_*` | 状态条、阶段行 |
| `session_ready` | `get_state`（RPC 面）/ 沙箱元数据 | session id、模型、thinking 档位 |
| `turn_started` / `turn_finished` | `turn_start` / `turn_end` | 轮次分隔 |
| `thinking_delta` | `message_update.thinking_delta` | 思考折叠块（流式） |
| `assistant_delta` | `message_update.text_delta` | 正文流 |
| `assistant_message` | `message_end`（权威全文） | 收口后的 Markdown |
| `tool_started` / `tool_updated` / `tool_finished` | `tool_execution_*` | 工具卡片（含流式输出/错误态） |
| `deliverables_declared` | ACP `present` 的 `tool_call.rawInput.files`（pi 族无源） | 未封存的文件声明卡片，终帧/详情再裁决 |
| `subagent_started` / `subagent_finished` | `tool_execution_*`（工具名 `subagent`） | 子代理（ChildRun）的类型化生命周期事件，替代该 toolCallId 的通用工具卡片（§3.5） |
| `usage_reported` | `turn_end.message.usage` | token 统计（`usage` 为 run 累计，`requestUsage` 为本次请求快照） |
| `kernel_status` | `extension_error` 等 | 警告提示 |
| `run_finished` / `run_failed` / `run_cancelled` | 宿主 | 终态、报告入口 |

传输细节：

* SSE 帧形如 `id: <seq>` / `event: <type>` / `data: {…}`，与既有工作台前端一致；`data`
  与事件日志的一行是同一个信封 `{type, v, runId, seq, ts, payload}`（`v` 是该类型的
  payload 版本，§3.6）；
* `seq` 每 run 单调递增；重连时浏览器发 `Last-Event-ID`，服务端重放之后的帧；
* 前端模型按 `seq` 水位去重（重复帧丢弃、缺口计数可见），因此**迟到/重复事件不会破坏状态**；
* 载荷有界：工具结果与文本按上限裁剪并显式标注 `truncated`；工具参数里的数组
  （`queries`/`urls` 等）按「字符串保持字符串、数组保持**有界**字符串数组」的契约投影，
  不压成单串，前端 `collectSources` 因此能拿到多查询/多 URL 语义。数组里的**对象**元素
  （`subagent` 的 `tasks`）投影为该项的紧凑 JSON 文本，同样是数组中的一个有界字符串：
  契约形状不变，卡片能显示派了哪些 agent、各自做什么。

### 3.1 提交先于发布

`EventBus.commit` 是唯一写入路径：分配 `seq` → 调用持久化回调把帧插入 `events` 表 →
才写入内存环形窗口并通知实时订阅者。同一 run 的提交由每 run 的提交锁串行；持久化回调
（可能等待磁盘同步）不持有总线全局锁，所以 `is_open`/`last_seq` 与新订阅永远不等磁盘，
且只反映持久化已经返回的帧。因此：

* **已提交的持久事件是重放事实源**。写库失败的那一帧在浏览器、SSE 与日志里都不存在，
  并且不消耗 `seq`（不会制造缺口）；运行会以 `event_log_failed` 诚实失败，而不是让
  浏览器看到一条日志里没有的帧。
* **存储是一个 SQLite 数据库**（`store.py`，标准库 `sqlite3`）：`<data_root>/runs/runs.db`，
  WAL 模式、`synchronous=NORMAL`，由宿主以 0600 普通文件打开（拒绝符号链接），`user_version`
  不符即拒绝打开（不迁移）。两张表：`runs(run_id 主键, created_at, tenant_id, status, holds, record)`
  保存完整 `RunRecord`（JSON）与查询要过滤的列；`events` 以 **`(run_id, seq)` 为主键**保存每个已提交帧。
  读者看见一帧当且仅当它的插入事务已提交：没有半行、没有提交水位、没有截尾修复，也没有进程内
  索引或模块级缓存——列表、按租户列表、资源台账与准入都直接查询同一个库。报告、子代理产物与
  会话文件仍是 `runs/<id>/` 下的文件（§5.2）。
* **终帧就是终态提交**：`RunStore.commit_terminal()` 在**同一个事务**里插入终帧并把 run 行改写为
  终态记录（结论、报告、清理结果、`event_count` = 终帧 `seq`），该事务以 `synchronous=FULL` 提交。
  提交返回之前任何读者都看不到其中任一半；返回之后详情、列表、报告下载、SSE 重放与资源台账
  都读到同一个已提交事实，且不会先出现再撤回。事务失败（含约束或磁盘错误）整体回滚并抛
  `StoreWriteError`，run 保持同一个未提交的 `running` 状态。run 行已是终态时，同一事务内拒绝再写
  任何帧（`RunAlreadyTerminal`），因此终帧之后什么都不能追加；普通帧不能经 `append_event` 写终帧，
  `update()` 也拒绝终态字段（`status`/`finished_at`/`cleanup`/`event_count` 只能随终帧提交）。
  耐久性以 SQLite 的提交为界（不额外做目录 `fsync` 或 macOS `F_FULLFSYNC`）。
* **准入读它写的台账**：`RunStore.create(admission_check=…)` 在 `BEGIN IMMEDIATE` 事务内读出持有资源
  的行（`holds = 1`）交给配额检查，检查通过才在同一事务内插入新行；拒绝时整体回滚，不留行也不留
  run 目录。配额计数与插入因此不会与同库的另一个写者交错。
* 环形窗口只是「读完日志 → 挂上实时尾」之间的交接缓冲，不是历史来源；run 关闭时即释放，
  不会让所有 run 的历史常驻内存。用户历史（`events` 表）不因任何 TTL 被删除。
* 单机单宿主：`runs.db` 只被持有 `host.lock` 的进程写入，不是多主机共享存储（见 §4 所有权）。

### 3.2 重放与慢消费者

`GET /api/runs/{id}/events` 的顺序是：**先订阅实时尾，再读持久日志，最后按 `seq` 合并去重**。

* 活跃 run：日志里的已提交帧先写出，实时帧按 `seq > 已写出水位` 继续；读取期间关闭的 run
  也不会丢终帧（终帧已在日志里，或在订阅队列里）。
* 已结束/上次进程的 run：只读 `events` 表（按 `seq` 分页），因此重放不受内存窗口容量影响，也无需重新启动 pi。
* 每个实时订阅者有一个**有界队列**（默认 2000 帧）。停止读取的客户端会被主动断开，而不是让
  队列无限增长；浏览器按 `Last-Event-ID` 重连并从日志补齐，因此断开不丢已提交历史。
* **连接数有界**：全局上限 `GLINK_MAX_GLOBAL_STREAMS`（默认 64），多租户下每个租户另有
  `GLINK_MAX_TENANT_STREAMS`（默认 16；local 只用全局上限）。额度在查看 run 之前预留，超额
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
  脚本与工具输出都可能很长，单帧不受限会让 `events` 表与重放环形窗口膨胀（事件日志是
  历史事实源，不因 TTL 删除），而流式 delta 已经把完整正文交给浏览器。前端因此只在
  **非截断**的权威帧上用 pi 的副本覆盖流式正文；截断帧且流式正文更长时保留流式正文，
  仅关闭该内容块并标记 `truncated`（重放历史亦然），避免“终帧比流式更短”把回答改短。
* **ACP 族的权威帧覆盖整个回合**：pi 族 `assistant_message` 来自 `message_end`，一帧对应一条
  message，前端替换最后一个未闭文本块即可；ACP 没有逐 message 整帧，投影层在回合末装配整回合
  正文（`docs/multi-harness-plan.md` §4.1），此时回合中的正文段可能已被工具/plan 项夹断成多个
  文本块，逐 message 的替换末块规则会把整回合正文再追加一遍。ACP 装配帧因此带
  `turnScoped: true`，前端用它**合并替换本轮全部文本块**（合并块落在本轮最后一个文本块原位；
  截断且流式拼接更长时保留流式正文）；缺省该字段的帧保持逐 message 语义，旧日志按原规则重放。
* **`stopReason=length` 是交付不完整**：run 仍为 `finished`，但前端在结论区显示“交付不完整”
  提示而不是当作普通完成（§4 的终态判定表）。
* **工具终态跟随 run**：未收到自身 `tool_finished` 的工具不会默认标 `done`——
  run 正常收尾为 `done`，失败为 `interrupted`，取消为 `cancelled`；已拿到终态帧的工具保持原状态。

前端从不解析终端文本：`tests/web/run-stream.test.mjs` 里有对应断言（扫描 reducer、订阅与视图模块）。

### 3.5 子代理（ChildRun）模型

子代理是 run 作用域内的一等领域对象 ChildRun：**权威表示是文档（`child.json`）与终帧汇总，
事件只是状态迁移的推送**；子代理正文（逐步工具流、中间消息）永不进事件日志，只经
`children/<n>/` 下的产物文件与下载端点暴露。模型主体是协议可见事实（两族同构）；
pi 族另有 `_meta.json` 增强（R2，见下「R2 增强与关联」）。

**child.json 字段**（落点 `runs/<id>/children/<n>/child.json`，每次状态迁移经目录句柄原子重写，
与 §6.1 产物写入同一条边界）：

| 字段 | 含义 |
|---|---|
| `n` | run 内序号，从 0 开始单调分配 |
| `toolCallId` | 派发它的那次工具调用 id（一次调用可派发多个子代理） |
| `agent` | 子代理名（派发参数给定；ACP 族无此字段时按协议可见事实取空/省略，不伪造） |
| `task` | 任务描述，限幅 |
| `status` | `running` / `finished` / `failed` / `cancelled` / `interrupted`；终态跟随 run 的规则与工具终态一致（§3.4「工具终态跟随 run」） |
| `resultPreview` | 结果摘要，限幅 |
| `enrichment` | `"none"`（无产物可关联/非 pi 族）/ `"meta"`（已按 `_meta.json` 增强）/ `"unmatched"`（有产物但序数配对失败，不错配） |
| `artifacts` | R2 增强：关联成功的产物清单 `[{name, bytes, source}]`，文件本体复制在 `children/<n>/` 下（`<kind>.<ext>` 归一名） |

**事件载荷与识别规则**：

* 工具名识别：pi 族与 ACP 族当前都以工具名 `"subagent"` 识别子代理派发（常量 + 钉住测试），
  其他 harness 不做特例。
* `subagent_started {n, toolCallId, agent, task}`：pi 族一次 `tool_call` 可派发多个
  （`args.tasks` 数组），每个 task 一个 started；ACP（dsh）一次一个
  （`args.description` / `args.prompt`）。args 形状不符合预期时**回退为普通工具卡片**
  （防御，不伪造类型事件）。
* `subagent_finished {toolCallId, ns: [...], status, isError, resultPreview}`：一次
  `tool_finished` 收口该 call 的全部子代理；`resultPreview` 不切分，每个 child 文档记同一份
  限幅摘要。**异步派发的 ACP 包是例外**：包清单声明
  `subagent_dispatch = "async"`（dsh：`subagent` 工具在派发被接受时就以 completed 结束，只回
  `started subagent <childId>`，子代理随后独立运行；真实数据里工具终态与子代理会话创建相差
  17ms，子代理会话继续跑了约 5 分钟）时，completed 的派发调用**不产生**
  `subagent_finished`（failed 的派发仍以失败收口），child 保持 `running`，由 run 终帧按下面的跟随规则收口
  （clean→finished、failed→interrupted、cancelled→cancelled）——宁可不报完成，也不误报完成。
  其他 ACP agent 的同步工具终态仍照常收口。
* subagent 调用的 `tool_updated` 一律抑制（`"(running...)"` 占位无信息）；通用
  `tool_started` / `tool_finished` 对这些 toolCallId 不再产生。
* 类型事件载荷只能由**已脱敏**的值构造（与现有投影同一道 redactor，§3.3/§6.1）。
* 终帧 payload 新增 `children: [{n, agent, task, status}]` 汇总，是回放渲染的事实源。
* **读面与有界**：文档经 `GET /api/runs/{id}/children` 暴露（owner 与 404 口径和 run 详情
  一致，见 §10.2）；`read_children` 每次最多读 `CHILD_READ_LIMIT`（64）个文档——损坏、
  序号不符与**超出上限**的文档都计入响应的 `skipped` 如实上报，不静默等同缺失（>64 的
  派发属 runaway 场景，有界是故意设计；终帧汇总同样以这 64 个为界）。重启对账的
  `run_failed(interrupted)` 终帧与正常终帧走同一套子代理收口与汇总（文档先按
  interrupted 收口，终帧再带 children）。
* **历史日志读侧兼容**：事件日志不可变；旧格式（`toolName=subagent` 的通用工具卡片）前端
  必须仍能照常按旧卡片渲染——这是硬约束，读侧兼容不属于写侧变化。

**R2 增强与关联（pi 族）**：pi-subagents 的 `_meta.json`/笔记/transcript 是它的**私有布局**
（非官方文档化 API），由 `kernel/subagent_artifacts.py` 对照 `tests/fixtures/subagent-artifacts/`
钉住——包升级改变布局时测试立刻变红，而不是静默丢增强。命名
`<runId>_<safeAgent>_<index>_{input,output,transcript,meta}`（`index` 是单次派发内序号，
与 run 文档的全局 `n` 不同坐标系；文件名 runId 是包内派发 id，与 toolCallId 无映射），
关联因此是**序数启发式**：同 agent 名下文档按 `n`、文件按 `index` 排序，数量一致才配对，
冲突或缺侧整体标 `unmatched` 不错配。配对成功的文件复制进 `children/<n>/`（归一为
`<kind>.<ext>`），`_meta.json` 的白名单计数（`exitCode`/`durationMs`/`toolCount`/`model`/
`usage.{input,output,cacheRead,cacheWrite,turns}`）写入文档；**`cost` 被刻意排除**（估费不是
宿主事实，§3.4 口径）。增强在收尾时进行（E2B 镜像与会话目录两个候选源，三面统一），
副本过 run 脱敏边界；终帧 `children` 摘要与 `GET /api/runs/{id}/children` 带上这些字段，
`GET /api/runs/{id}/children/{n}/{file}` 按报告下载同一套规则（固定位置、目录句柄、
服务前再脱敏、`X-Redacted`、owner 404）提供产物下载。ACP 族无此布局，永不被触碰。

**运行中进度（R3a）**：`running` 期间的子代理可以有一条
`subagent_updated {n, steps, lastToolName, lastToolTarget, bytes}` 事件。
`lastToolName`/`lastToolTarget` 可空且限幅；`bytes` 是子会话/transcript 当前字节数的
**快照**——累计快照不是增量，与 `tool_updated` 同一语义。载荷只含 facts（步骤数、
最近工具名与目标、字节数），与既有投影过同一道 `_emit` 脱敏。它是「最新值取胜」的
**瞬时事实**：每 child 节流至 ≤1 条/2s，回放渲染取该 child 的**最后一条**（丢中间帧
不改变最终呈现）；它**永不参与终态判定**（终态仍只由 `subagent_finished` 与 run 终帧
裁决），观察者失败只发 `kernel_status` 警告，且**不写 `child.json`**——权威文档不被
瞬时进度污染。

进度由观察者 seam 供给：`ExecutionAdapter.child_progress_source()` 是 **ABC 默认空实现**
（与 `recovered_session` / `recover_subagents` 同例，不覆写即该面无进度）。每 run 的观察者
对象、轮询线程、停止事件、closed 标志、节流/单调钳制与有界退避都在
`runs/child_progress_observer.py`；它由 Observe 阶段插件 `runs/plugins.py::ChildProgressPlugin` 持有，
只在已提交的 started/finished 帧与收尾第一步（`ObserveStage.finish`）被调用，每 run 至多起**一个**轮询线程，finalize 时退出，连续失败按有界退避
**继续重试**（不设「失败预算后停止」：真实 E2B run 里一次沙箱请求超时就能把当时的预算花完，
临时故障不得变成 run 生命周期内的永久失明——GLINK-OBS-01），且**不在准入锁内**。E2B 面的
单次 ``files.list`` / ``files.read`` 显式传 ``request_timeout``（不使用 SDK 默认 60s），
让一次卡住的调用快速失败、退避后重试；失败仍只用一条 ``kernel_status`` 警告。
关联规则按族区分：

* **pi 族**：前台守卫保证同一时刻至多一个在飞派发 call；transcript 文件
  （`subagent-artifacts/<runId>_<agent>_<index>_transcript.jsonl`）的 `index` 等于该
  call 内 task 位置，据此把文件配对到在飞 call 的对应 child；对不上即该 child 无
  进度，不伪造。**归属安全**：同一目录会躺着上一次派发已冻结的同名 `(agent, index)`
  transcript，因此进度源只报告**自上轮以来有变化**的文件（首次见到只建基线），
  冻结的旧文件永远不会被错配给新在飞的 child（与 R2「无法区分宁可不关联」同一条
  不伪造口径）；服务端对 `steps`/`bytes` 做单调钳制（快照语义，替换重解析也不回退）。
  观察者在线程停止后不再启动：finalize 停止观察者时在 run 锁内置观察者对象的 closed 标志，
  停止与 verdict 之间迟到的 started 帧不会再起线程。
* **ACP 族**：主会话 `tool/call` 帧加上**相邻**的 `subagent/catalog` 帧给出
  callId↔childId 的精确关联，子会话的 step/tool 帧提取进度（实测见
  docs/evidence/acp-subagent-zstd-spike.txt）。会话文件是 dsh 私有的 zstd 压缩
  JSONL，按整份重读（有大小上限）；`zstandard` 为可选依赖
  （`glink-harness[acp-progress]`），缺失时该面如实无进度，不报错。

各面能力：pi 族 inproc/docker/e2b 与 ACP 族 inproc/docker（dsh 0.1.7+）为 **steps**
（逐步进度）；ACP e2b 面本次不实现，能力如实停留 **coarse**。没有进度源或关联失败的
child 在前端显示为普通「进行中」，不伪造进度。

### 3.6 事件 schema 与版本

决定与来由见 [ADR 0011](adr/0011-versioned-event-schema.md)。

**单一来源**：`host/glink_harness/event_schema.py` 声明全部产品事件类型、每个类型的 payload
字段（名称、JSON 类型、是否必填、枚举、数组元素类型）以及 payload 版本 `v`（按类型、从 1
起）。`events.py` 的类型常量与终帧集合在导入时与 schema 核对，不一致即启动失败；上表是
它的阅读版，字段以 schema 为准。

**校验边界**：

* `EventBus.commit` 在分配 `seq` 之前校验每一帧，并把 `v` 写进信封（`RunEvent.as_dict`
  / SSE `data` / `runs.db` 的 `events` 行同形）；
* 两个内核投影（pi `Projector` / `project_event`、ACP `AcpProjector`）经共享的
  `projection.checked` 校验自己产出的每个 `(type, payload)`，漂移在产生处就被点名；
* 资源台账为遗留 run 直接写入的中断终帧（不经总线）同样先校验、带 `v`。

**违规处理**：`GLINK_EVENT_SCHEMA=strict`（测试套件在 `tests/conftest.py` 设置）时违规帧
抛 `EventSchemaError`，不落盘、不发布、不消耗 `seq`；未设置时记录错误日志后**仍提交**
——生产 run 不因契约缺陷中断，但日志点名来源与字段。两种模式都把违规记入进程内列表，
测试在每个用例结束时断言它为空，后台线程吞掉的违规也会让用例失败。

**前端生成物**：`scripts/gen-event-schema.py` 把 schema 渲染为
`web/modules/event-schema.gen.mjs`（JSDoc typedef、`EVENT_SCHEMA`、`EVENT_TYPES`、
`EVENT_VERSIONS`、`TERMINAL_EVENT_TYPES`），勿手改；`scripts/check.sh` 以 `--check` 校验
它与 schema 一致，过期即失败。

**前端分层**（`web/modules/`）：

* `run-reducer.mjs` —— 纯 `reduce(state, event)`：`seq` 水位去重与缺口计数、累计快照
  替换（`tool_updated` / `subagent_updated`）、增量追加（`*_delta`）、权威终帧收口
  （`assistant_message`、`tool_finished`、run 终帧及其 `children` 摘要）。不触碰 DOM、
  网络与时钟（结束时间取终帧自身的 `ts`），由 `tests/fixtures/events/*.jsonl` 驱动的
  `tests/web/run-reducer.test.mjs` 覆盖；
* `run-stream.mjs` —— `createRunSubscription`（`EventSource`、有界重试、状态上报，监听的
  具名帧来自生成物）与 `createRunFeed`（订阅折叠进 reducer，按终帧**类型**关闭流）；
* 视图 —— `run-view.mjs`（单个 run）、`run-list-view.mjs`（导航/首页/报告列表），
  `workspace.mjs` 只做编排。

**版本与旧数据**：reducer 只应用 `v` 与 `EVENT_VERSIONS[type]` 严格相等的帧；其余帧（含
未知类型）不渲染、计入 `rejectedFrames`，但 `seq` 仍推进水位。改变某类型 payload 含义且旧
读者会误读时，提升该类型的 `v` 并同步 reducer。**schema 引入之前写下的事件日志没有 `v`，
当前前端不可读，也没有迁移**：回放这类 run 时时间线为空，状态停在 REST 记录给出的值。

### 3.7 跨消费方一致性：事件样例库与契约测试

决定与来由见 [ADR 0021](adr/0021-event-fixture-contract.md)。

同一事件流有多个消费方 fold（本仓 `run-reducer.mjs` 块模型；ULegal trajectory 行投影），
一致性不靠共享 fold 代码（两侧语言栈不同），靠 glink 维护的**权威事件样例库**
（golden fixtures，覆盖事件词汇全形态；`tests/fixtures/events/*.jsonl` 为其雏形）与各消费
方的**契约测试**：同一份样例喂两侧、fold 输出快照必须一致。演进护栏：新事件形态先入
样例库再进实现，消费方契约测试不过即红。ULegal 侧 trajectory 定位为业务里程碑 + 展示
fold，delta/直播帧不落盘不投影；事件原文存档归 R3 存储边界重定义
（[multi-harness-plan.md](multi-harness-plan.md) §6），本期不建。

## 4. run 生命周期

1. `POST /api/runs {prompt, parentRunId?, blueprint?}` → 准入（见下）→ 生成 `runId`（run 目录**排他**创建，
   生成的 id 与已有行或目录碰撞则有限重试重新生成；既有记录绝不被覆盖），在同一事务内插入 run 行（status=running）。准入先过
   **宿主级 `/` 命令门禁**（与 harness 无关，GLINK-ARCH-6）：去掉前导空白的 prompt 若以 `/` 开头，
   只有 `/<蓝图名> <题目>` 这一种形式被接受——宿主把它解释为 `blueprint=<蓝图名>` +
   `prompt=<题目>`（与显式 `blueprint` 字段同一条解析；两者同时给出即拒绝，不猜优先级），
   蓝图名从 `methodology/blueprints/` 目录推导、不硬编码；其他 `/` 开头输入一律
   `400 unsupported_command`（错误体只给推导出的蓝图名列表，不回显被拒输入），题目本身仍以
   `/` 开头时同样拒绝（重写后的 prompt 绝不能带着 `/` 进入内核）。门禁在凭据解析、父会话读取、
   run 目录与执行面创建之前，追问（`parentRunId`）与三个执行面同样适用。该门禁存在的原因不变：
   pi 对以 `/` 开头的消息先派发扩展命令（docs/rpc-commands.md），载入 pi-subagents 后
   `/run --bg`、`/subagents-detach` 这类客户端可控输入能绕过 `tool_call` 守卫发起不随本 run
   收口的后台进程（见 pi-integration-notes.md §10.4）；命令集合现在来自宿主自己的蓝图目录，
   不再依赖 pi 的模板展开。
   `parentRunId` 只接受已有会话的最新已结束轮次：从父 run 专属目录安全读取脱敏后的会话文件，复制到新 run 专属目录，用 pi 官方 `--session` 续接；会话不可用或已存在后续轮次时拒绝，不冒充追问。追问门禁的同族判定比 run 记录自身的事实：准入时 run 行在同一事务内写入本 run 的 `RunSpec`（`"spec"` 对象，见 §2：`family`、`pack`、`executor`、`blueprint`、`credential_origin`，来自冻结配置，与 run id/owner 同一次写入），session_ready 提交时写回 `agent`（ACP 为协商到的 agentInfo.name；pi 协议不报 agent 身份，不伪造）；`spec.family` 不同的跨族追问一律 `session_unavailable`（fail-closed），同族但 `spec.pack` 与本包不同同样 `session_unavailable`；缺 `"spec"` 或缺必填字段的行按不可解析处理（fail-closed），不会进入追问门禁；旧 `meta.json` 记录根本不被读取（[ADR 0001](adr/0001-required-run-admission-facts.md)、[ADR 0002](adr/0002-follow-up-judged-by-parent-record.md)）。**多租户模式下父 run 还必须属于调用者自己的租户**：未知/异租户/无 owner 的父 run 在读取它的会话、报告、状态或子 run 计数之前就按未知 run 拒绝（404）。前端按父链展示连续对话，侧栏只列最新一轮；
2. 渲染**本 run 的**运行期配置目录与 session 目录（`runs/<id>/pi-agent`、`runs/<id>/sessions/`），
   按 `LOCAL_WEB_EXECUTOR` 选择执行面（见 §5）；
3. 事件投影 → `EventBus.commit`：先插入 `events` 表，再实时给 SSE（见 §3.1）；
4. 执行面把冻结输入交给内核，等待「落定」或超时/取消/进程退出；
5. 收尾把四类事实分开，各有唯一 owner 与提交点（`runs/pipeline.py` 与 `runs/stages.py`，见下「生命周期管线」）：

   1. **执行事实**：执行面 outcome、最终模型结果、事件日志健康度，run 处于 open 阶段时收集；
   2. **产物事实**：先向执行面回收产物（E2B 从沙箱拉到工作区），再 `close()` 执行面；随后经
      目录句柄安全读取 `report.md`（见 §6.1），脱敏后写入宿主拥有的
      `runs/<id>/artifacts/report.md`。拒绝的产物记为 `report_rejected` 并发出 `kernel_status`
      警告，缺失记为 `no_report`；工作区与会话文件各过一遍脱敏。ACP 声明的补充交付文件
      （最多 8 个）单独安全读取、脱敏并封存到 `artifacts/<相对路径>`；终帧与记录包含逐件
      成功/失败原因，回收失败不阻终态（[ADR 0017](adr/0017-declared-deliverables.md)）；
   3. **结论（verdict）**：在 run 锁内**一次性**由全部事实与取消标志判定，并立即以同步追加
      终帧提交（§3.1，`fsync` 成功才可见）。这是 run 的**线性化点**：之前受理的取消必然体现为 `cancelled`；之后的
      取消被拒绝并返回已提交状态；之后到达的内核帧被丢弃，终帧之后不再有任何帧；
   4. **资源释放**：`close()` 的 `CleanupReport`（`released: true/false`，异常视为 `false`）写在
      终态记录里；收尾崩溃导致清理从未执行时记 `released: null`（未知，同重启对账的中断收口），持有不因此消失。它独立于结论，也独立于线程是否结束（见下「准入与资源」）。

   提交成功后写落定副本、关闭实时流、移出执行表（连同总线的每 run 状态一并清理，
   防止随 run 数无界累积；后续读面回退持久化事实）。若终帧写入或 `fsync` 失败（如磁盘满、I/O 错误），**没有任何读面
   显示终态**：run 保持 `running`、保留执行槽与实时流，停止请求返回 `finalizing`，
   `shutdown()` 报告该 run，下次启动由新 owner 按中断收口。

**族接口（HarnessFamily）**：上文中一切「pi 族做 X、ACP 族做 Y」的策略都收敛在
`kernel/harness.py` 的 `HarnessFamily` 后面，两个实现是 `harness_pi.py` 的 `PiHarnessFamily`
与 `harness_acp.py` 的 `AcpHarnessFamily`；族实例由 `select_family` 构造、按 `RunSpec.family`
缓存在 `RunAdmission`（`family_for`；RunManager `__init__` 先解析配置的族，配置错误在启动时失败），每个 run
的族取自**它自己记录里的** `spec.family`（`family_of`），不读当前配置做族分支——准入时
`RunSpec` 随记录原子写入是冻结点本身，留在 runner；GLINK-ARCH-6 起 `select_family` 的输入是
`harnesses/<pack>/harness.toml` 解析出的包清单：`family` 决定实现，包的能力提示与 ACP 包的
`home/` 覆盖层由该包提供，包名随 `spec.pack` 写入 run 记录；P3 起 `HostConfig.pack` 直接持有
`HarnessPack` 对象，族代码只读其 `[capabilities]` 能力矩阵——`resume`、`subagent_dispatch`、
`child_progress`、`session_config`、`permission_allow`、`patch_overlays`——不再按包名特判）。族接口覆盖六面职责：(a) 准入
——凭据要求（`/` 命令门禁与斜杠解释自 GLINK-ARCH-6 起是宿主级、harness 无关，不再由族提供白名单）；
(b) 追问准入与续接种子——族追加门禁、工作区复用、会话种子与 `resume_session` 值、会话身份
的校验时机；(c) runtime 渲染与 projector 工厂（渲染中新知的秘密经 `PreparedRuntime` 交出，
由 runner 按原序并入本 run 脱敏边界后才构造 projector；渲染出的 runtime 同时自带
`session_sweeps()` 声明收尾扫描哪些会话/状态路径、能否重写、哪些注入凭据副本要删除，见 §6.1）；(d) launch 与协议驱动——`launch(spec, sandbox)`
给出与载体无关的 `Launch`，`driver(spec, sandbox=, launch=, ...)` 给出本族的 `ExecutionAdapter`，
`build_adapter` 是二者唯一的组合（§2）；族内没有按执行面名的 plan 方法。资源一存在前即确定的身份
（Docker 容器名）由 `sandbox.planned_identity()` 给出、runner 在 `start()` 前持久化，资源创建后才
知道的身份（E2B 沙箱 id）由 sandbox 经 `on_identity` 回调交给 runner——两族同行为，是执行面事实；
(e) 终态词汇归一化——`model_stop_verdict` 把各族自己的 stop 词汇（pi 的
error/aborted/length，ACP 的 StopReason 集合）译成产品终态词汇，译不了即落入 runner 的交付
策略（报告检查）；产品词汇本身（status 三值与 stop_reason 字符串集合）仍由 `DecideStage`
定义；(f) 子代理增强与运行中事实——pi 族的产物关联（`meta`/`unmatched`/`none`）、按族的进度
配对（pi=transcript index+agent 同名，ACP=call_id 精确命中）与按族的用量累计（pi 去重累加，
ACP 累计快照替换）。族的 `state_transfer` 从准入认可的状态来源返回归档能力请求、回灌决定及热路径候选；宿主仅签发能力，
不导入 ACP 具体类型或重新选择状态来源。编排层保留：锁与相位（open/committed/uncommitted）、准入并发与配额、
stop()/StopDecision、生命周期管线的**顺序控制**、`DecideStage` 的共同前缀与交付策略收尾、
终帧 payload 组装与持久化、事件提交路径（`RunChannel`）、资源台账转发、共享追问门禁
（owner→`spec.family`/`spec.pack`→terminal→stale 的拒绝次序是生命周期策略）与 harness 无关的蓝图/命令
门禁。

**生命周期管线**（`runs/pipeline.py::RunPipeline`，[ADR 0013](adr/0013-run-lifecycle-pipeline.md)）：一个 run 依次经过 Admit → Prepare →
Launch → Observe → Recover → Seal → Decide → Commit 八个阶段，每个阶段只接收上一阶段冻结的
事实，次序由管线定义保证：

| 阶段 | 实现 | 输入 → 输出 |
|---|---|---|
| Admit | `runs/admission.py::RunAdmission`：`prepare`（锁外：蓝图解析、凭据、秘密边界、追问种子 IO）+ `admit`（准入锁内：父 run 权威复核、建记录）；并发/配额检查在 `RunManager.start` 的锁内 | 请求 → `AdmittedRun` |
| Prepare | `runs/stages.py::PrepareStage`：报告种子、运行时渲染（运行时绑定到秘密边界并构造 projector）、出口端点、adapter | `RunExecution` → `LaunchPlan` |
| Launch | `LaunchStage`：启动执行面并等待落定，执行事实写入可变的 `LiveExecutionFacts` | `LaunchPlan` → 执行事实 |
| Observe | `ObserveStage.finish`：插件 `close`（子代理进度观察者先死）→ projector 首 flush | — |
| Recover | `RecoverStage`：`face_artifacts`（向执行面回收产物、会话事实、子代理镜像）→ `close_face` | → `RecoveryFacts`（含 `ResourceFacts`） |
| Seal | `SealStage`：`sweep`（工作区/子代理镜像/会话与状态根）→ `enrich`（插件 `seal`：子代理增强）→ `report` | `RecoveryFacts` → `ArtifactFacts` |
| Decide | `DecideStage`：在 run 锁内把执行事实冻结为 `ExecutionFacts`，与取消标志一起判定 | → `Verdict` |
| Commit | `CommitStage`：同一锁内插件 `settle`、终帧提交（线性化点）、提交后簿记、出口授权释放 | `Verdict` → committed |

`RunManager`（`runs/runner.py`）只保留公共 API（start/stop/wait/shutdown/reconcile 与资源台账转发）
和准入锁；阶段数据类型在 `runs/state.py`，事件提交路径（提交前再脱敏、用量累计、插件
`on_committed`）在 `runs/channel.py`。**秘密边界** `runs/secrets.py::RunSecretBoundary` 把一个 run
的脱敏器、出口 token 授权、清扫根（工作区、子代理镜像、运行时声明的会话/状态根）与注入凭据
副本收成一个对象：各阶段经它脱敏与学习新值，只有 Seal 阶段调用它的收尾扫描（`seal_trees` /
`seal_sessions`）。子代理进度与增强是 Observe / Recover–Seal 阶段的插件（`runs/plugins.py`：
`ChildDocumentsPlugin`、`ChildProgressPlugin`、`ChildEnrichmentPlugin`，钩子为
`on_committed` / `close` / `seal` / `settle`），族差异经 `HarnessFamily` 的 seam（进度配对、
进度源、`enrich_children`）供给，不是 runner 方法。

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
`finalizing`）；未知 run 返回 `404 unknown_run`。取消请求在 pi 尚未启动时也被受理。三个执行面的
取消都是协议级 abort：E2B 面沙箱存活到收尾，被取消的 run 与完成的 run 一样回收 `report.md` 与
主会话（终态表第一行的「已回收的报告保留」对三面一致成立；旧 `--mode json` 路径运行中不能
abort、取消即毁沙箱的义务已随 GLINK-ARCH-3 删除，见 §5.1/§5.2）。

**准入与资源**：新 run 只在「本进程没有执行中的 run（仅 local 模式；否则 `409 run_busy`）」且容量未满时
受理。资源台账直接由持久记录导出：未终态的 run、`cleanup.released` 不为 `true`（`false` 为已知失败，
`null` 为未知）且没有裁决的终态 run 都占用容量；早于资源跟踪的历史记录（无 `cleanup`）不参与。重启不会
清空台账。

* **local 单用户**：任一持有未确认释放即 `409 resources_unreleased`（附 run id），且保持全局单 run 槽位。
* **multitenant**：持有计入其 owner 租户的并发槽位**和**全局槽位（无 owner 的记录——单用户面写入——只计全局），
  与 `GLINK_MAX_TENANT_RUNS` / `GLINK_MAX_GLOBAL_RUNS` 一同比较，超额返回
  `429 quota_exceeded`（scope/limit，不含 run id）；持有不会被遗忘，达到全局上限时所有新准入都被拒绝
  （fail-closed）。
* **不可解析行**：`runs.db` 由宿主独占写入，某行的 `record` 不是完整记录（非 JSON、缺必填字段或
  `spec`）时不绕过猜测：读到它的列表、详情、资源台账与准入一律抛 `StoreError`（fail-closed，新准入
  全部拒绝），直到操作员修复数据库。不再有 `corrupt` 标注与 `resource-adjudication.json` 侧车。

释放容量有三条路：执行面清理确认 `released: true`、在线自动对账确认，或操作员在宿主**停止时**以
`python -m glink_harness.app --mark-released <runId> --note "<核对说明>"` 记录人工裁决（`--unreleased`
列出待核对项）。人工裁决写出 `resource_adjudication{decision, source: "operator", note, at}`，自动对账
写出 `source: "auto-verified"` 与 `method`；两者都保留原清理事实与已提交的终态。离线运维命令不接触
Docker/E2B，也不删除任何资源。

**在线自动对账**：宿主持有数据目录时，按 `GLINK_RESOURCE_RECONCILE_SECONDS`（默认 60，0 关闭）在
后台线程周期性确认未释放的持有。探测只使用 run 记录里**持久化的执行身份**，且按记录选面：有
`container_name` 就按名字 `docker inspect`（不存在即确认；仍存在则先按 `stop_argv` 语义幂等
`docker rm -f` 后再次确认），有 `sandbox_id` 就调用 E2B SDK 按 id 终止/查询（凭据取自冻结配置，
不依赖进程环境）。不按名字模式猜测，不列举或批量操作任何资源；两种身份都有的记录必须都确认。
「已不存在」只认执行面的契约（inspect 的 no-such、SDK 的 `False` 返回或 `SandboxNotFoundException`
类型）：探测失败、超时、鉴权错误或无法判定的返回一律保持持有。对账不在准入锁内执行，关闭宿主时
停止该线程；`inproc` 宿主不启用它（不取得外部资源；遗留的外部身份仍走离线人工裁决）。
追问权威准入、泊车、租约更新、清理与删除共享对话级进程内互斥（`contracts.conversation_guard`）；
清理在锁内重查持有 run，检查、按 id 销毁与删除记录同属一个临界段。追问先等对话锁、再进全局准入锁
复核并创建持有记录，慢云调用不占全局准入锁；运行中会话不靠延长闲置 TTL 获得保护。
同一个扫描线程还销毁**过期的对话租约**（[ADR 0020](adr/0020-conversation-sandbox-lease-hot-path.md)）：
`conversations/<根run>/lease.json` 过了闲置 TTL 就按其记录的沙箱 id（含挂账的 retired）销毁，
全部确认后才删记录；未确认保留到下轮，未过期仅重试 retired 挂账。租约对账只需要冻结的 E2B key（宿主
换面后仍能清理），无 key 时不动任何租约；它不进入 run 的持有/槽位语义。
台账、重启对账、人工裁决与在线扫描的实现归属 `runs/resource_ledger.py`（`RunManager` 上的同名方法
只是转发，探针组装——含 E2B 冻结凭据——也在该模块内完成）；对话删除的最小闭环
（`RunManager.delete_conversation`：销毁租约沙箱 + 清理对话状态对象）也在该模块承载。

**所有权、重启对账与关闭**：宿主先取得 `data_root/host.lock` 的独占 `flock`（进程存活期间
持有、退出即释放；同进程二次获取也被拒），**再**对账和绑定端口。`data_root` 由宿主以 0700 创建，
`runs/`、`workspaces/` 与各 run 子目录（含 `pi-agent/`、`sessions/`、`artifacts/`）同样；已存在的
data_root 若是符号链接、不是目录或带组/其他人写权限，宿主拒绝启动并提示操作员自行 `chmod 700`
（不自动改动已有目录权限，参照账号库的 fail-closed 口径）。同一 data_root 的第二个宿主
——无论同端口还是不同端口——在读任何记录前以 `DataRootBusy` 退出，不改动活跃 owner 的 run。
`RunManager.reconcile()` 要求持有所有权：终帧与终态同一事务提交，遗留 `running` 行因此必然没有终帧，
一律提交 `run_failed(interrupted)`，资源状态为 `released: null`（未知），并持续占用容量直至裁决。
容器名/沙箱 id 在拿到时即持久化，`unreleased_resources()` 供操作员做有界对账。除在线自动对账按已持久化
身份执行的那一次幂等删除/终止（上文）之外，宿主不操作任何其他遗留资源：没有身份、探测不确定或
不属于本记录的容器/沙箱永远不会被触碰。进程退出时 `RunManager.shutdown()` 取消并等待本进程 run，
返回所有「终态未提交或资源未确认释放」的 run id（包括更早遗留的持有），进程返回码非零。
`flock` 只覆盖单机本地文件系统，不是多宿主租约（见 [多租户方案](multitenancy-plan.md) §3.2）。

## 5. 三种执行面对照

| 维度 | inproc（本机） | docker | e2b |
|---|---|---|---|
| 内核进程位置 | 宿主机器 | 一次性容器 | 云沙箱 |
| 协议 | `--mode rpc`（双向 JSONL） | `--mode rpc`（跨容器 stdio） | `--mode rpc`（沙箱后台命令 `background=True, stdin=True` 的双向 stdio，经 `SandboxCommandProcess` 以 `popen=` 注入共享 `PiRpcClient`） |
| 事件时序 | 实时 | 实时 | 实时（沙箱命令流即权威：断流即 `execution_error`，不补发、不对账，见 §5.2） |
| 运行时来源 | 宿主安装的 Node v24.16.0 / pi | 镜像自包含（`glink-harness-worker:latest`，node v24.21.0 + pi 0.86.1 + `/opt/pi-packages` 预装 pi 包；镜像名在启动时冻结） | 模板自包含（`glink-harness-worker`，node v24.21.0 + pi 0.86.1 + `/opt/pi-packages` 预装 pi 包） |
| 运行期配置 | `runs/<id>/pi-agent`（本 run 独占） | 同目录挂到 `/config` | 同一目录整棵上传到沙箱 `/home/user/config` |
| 工作区 | `<data>/workspaces/<runId>` | 同左（挂到 `/workspace`） | 沙箱内 `/home/user/workspace`，结束后拉回宿主 |
| 会话文件 | `runs/<id>/sessions/`（宿主可查） | 该目录挂到 `/sessions`；宿主按挂载映射回本 run 目录后脱敏 | 销毁前按会话头判别回收主会话到本 run 目录（分支会话头部带 `parentSession`，主会话不带），供下一轮使用；主会话无法唯一确定或回收失败不可续接 |
| 模型凭据 | 真实 key 不进执行环境；本 run 的 `auth.json` 只含出口代理 token（§6），`baseUrl` 指向 `http://127.0.0.1:<port>/model` | 同一 token 随 `/config` 挂载；`baseUrl` 走 `host.docker.internal`（`--add-host …:host-gateway`）或 `GLINK_EGRESS_PUBLIC_URL` | 同一 token 随运行期配置上传；必须配置 `GLINK_EGRESS_PUBLIC_URL`，否则启动即拒绝 |
| 搜索凭据 | 有检索 grant 时 `web-search.json`（600）只含 token 与代理 `/search` 地址 | 同左；Docker 带检索 key 需 https 的 public URL | 同左（随沙箱消失） |
| 隔离强度 | 进程级 | 容器级 | 云端沙箱级 |

### 5.1 统一执行 Interface

三种面只在「怎么把内核拉起来、怎么说话、怎么取消、怎么回收、怎么释放」上不同；这些差异全在
`kernel/execution.py` 的 `ExecutionAdapter` 后面。Run 管理只驱动一个固定顺序：

```
start() → session_info() → deliver_input() → wait(cancel, deadline)
        → recover_artifacts(destination) → recovered_session() → close() → CleanupReport
```

* **冻结输入**：`ExecutionSpec` 携带 run id、prompt、system prompt、本 run 工作区与运行期配置、
  timeout；适配器不在运行时重读全局环境（Docker 镜像、E2B 模板、凭据与路径均在配置冻结时确定）。
* **事件**：三个适配器都只把内核帧交给同一个 `on_frame` 回调（投影 + 持久化在宿主侧），
  因此浏览器看到同一套产品事件。
* **最终 Outcome**：`ExecutionOutcome` 只携带执行层事实（是否落定、exit code、取消、超时、错误）；
  最终终态由宿主结合模型结果与产物策略判定（§4）。`ExecutionOutcome` 与 driver 线程适配器共用的
  封存基类（outcome 锁、settle/seal 临界区协议）在 `kernel/outcome.py`；各面的 `wait` 循环与
  错误文案仍属各自适配器（pi 三面共用 `PiExecutionAdapter` 的 `wait` 循环，`sandbox.remote`
  为真时只追加两条面事实：`cancelled`/`timed_out` 返回前多等一个 abort 收尾宽限
  （`ABORT_SETTLE_GRACE_SECONDS` = 10s，只保护回收完整性，终态判定不受宽限影响），带
  `stream_error` 的 `kernel_exit` 改写为 `execution_error`——断流即失败，见 §5.2；
  ACP 面即时判定且超时拆解是 `session/cancel`）。
* **取消/截止时间**：取消是一个 flag（run 管理拥有），`cancel()` 只负责提前结束阻塞；超时由
  `wait(deadline)` 的适配器实现处理，返回 `timed_out` 而不是抛异常。pi 三面的 `cancel()`
  都是协议级 RPC `abort`（spike 实测被受理：流停止增长、`get_state` 回 idle）；E2B 面因此
  沙箱存活到收尾，**被取消的 run 也能回收 `report.md` 与主会话**——旧 `--mode json` 路径
  运行中不能 abort、cancel 只能直接销毁沙箱，报告与会话随沙箱即毁，该旧义务已随旧适配器删除。
* **产物回收与关闭**：`recover_artifacts` 先把该面的产物放到宿主工作区（inproc/Docker 本来就是
  宿主目录，是 no-op；E2B 从沙箱拉回，工作区直系子项限额 `WORKSPACE_RECOVER_MAX_FILES` = 64 个文件、
  总量 `WORKSPACE_RECOVER_MAX_TOTAL_BYTES` = 64 MiB、单文件 `ARTIFACT_MAX_BYTES` = 32 MiB，
  逐文件限幅并拒绝路径分隔符），然后 `close()` 才销毁
  进程/容器/沙箱并报告 `CleanupReport(released, failures)`——E2B 的销毁经 `kill_within_budget`
  （`E2B_KILL_TIMEOUT_SECONDS` = 15s），超时记入 `failures` 并在重启对账时按不确定保持持有（hold）；
  E2B 的文件类调用统一带 `request_timeout`（`E2B_REQUEST_TIMEOUT_SECONDS` = 15s），卡死的调用记为
  产物失败而不拖长收尾。清理失败会被记入 run 记录并继续占用容量（§4），不谎报已释放。
  回收先取 `report.md` 与合法声明的交付文件（嵌套相对路径逐级列目录确认无链接），再取普通
  直系子文件；三项上限不放大，优先级只决定谁占配额，超限记入 `artifactFailures`。
  回收只取**普通文件**：沙箱条目按 SDK 报告的 `EntryInfo.type`（真实 SDK 是 `FileType` 枚举）跳过目录与
  符号链接——不跟随链接，与 §6.1 的 ArtifactTree 同一条边界——它们不记为失败；超限或不可读的普通文件
  才记为失败并写进终态 `artifactFailures`。回收后由 `recovered_session()` 回答「回收过程才得知、值得
  补发 `session_ready` 的会话事实」（pi 沙箱面在 `session_ready` 即知会话身份、回收带回会话文件后
  补发一帧带宿主 `session_file` 的 `session_ready`、ACP 沙箱面的 state-root 回收结论）；
  其余面在 run 开始前已知会话，恒返回 `None`。
* **持久执行身份**：`identity()` 给出容器名或沙箱 id。身份在资源一存在时就写入 run 记录：
  Docker 面在 `docker run` 之前的容器名确定后写入，E2B 面在拿到 `sandbox_id` 后、上传配置/续接之前
  写入；创建后的失败清理不会抹掉已拿到的身份，终态 `cleanup.external` 也带上它。它是重启对账（§4）
  与在线自动对账唯一的资源归属凭据，不靠猜名字。

### 5.2 每 run 独立可写环境

一个 run 拥有自己的运行期配置目录、session 目录与工作区：

```
<data_root>/runs/<runId>/
├── pi-agent/                     PI_CODING_AGENT_DIR（models.json、auth.json、模板副本）
├── sessions/                     PI_CODING_AGENT_SESSION_DIR（每 run 一个，随 run 目录创建）
└── artifacts/report.md           宿主拥有的主报告副本；声明的补充文件在 artifacts/<相对路径>
<data_root>/runs/runs.db          run 记录与持久事件（SQLite，§3.1）
<data_root>/workspaces/<runId>/   内核工作目录
```

渲染每个模板条目都是「先删再拷」，并且每次渲染都会重建（或删除）`auth.json` /
`web-search.json`；因此上一 run 的扩展、skill 或 token 不会因增量拷贝而残留（两者只含本 run 的出口代理
token，run 收尾时 token 已吊销，文件保留也无害，见 §6）。共享项只限于
仓库 `harnesses/<pack>/` 与 `methodology/` 模板目录（只读输入）和 `data_root` 本身。

**多轮续接**：run 的事件日志与终态仍不可变；追问是有 `parent_run_id` 的新 run，不修改上轮。每轮独立配置、工作区和会话目录，复制已脱敏会话（≤32 MiB）与上一轮安全报告 `report.md`（不复制其他中间文件）；仅作上下文的报告若本轮未改动，不冒充本轮新产物，旧报告仍在历史轮次。Docker 用容器内 `/sessions/continued.jsonl`，E2B 先回收并清扫会话文件，下一轮上传副本再续接。历史 E2B 运行未回收的会话不能自动续接。E2B 面的续接身份验证时点与另两面相同（GLINK-ARCH-3 起）：pi 以 `--session /home/user/sessions/continued.jsonl` 原地打开续接会话（保持原 session id），RPC `get_state` 在 `session_ready` 即知会话身份，追问身份不符在**回合前、第一次模型调用前**拒 `session_unavailable`；e2b 面 `session_ready` 也从回合后补发改为回合前发布（回收后 `recovered_session()` 仍会补发一帧带宿主 `session_file` 的 `session_ready`）。（决策史见 [ADR 0004](adr/0004-pi-e2b-rpc-convergence.md)。）ACP 追问的状态根继承按**父 run 记录的 `executor`** 推导：e2b 指向对话状态根 `conversations/<根run>/state`（父轮有自身 `acp-agent/recovered` 完整镜像时优先取它，包括混合通道历史；没有自身镜像时对话 manifest 必须来自父轮自己，坏 manifest 不回退）、其余执行面指向 `acp-agent/home`，宿主切换执行面后历史 e2b run 的合法追问仍可受理（[ADR 0002](adr/0002-follow-up-judged-by-parent-record.md)、[ADR 0018](adr/0018-conversation-state-archive-channel.md)）。e2b 面的回收/回灌优先走归档通道（单请求 tar 流，按 offset 增量，凭据目标不进归档），没有父轮自身 recovered 镜像时，门禁要求对话 manifest 的 `sourceRunId` 等于父 run 自己；坏 manifest 一律拒绝而不回退。门禁认可的状态根随准入事实传给 Prepare，只有该根是对话级 `state/` 时才由归档通道回灌；旧 `recovered` 或本地/Docker 父轮的 `home` 仍经继承 + files 上传，不能因通道开启而换成另一个空源。缺少合法 manifest 的 `state/` 不受理，归档 GET 返回 404 而非成功空 tar；通道不可用或失败时降级为宿主侧继承 + files 逐文件（现状能力），降级事实进事件流。状态上限是异常检测三分支（[ADR 0019](adr/0019-acp-state-bounds-warning-tier-and-adaptive-resume.md)）：越过警告线（总量 64MB/单文件 32MB/2048 文件，正常研究会话的数倍）只发 `kernel_status` 警告（`acp_state_threshold_warning`）照常续接，越过 fail-closed 线（总量 256MB 对齐通道）才整次拒绝；续接 RPC 的超时按状态根体量自适应（保底 60s + 4MiB/s，硬上限 600s），继承/回灌/回收的体量耗时与 resume 重解析耗时（`stateInherit`/`stateBackfill`/`stateRecovery`/`resumeDurationMs`）进 `session_ready`。多租户模式下父 run 先过租户门禁（§10.2），否则不读它的任何字段；子 run 继承自己的 owner、凭据与私有工作区。三面 live 续接尚未验收，隔离替身测试不能替代镜像/模板验证。

**ACP 旧审计兼容例外（GLINK-ACP-15）**：状态继承/回灌与凭据交付完成后、新 agent spawn
之前，只有 dsh 续接目标 HOME 的 `.dsh/sessions/<cwd>/<session>/session.v3.jsonl.zstd`
可作一次白名单 envelope 兼容：仅为已退役的宿主自有 `web/jina-fetch-request` 补
`ignorable: true`，不改事件数、seq、引用、payload 或其他字段；已有标记不重写，未知类型
继续 fail closed。本地/Docker 处理本轮继承副本，E2B 处理 files/归档冷回灌目标或 `reboot`
热重连的沙箱目标；热路径的旧 agent 已在 pause 前停止，reboot 不恢复活 writer。
父 run 与权威归档不就地修补，本轮正常回收仍可发布其最新状态。lock 不删除。编解码复用
Node 公共 Zstandard API，按完整帧验证（头单独一帧），压缩文件沿用继承上限，解压日志另按
每目标根总量 ≤256 MiB 限幅。远程从可信 `/` 锚点逐级按即时目录 entry kind 核验
HOME 自身与全部父分量，核验后才遍历/读写（热重连先于 generation 读取与凭据上传）；
scratch 写入前复验，rename 保留全目录链无链接守卫。被拒绝的 HOME 不再由失败收尾
回收或发布。本地 ArtifactTree 目录句柄不变；替换原子发布，失败不启动 agent，
过程日志不含会话正文或 URL。理由与授权边界见 [ACP 集成笔记 §19.6](acp-integration-notes.md#196-修订撤销-webjina-fetch-request-会话事件)。

**子代理副产物**：pi-subagents 的分支会话与主会话同名同目录（只能按会话头区分，见上表），产物目录由 `artifactDir: "session"` 落在会话目录的 `subagent-artifacts/`。E2B 面在沙箱销毁前把这些有界镜像到宿主拥有的 `runs/<id>/subagents/`（分支会话进 `sessions/`、产物进 `artifacts/`；深度/文件数/单文件/总量限幅，链接不跟随，失败记 `artifactFailures`），inproc/Docker 面的同类文件本就在宿主会话目录内、不另拷贝；两路的会话目录与镜像目录都在收尾时过 run 的脱敏边界（与 §6.1 会话文件同一条规则）。R1 起每个子代理另有权威文档 `runs/<id>/children/<n>/child.json`（§3.5）：它是协议可见事实的落地，与 P0 的 `subagents/` 原始镜像各自独立——镜像是回收基质（保持原样、不改写）；R2 起收尾时按序数启发式把产物关联复制进 `children/<n>/`（§3.5「R2 增强与关联」），未关联的文件留在基质里并记日志。

**事件时序**：E2B 面自 GLINK-ARCH-3 起与另两面说同一个协议：pi 以 `pi --mode rpc` 作为沙箱
*background* 命令启动（`E2BSandbox.spawn`，`background=True, stdin=True`），
`CommandHandle` 经 `SandboxCommandProcess`（`acp_e2b_env.py`，`subprocess.Popen` 形状的门面：
`send_stdin`/`close_stdin`/流迭代/`kill`，`returncode` 属性给客户端读退出码）以 `popen=` 注入
宿主共享的 `PiRpcClient`——帧分发、投影与终态判定与 inproc/docker 复用同一套代码（ACP 面同构
执行面：同一个 `E2BSandbox.spawn` 承载 ACP 族）。stdout 块不保证帧对齐，仍按 LF 拆帧（合法 JSON 字符串里的
U+2028/U+2029 不拆帧，与 RPC 面共用 `tests/fixtures/lf-framing.json` 语料）；prompt 本体走 stdin
的 `prompt` 帧，不再出现在命令行（旧的 base64 prompt/system 编码机制已随 `--mode json` 路径整体废除）。

**断流即失败（与旧 tee 路径的取舍，如实记录）**：RPC 通道没有第二份事件副本。
旧路径把 stdout `tee` 进 `/home/user/events.jsonl` 作「exactly-once 完整性权威」，断流可按已交付行数补发剩余帧；
新通道**流即权威**，沙箱流断开（拿不到退出码）时 run 判 `failed` / `execution_error`：已交付的帧
保留在事件日志（它们真实到达过），缺失的尾巴不存在于任何权威副本——**不补发、不伪造完整性、
不做 `get_state` 对账**（其响应帧走同一条已断的 stdout；SDK 重连是否重放已消费输出未规定，
重复投递比失败更糟；run 已至终态判定窗口，沙箱随即销毁，对账也不改变任何后续行为）。断流后
`SandboxCommandProcess.wait()` 拿不到退出码的 `OSError` 在 `close()` 内降级为日志——沙箱
`kill()` 才是释放权威，一次已判定失败的断流不再被谎报为资源未释放。这套取舍换来的是双向通道：
**运行中 abort**（取消是协议级动作，沙箱存活到收尾，被取消的 run 也能回收 `report.md` 与主会话，
见 §5.1）与 **`session_ready` 前即知会话身份**（追问身份不符在回合前拒收，见本节「多轮续接」）。
旧机制的整套 tee/marker/补读机制（事件文件、stderr/退出码落盘、`LFLineFramer`、`EventCatchUpFailed`）
已随旧 `E2BSandboxExecutionAdapter` 删除。

命令链由 `E2BSandbox.command(launch)` 组合（两族共用）：先 `test -x` 验包守卫——模板早于宿主时以
exit 78 失败（`kernel_exit`）——再 `cd` 工作区并 `exec` 整条 argv（`shlex.join` 负责
`--append-system-prompt` 文本的引用）。沙箱文件读取（产物、会话、子代理镜像）仍先按 SDK 条目
元数据或分块读取在**读取前**限幅，SDK 既不提供大小也不支持分块时拒绝整份载入，不将沙箱内容
无界读入宿主内存。双向 stdio 的可行性由 spike 实测钉死（get_state 握手、完整回合、运行中 abort、
`close_stdin` 后退出码 0，15/15，证据 `docs/evidence/pi-e2b-rpc-stdin-spike.txt`）；宿主侧离线
验证用 stdin 版 fake 沙箱（fake handle + 真实 fake pi 子进程）；**通道级 spike 已过，host 级真实
E2B live 冒烟未做**。配置交付带有 `(相对路径, sha256)` 清单，`harnesses/pi/` 的 `settings.json`、
`extensions/`、`skills/` 与生成文件都按同一路径语义送达（工作约定与蓝图由宿主拼进系统提示，不落运行期目录）。旧路径接口的误读与更正过程
记录在 `docs/evidence/e2b-run.txt` 末尾（历史证据，描述的是已删除的机制）。

同一个适配器显式传 `commands.run(..., timeout=<沙箱寿命>)`：SDK 默认值是命令累计运行 60 秒，
而真实研究 run 是分钟级，遗留默认会把内核在沙箱仍存活时杀掉（run 以 `execution_error` 结束于
`TimeoutException`，报告与产物丢失）。命令上限与创建沙箱用的同一个计算值绑定，不会比沙箱寿命更长，
也不会更短。

### 5.3 容器面预装 pi 包与 harness 包 pin

`harnesses/pi/settings.json` 是 pi 包唯一的包声明真源（`packages` 里的 `npm:` 声明带精确版本）。三个面在渲染
本 run 的运行期 `pi-agent/settings.json` 时按面向分流：

* **inproc** 保留 `npm:<name>@<pin>`，pi 启动时把它装到本 run 的运行期目录（行为不变，只是不再漂版本）；
* **docker / e2b** 把每个命中预装名单的 `npm:` 声明改写为镜像内**本地包路径**
  `/opt/pi-packages/node_modules/<name>`（pi 官方 local package，"Loaded from the resolved path
  without copying"），因此容器/沙箱 run **不再访问 registry、不再每 run 生成 `npm/`**；
  未列入预装名单的声明原样保留（本仓当前两个 `npm:` 声明都已列名：`pi-web-access` 与
  `pi-subagents`）。

版本一致性以包清单为唯一真源（GLINK-ARCH P3）：`harnesses/pi/harness.toml` 的
`[[image.packages]]` 声明 pi 内核与两个预装包的精确版本，`scripts/generate-pack-artifacts.py`
生成两个 Dockerfile 的 `ARG PI_VERSION` / `ARG PI_WEB_ACCESS_VERSION` / `ARG PI_SUBAGENTS_VERSION`
与模板的 `packages`，`tests/test_repository_boundaries.py` 检查生成产物与清单一致；容器面的
预装名单也取自同一清单（`HarnessPack.prebaked_pi_packages()`）。预装安装用 `--legacy-peer-deps`，与 pi 自己的 npm 托管安装一致：否则 npm 会自动装入
一份版本可能不同的 `@earendil-works/pi-*` peer，扩展会从副本解析宿主 API 并改变工具的激活方式。

**harness 包 pin（GLINK-ARCH-6，P3 起改为生成）**：ACP 包（dsh/codex/opencode）的上游包与精确版本
写在各自 `harnesses/<pack>/harness.toml` 的 `[[image.packages]]`（`role = "agent"`），
`docker/Dockerfile.acp-worker` 与 `docker/e2b/Dockerfile.acp-e2b` 的同名 ARG 以及
`docker/e2b/templates.json` 的 `acp-worker` 条目（别名 `glink-acp-worker`、CPU/内存、
E2B CLI 构建命令）都由生成脚本写出，同一测试比对产物与清单；闭源适配器及其 CLI 层已随
GLINK-ARCH-6 从这两个镜像删除。

**包缺失必须是明确失败。** pi 对不存在的本地包路径完全静默，所以两个镜像都带
`/opt/pi-packages/check-packages.sh`：Docker 面把它作为容器的 `--entrypoint`（镜像早于宿主时
容器起不来，退出 127；包被删时 exit 78），E2B 面在 `E2BSandbox.command` 的命令链里先验证守卫存在（守卫脚本
本身缺失同样 exit 78）再交给守卫执行。失败分类两面不同：Docker 面容器起不来/守卫 exit 78 是
`kernel_exit`；E2B 面守卫失败时 pi 从未启动，RPC 客户端的首个请求无处应答，run 以
`execution_error` 收场（错误文本为 `PiRpcClosed` 或「pi exited with code 78」，竞态相关）。
两面守卫写出的中文原因都只留在 stderr（Docker 面容器日志、E2B 面 RPC 客户端的
`stderr_tail` 历史），不拼进 outcome 错误文本。ACP 面不同：协议只走 JSON-RPC，agent 自己
的崩溃信息只在 stderr，所以 ACP 的 `kernel_exit` 报告会把有界（行数/每行/整块三层上限）的
agent stderr 尾部拼进失败详情；终帧 `error` 与宿主失败日志是同一份文本，都先过本 run 的
`SecretRedactor`，宿主自己 terminate/kill 导致的退出不归因给 agent（`kernel/acp_execution.py`
的 `_kernel_exit_message`/`_complete_kernel_exit`、`acp_rpc.py::stderr_tail`）。构建、候选 tag、回滚句柄、实测耗时与研究记录的实测事实见
`docs/evidence/prebaked-packages.txt` 与 `docs/pi-integration-notes.md` §9。

**子代理包（GLINK-SUB-02）**：`pi-subagents` 自本任务起真正加载，并且仍受第 5.3 节的同一套
机制约束（模板 pin、容器面镜像内路径、版本一致性测试）。0.40.0 没有禁用 async/detached 的
包开关，产品改用 `harnesses/pi/extensions/subagent-foreground-guard.ts` 经官方 `tool_call` 钩子把派发
强制为前台同步（可安全改写就改写，其余阻止；详单见 `docs/pi-integration-notes.md` §10），
配合 extension config 的最保守取值，使子代理成为主 run 内的同步工具调用。镜像守卫脚本目前
只检查 `pi-web-access`，这个已知缺口固定在 `tests/test_kernel_docker_env.py`（不受本任务修改
Dockerfile 的边界约束，缺 `pi-subagents` 只会退化成“没有子代理工具”）。离线的扩展单测与
无模型加载冒烟不作为真实模型/容器面验收的证据。

## 6. 凭据流

真实模型 key 与检索 key **不进入执行环境**（决定 D4，[ADR 0008](adr/0008-egress-proxy.md)）。pi 族的
run 只拿到宿主凭据出口代理（`host/glink_harness/egress.py::EgressProxy`）的地址和一个本 run 的短期 token：

* **代理**：宿主进程内一个独立的 loopback 监听（`GLINK_EGRESS_HOST`，默认 `127.0.0.1`；
  `GLINK_EGRESS_PORT`，默认 0 = 随机端口），与 Web 端口分开。首个 grant 签发时绑定，最后一个 grant
  吊销后同步停止（在锁内等监听 socket 关闭后才返回，所以固定端口下紧接着的签发可以立即重新绑定），空闲宿主没有打开的出口端口。仅接受 `POST /model/chat/completions` 转发到宿主冻结的
  `OPENAI_BASE_URL`（替换为 `Authorization: Bearer <真实 key>`），及有检索授权时的
  `POST /search/search`、`POST /search/answer` 转发到宿主冻结的
  `GLINK_WEB_SEARCH_UPSTREAM`（默认 `https://api.exa.ai`，替换为 `x-api-key`）。方法、路径精确
  匹配，不接受其它端点、编码别名或查询串；模型请求须为 UTF-8 JSON，顶层 `model` 精确等于
  grant 中冻结的宿主模型 id，非法 JSON、重复键或媒体类型不符均不转发、不注入 key。内核送来的
  `Authorization` / `x-api-key` / `Cookie` 与逐跳头不透传；上游重定向原样返回、不跟随（跟随会把真实
  key 带去别处）；上游只接受 https，或 loopback 上的 http；loopback 上游不经操作员 `HTTP(S)_PROXY`。
  拒绝码：未知路由 404 `unknown_route`（含 `..` 等路径穿越）、token 无效/已吊销 401 `invalid_token`、
  无检索授权 403 `search_not_granted`、模型/JSON 不符 403 `model_not_granted`、配额用尽 429 `quota_exceeded`、上游不可达 502。
* **token**：每个 run 一个（`pge_` + 32 字节随机），绑定 `run_id`、tenant 与冻结模型 id（以及该 run 解析出的真实
  key），**只存宿主内存**（按 sha256 摘要索引）。runner 在终帧提交时吊销（此时内核已退出）；宿主重启
  使全部 token 失效。token 是本 run 已知秘密，并入本 run 的脱敏边界。
* **渲染**：`models.json` 的 `baseUrl` 为 `<egress origin>/model`（字面量；不含 `apiKey`）；`auth.json`
  为 `{"frontier":{"type":"api_key","key":"<token>"}}`；有检索 grant 时 `web-search.json` 为
  `{"exaApiKey":"<token>","exaBaseUrl":"<egress origin>/search"}`，否则删除该文件（插件回退到无密钥的
  Exa MCP 检索，不是错误）。旧的 `GLINK_WEB_SEARCH_CONFIG` 与操作员 `~/.pi/web-search.json`
  **不再读取**。
* **执行面地址**：inproc 为 `http://127.0.0.1:<port>`；Docker 为 `http://host.docker.internal:<port>`
  并加 `--add-host host.docker.internal:host-gateway`；E2B 没有宿主可达路径，**必须**配置
  `GLINK_EGRESS_PUBLIC_URL`（由部署把该 URL 转发到固定的 `GLINK_EGRESS_PORT`），缺失时启动即
  `ConfigError`（fail closed，绝不退回渲染真实 key）。配置了 public URL 时任何执行面都优先使用它。
  pi-web-access 拒绝非 loopback 的 http 检索地址，所以 Docker 面带 `GLINK_WEB_SEARCH_KEY` 时同样需要
  https 的 public URL。
* **计量**：代理读取上游自己的 OpenAI 兼容 `usage`（JSON 整体或 SSE 最后一个带 `usage` 的块），按 run
  累计。终帧与 run 记录里 pi 族的 `usage` 取代理计量（`source: "egress"`，含 `requests`、
  `searchRequests`、`input`、`output`、`cacheRead`、`totalTokens`、`unmetered`、`refused`），内核自报的
  用量另记 `kernelUsage`，不是权威。唯一的策略钩子是按租户的每 run 配额
  （`GLINK_EGRESS_MAX_REQUESTS_PER_RUN` / `GLINK_EGRESS_MAX_TOKENS_PER_RUN`，0 = 不限）。
* **ACP 族未覆盖**：ACP agent 仍按 [ADR 0003](adr/0003-acp-credential-copies-removed-at-close-out.md) /
  acp-integration-notes §18 注入凭据副本并在收尾删除；把 ACP 接入出口代理是未决项。注入通道自
  [ADR 0015](adr/0015-acp-credential-channels-under-model-credential-policy.md) 起服从模型凭据政策：
  多租户 tenant 政策下配置通道即拒绝启动、ACP run 在准入拒绝（租户级注入点是 M2 未实现）；shared
  下通道是 ACP 宿主的具名凭据源，缺源不启动。
* 浏览器永远拿不到密钥：`/api/config` 只返回模型名与是否已配置。

### 6.1 出站脱敏边界

宿主持有凭据，而 pi 的工具**可以**把它们回显出来：`bash` 能跑 `env`、`read` 能打开运行期
配置文件、模型也可能把值引用回回答。因此每一道产品出口都经过
`host/glink_harness/redaction.py` 的 `SecretRedactor`：

| 出口 | 处理 | 代码 |
|---|---|---|
| 事件投影 | 载荷里每个字符串递归脱敏；delta 走尾部暂存（§3.3） | `kernel/projection.py::project_event(frame, redactor)`、`redaction.py::StreamRedactor` |
| 事件总线 / `events` 表 | 提交（先落盘）前再脱敏一次 | `runs/channel.py::RunChannel.emit` |
| 报告文件 | 经工作区目录句柄读取、脱敏、在 run 目录句柄内原子写入 `runs/<id>/artifacts/report.md`，并记 `redacted=true` | `runs/stages.py::SealStage.report` |
| 声明交付文件 | ACP `present` → `deliverables_declared`（声明≠交付）；封存最多 8 个工作区相对路径，逐件校验/脱敏，终帧与记录携带逐件结果。E2B 优先取主报告与声明文件再取其他直系子文件，64 MiB 总量不变 | `kernel/acp_projection.py`、`runs/channel.py`、`runs/stages.py`、`kernel/e2b_env.py` |
| 会话文件 | pi 自写的 JSONL 在收尾时脱敏；只允许本次 run 会话子目录内的普通文件（拒绝链接/越界），Docker 面先把容器路径 `/sessions/…` 映射回该挂载源；子代理分支会话与 `subagent-artifacts/` 同目录被同一边界清扫 | `runs/secrets.py::RunSecretBoundary.sanitize_session_file`、`sandbox.DockerSandbox.host_path`（`docker_env.translate_container_path`） |
| ACP 状态根 | 收尾扫描范围与处置由运行时契约声明：agent 自己的会话存储（`acp-agent/home`，E2B 历史回收的 `acp-agent/recovered`）**只读扫描**——凭据形状值并入本 run 边界，文件字节不改写（改写会破坏追问要续接的会话，与 §10.2 继承口径同一义务）；对话状态根（`conversations/<根run>/state`，0018）的凭据形状值在**回收时**吸收（只扫本轮变化的文件，同一预算），不在收尾重复改写；**注入凭据副本**（`GLINK_ACP_CREDENTIAL_FILES` 的 dest）在扫描之后逐个删除（只删已知相对路径：不按模式匹配、不跟随链接、必须在根内；追问会从源重新注入，继承与归档都 skip 这些目标）；删除失败记入终帧 `artifactFailures` 并告警，不改变终态；缺失的根按「无可扫之物」处理 | `kernel/sweep.py::SessionSweep`、`kernel/acp_env.py::AcpRuntime.session_sweeps`、`runs/secrets.py::RunSecretBoundary.seal_sessions`、`absorb`、`remove_credential_copies`、`kernel/acp_e2b_env.py::_scan_published_secrets`、`artifacts.py::ArtifactTree.remove` |
| 子代理镜像 | E2B 面回收的 `runs/<id>/subagents/`（分支会话 + 产物）收尾时经目录句柄清扫，与 §5.2 同一条边界 | `runs/secrets.py::RunSecretBoundary.sweep_tree`（`seal_trees`）、`kernel/e2b_env.py::recover_subagent_files` |
| 子代理文档 | `runs/<id>/children/<n>/child.json` 只由已过两道脱敏的事件载荷构造（投影 → 提交前再脱敏），`GET /api/runs/{id}/children` 按存储原样下发，不引入新秘密；`children/<n>/<file>` 产物下载按报告下载同一套规则（服务前再脱敏、`X-Redacted`） | `runs/channel.py::RunChannel.emit`、`runs/plugins.py::ChildDocumentsPlugin`、`store.py::read_children`、`web_routes.py` |
| 报告下载 | `report_path` 不是下载能力：只认一个固定位置——宿主产物副本 `runs/<id>/artifacts/report.md`；工作区 `workspaces/<id>/report.md` 及其他路径一律视为无报告（[ADR 0007](adr/0007-report-served-only-from-host-artifact.md)）。服务前再脱敏，响应头 `X-Redacted` 标明是否发生；`hasReport` 用同一位置规则与同一拒绝条件计算 | `store.py::report_location`、`web_routes.py::_send_report`、`artifacts.py` |
| 补充交付文件下载 | `GET /api/runs/{id}` 的 `deliverables` 给逐件状态/可用性；`GET /api/runs/{id}/deliverables/{index}` 只认成功封存的宿主产物副本，不把路径当能力。owner 门禁→位置/大小校验→本 run 再脱敏，同形 404 与 `X-Redacted`，见 [ADR 0017](adr/0017-declared-deliverables.md) | `runs/read_model.py`、`store.py::deliverable_location`、`web_routes.py` |

**产物路径安全**（`artifacts.py::ArtifactTree`）：宿主从不「先 realpath 校验、再按路径打开」。
每次操作持有 run 根目录的目录句柄（根的父目录是宿主配置，可信；根本身及其下每一级都以
`O_NOFOLLOW` 逐级打开），叶子只接受普通文件（FIFO 以非阻塞方式打开后拒绝），打开前后身份须
一致，打开后再从根重新核对每一级目录项；写入在已核对的父目录句柄内建临时文件并 `renameat`
发布。任一祖先在校验与打开之间被换成链接，读写都会被拒绝，外部文件既不被读取也不被改写。同一原则
也约束沙箱侧回收：E2B 工作区列表里的目录与符号链接条目按 SDK 的 `EntryInfo.type` 跳过（不跟随、
不解析、不记为失败），只有普通文件进入宿主的这份目录句柄校验。

检测同时覆盖**值**（宿主知道的 key）与**形状**（`sk-…`、`e2b_…`、`exa-…`、`NAME=value`、
`Bearer …`），所以宿主从未见过的第三方 token 也会被掉包；跨 delta 的分割由 §3.3 的暂存覆盖。
`scripts/check-no-secrets.py` 用同一套规则扫描运行态产物，并把**文档占位符**
（如 README 里的 `sk-ant-...`）与真实值分开计数。run 数据库经 SQLite 只读打开，逐行扫描
`runs.record` 与 `events.body` 的完整值（WAL 由 SQLite 合并）；不把 `runs.db` / `runs.db-wal`
当文本扫描——大值会被拆到 overflow page、WAL 也按页组织，跨页的凭据在字节层面会被截断而漏报。
数据库无法打开时扫描判失败，不报告 0 命中（`tests/test_check_no_secrets.py`）。

**脱敏不是安全沙箱**：它处理的是「宿主已能把值交付给内核、内核又把它回显到产品面」这一层的
回显保护；任意编码、分拆到不同工具输出、写入二进制或直接网络外传不在承诺范围。需要更强隔离
时应使用 Docker / E2B 执行面，并为内核配专用、限额的凭据；多租户的每 run 凭据渲染与脱敏边界
见 §10.3，配置与迁移见 `docs/multitenancy-plan.md`。

### 6.2 输入侧最小权限

pi 子进程拿到的不是宿主 `os.environ` 的副本：`kernel/agent_env.py` 只放行
PATH / HOME / 代理 / 证书等运行必需项，再注入指向运行期配置目录的 `PI_*`。宿主 shell 里其他
凭据（例如 `ANTHROPIC_API_KEY`、`AWS_SECRET_ACCESS_KEY`）不会进入 pi 进程，模型 key 更不在
环境或运行期文件里（回归：`tests/test_redaction.py::test_minimal_child_environment_does_not_inherit_host_credentials`、
`tests/test_kernel_agent_env.py::test_only_the_egress_token_reaches_auth_json_not_the_environment`、
`tests/test_egress.py`）。

**残留风险（如实记录）**：run 进行中出口代理 token 以文件形式存在于运行期 `auth.json` /
`web-search.json`（0600、仓库外），内核工具可以读到并在 run 期间直接调用代理——它只能访问宿主冻结的
两个上游、受本 run 配额约束、终帧提交时失效，且不暴露真实 key。token 回显仍由 §6.1 出站脱敏覆盖。
执行面透传的 `HTTP(S)_PROXY` 若不排除代理地址，可能让内核到代理的请求绕行；越过 token 配额的那一次
请求仍会完成（用量只在上游应答后可知）。三面的历史凭据交付证据见
`docs/evidence/{credential-boundary,docker-run,e2b-run}.txt`（出口代理之前的形态，不证明本设计）。

## 7. 目录边界

| 路径 | 属于 | 说明 |
|---|---|---|
| `host/glink_harness/` | 宿主 | `config` / `events` / `store` / `tenancy` / `ownership` / `static_files` / `redaction`（出站脱敏）/ `accounts`（站内账号）/ `auth`（身份断言与会话）/ `dotenv`（.env 解析）/ `blueprints`（蓝图目录推导与解析渲染）、`harnesses`（harness 包清单的目录推导、fail-closed 解析与选择）；组成根 `app`（服务器与 `create_server`） |
| `host/glink_harness/web_*.py` | 宿主 HTTP 层 | `web_routes`（路由与 run/health/报告 API）/ `web_sse`（事件流与连接额度）/ `web_security`（身份、会话与请求防护）/ `web_http`（请求体、响应、静态资源、脱敏访问日志）；一个 `HostRequestHandler` 由它们组合而成 |
| `host/glink_harness/cli.py` | 宿主 CLI | 参数与 `main`、离线运维子命令（`--unreleased` / `--mark-released` / `--create-invite`） |
| `host/glink_harness/kernel/` | 接入层 | pi 族：`rpc` / `framing` / `projection` / `agent_env` / `subagent_artifacts`（pi-subagents 产物布局解析与关联，R2）；ACP 族：`acp_rpc` / `acp_projection` / `acp_env` / `acp_execution`；执行面：`execution` / `outcome`（执行 outcome 类型与 driver 线程适配器的封存基类）/ `docker_env` / `e2b_env`（pi 面 E2B 沙箱与共享的沙箱读写助手，含 resume/kill-by-id）/ `acp_e2b_env`（ACP 面 E2B 沙箱与后台命令的 stdio 桥接）；对话状态基础设施：`archive_channel`（0018 归档通道）/ `conversation_lease` + `acp_sandbox_park`（0020 对话租约事实与收尾泊车）；两族的 run 生命周期策略：`harness` / `harness_pi` / `harness_acp`。不导入 `runs/` 或宿主编排模块 |
| `host/glink_harness/runs/` | 编排层 | run 准入与执行骨架、收尾顺序、终态提交与资源台账：`runner`（RunManager 公共 API 与准入锁）/ `admission`（两段式准入）/ `pipeline` + `stages`（生命周期管线与各阶段）/ `state`（阶段事实类型）/ `channel`（事件提交路径）/ `secrets`（run 秘密边界）/ `plugins`（子代理文档/进度/增强插件）/ `errors` / `limits` / `resource_ledger`（外部资源台账、重启对账、人工裁决、在线身份探测扫描与对话租约对账/删除闭环，0020）/ `child_progress_observer`（R3a 每 run 进度观察者） |
| `host/glink_harness/artifacts.py` | 宿主 | 内核可达文件的目录句柄锚定读写（逐级不跟随链接、打开后复核、限幅、目录内原子替换） |
| `harnesses/<pack>/` | Harness 包 | `harness.toml`（`glink-harness/v2` 清单：`family`、`[entry].command`、`[capabilities]` 能力矩阵、`[image]` 镜像需求与上游包精确版本；Dockerfile ARG、`docker/e2b/templates.json` 与 pi 的 `settings.json` 包列表由 `scripts/generate-pack-artifacts.py` 从它生成）、可选 `capabilities.md`（能力提示，拼入系统提示）、ACP 包的 `home/`（每 run HOME 覆盖层，先于凭据注入）；pi 包另有 `settings.json`、`agents/researcher.md`（前台派发的取证子代理）、`extensions/subagent-foreground-guard.ts` 与 `extensions/subagent/config.json`（强制前台 + 最保守配置） |
| `web/` | 前端 | 外壳与事件视图（无打包步骤，原生 ESM）；视觉按百鉴 UI 标准库逐值对齐，见 `docs/ui-alignment.md` |
| `docker/` | 执行面 | `Dockerfile.worker`（pi 容器）、`Dockerfile.acp-worker`（ACP 容器）、`e2b/Dockerfile.e2b`（pi 模板）、`e2b/Dockerfile.acp-e2b`（ACP dev 模板） |
| `methodology/` | 方法论（harness 无关，唯一真源） | `work-agreement.md`（工作约定）、`blueprints/*.md`（蓝图：阶段/产出契约/检查点），格式契约见 `docs/blueprint-templates.md` |
| `scripts/` | 运维 | 启动、测试、真实 run 冒烟、E2B 模板构建；`spikes/`（ACP 各面的 live 探针） |
| `docs/` | 文档与证据 | 架构、边界、集成笔记、`evidence/` |
| `tests/` | 回归 | `test_*.py`（宿主/内核）、`web/*.test.mjs`（前端模型）、`browser/`（真实浏览器） |
| `<data_root>`（默认 `~/.glink-harness`） | 运行态 | `host.lock`（宿主独占所有权）、`runs/runs.db`（run 记录与事件，SQLite WAL，含 `-wal`/`-shm`）、`runs/<id>/{pi-agent/ 或 acp-agent/,sessions/,artifacts/,subagents/,children/}`（按本 run 的 harness 渲染；`subagents/` 为 E2B 面回收的子代理镜像；`children/<n>/child.json` 为子代理权威文档，§3.5）、`conversations/<根run>/state/`（ACP 对话级权威状态与 manifest，0018）与 `conversations/<根run>/lease.json`（对话沙箱租约，0020）、`workspaces/<id>/`；目录由宿主以 0700 创建，已存在的符号链接或组/其他人可写目录拒绝启动（不自动 chmod） |

运行期配置目录 `runs/<id>/pi-agent/` 是 pi 的 `PI_CODING_AGENT_DIR`：宿主在其中渲染
`models.json` 与 `web-search.json`，复制 `harnesses/pi/` 的可版本化配置，pi 自己往里写 `auth.json` /
`models-store.json`。仓库里的 `harnesses/pi/` 只是模板，永远不作为运行目录使用；`pi-agent/` 不共享，
所以并发 run 不会互写凭据或扩展。ACP 族的对应物是 `runs/<id>/acp-agent/home`（子进程的
`HOME`）：先铺 `harnesses/<pack>/home/` 覆盖层、再只读复制注入凭据（凭据目标优先，不被覆盖层
覆盖）且值并入该 run 脱敏边界；**run 收尾在只读扫描之后按 `credential_dests` 删除宿主
一侧的注入凭据副本**（`home` 与历史 `recovered` 两处，[ADR 0003](adr/0003-acp-credential-copies-removed-at-close-out.md)）；e2b 面在沙箱销毁前回收并发布到
对话状态根 `conversations/<根run>/state`（0018：归档通道按 offset 增量，凭据目标不进归档，
manifest 记 `sourceRunId`；历史 run 的 `acp-agent/recovered` 保留为兼容继承源），追问时经归档
通道回灌新沙箱（通道不可用降级宿主侧继承 + files 逐文件）。追问继承哪一个状态根按父 run 记录的 `executor`
推导（e2b→对话状态根，其余→`home`），不随宿主当前执行面配置漂移（[ADR 0002](adr/0002-follow-up-judged-by-parent-record.md)）。归档通道部署下，e2b 面 ACP run 收尾在回收发布成功后**泊车**（`pause(keep_memory=False)`）而非销毁：沙箱内凭据副本先删（凭据不随沙箱持久化），状态代数标记与对话租约（`conversations/<根run>/lease.json`，[ADR 0020](adr/0020-conversation-sandbox-lease-hot-path.md)）成对落盘，终帧 `cleanup.lease` 记录泊车事实且**不构成 resource hold**；追问优先重连该泊车沙箱（热路径，跳过回灌，`session/resume` 照常新起 agent 进程），代数双端不一致或重连失败一律降级阶段 1 冷路径（新沙箱 + 回灌），降级发 `kernel_status` 警告；闲置 TTL（`GLINK_CONVERSATION_LEASE_TTL_SECONDS`，默认 7200s、0 禁用、硬上限 24h）到期由资源对账 sweep 按沙箱 id fail-closed 销毁，删除对话走 `RunManager.delete_conversation` 最小闭环（销毁租约沙箱 + 清理对话状态对象；run 记录与事件另属产品面）。

宿主 HTTP 层的职责边界：路由只决定请求含义（`web_routes`），身份与请求防护集中在
`web_security`，事件流与连接额度在 `web_sse`，字节读写与静态资源在 `web_http`；`app.py` 只做
组合与生命周期（数据目录锁、租户材料、run 对账、绑定端口、资源台账扫描线程），CLI 入口与
离线运维子命令在 `cli.py`。拆分是纯结构调整：路由、状态码、载荷与既有导入路径（含
`glink_harness.app.main` / `load_config` / `_resource_command` 再导出）保持不变。

`/` 命令门禁本身在**准入层**（`runs/runner.py::RunManager.start`，斜杠命令与蓝图名从
`config.blueprints_dir` 推导，harness 无关），路由只负责把
`UnsupportedCommand` 映射成 `400 unsupported_command`：这样任何调用者（HTTP、脚本、未来的适配器）
都逃不过，且追问与三个执行面走同一条判定。拒绝发生在读父会话、写 run 目录、拉起执行面之前。

## 8. 可重复验证入口

```bash
bash scripts/check.sh                                  # 默认无模型交付检查（schema + ruff + mypy + Python/覆盖率 + Node）
scripts/run-tests.sh                                   # 仅 Python 的低层入口
node --test tests/web/*.test.mjs                       # 前端事件模型 + 导航生命周期回归
scripts/check-ui-tokens.py                             # token 层与百鉴标准库逐值比对（个人标准库不进仓库：`--library <repo>` 或 `GLINK_UI_LIBRARY=<repo>` 显式指定，缺省 skip）
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

glink 不再规划独立账号、订阅计费、项目资料库、业务成果工作区或第二套产品历史的扩建；这些能力
归 ULegal。多宿主、共享配额与对象存储若有实际需求，在产品后端侧单独决定，不沿旧计划继续建设
独立 glink 控制面。当前单宿主多租户实现及未覆盖项仍见 §10 与 [多租户方案](multitenancy-plan.md)。

本次方向确认不授权执行代码重构、数据库退出、真实历史导入、共享服务重启、模型/云端调用、
镜像或模板切换、发布与部署。相关操作分别遵循 [工程约束](engineering.md) 和实施验收。

## 10. 单宿主多租户（已实现，合成验证）

默认 `GLINK_TENANCY=local`；宿主任一模式都只绑定 loopback（见 §10.5 的行为迁移）。开启
`multitenant` 后身份入口由 `GLINK_AUTH_MODE` 选择：`gateway`（默认）部署在本机认证网关之后，
由网关注入身份断言；`accounts`（§10.6）在宿主内完成邀请注册、登录与服务端会话。模型凭据来源另由
`GLINK_MODEL_CREDENTIALS` 显式选择：`tenant`（默认，每租户自己的 key 文件）或 `shared`
（SaaS 策略，宿主冻结一套模型凭据供所有租户使用，见 §10.3）。`local` 模式没有身份入口；浏览器来源检查与 JSON-body API 的媒体类型门禁在所有模式一致执行。

### 10.1 身份与请求授权

两种身份入口（gateway / accounts）都只从服务端事实推导 `TenantContext`，下游的
owner 校验、配额、凭据与脱敏边界完全共用；客户端在任一模式下都不能用请求体、查询参数或自造
请求头指定租户。

- 断言：`X-Glink-Harness-Identity: v1.<base64url(payload)>.<base64url(hmac-sha256)>`，payload 至少含
  `tenantId`、`userId`、`role`、`audience`、`iat`、`exp`；宿主用仓库外 0600 密钥文件做 constant-time
  HMAC 校验，并检查 audience、过期与寿命上限（默认 300 秒）。重复断言头直接拒绝——这是宿主侧的
  “网关必须清除客户端同名头”。
- 请求体、查询参数与其它请求头不参与身份推导：多租户下除 `/api/health` 外所有 `/api/*` 先验签；
  未认证 401，权限不足 403，未知/异租户/无 owner 的记录统一同形 404。
- 事件流在写出任何响应头之前用同一 owner 规则验权；断言过期在**重放与实时两个阶段**都按帧检查：
  持久 `events` 表的每一帧、实时订阅的每次等待都先确认断言未过期，过期即干净结束该流（实时
  等待用 `EventBus.subscribe(idle_poll=…)` 返回控制流，不改变持久事件语义）。过期没有宽限窗口：
  通过验证的上下文绝不会已经过期，`clock_skew` 只容忍签发时间略快于宿主时钟的网关。
- 所有模式（含 local）的 POST 检查 `Origin` / `Referer` / `Sec-Fetch-Site`：任何表示跨站的元数据都会在启动执行面之前被拒绝；
  可信来源仅取实际监听端口上的 loopback origin（`127.0.0.1` / `localhost` / `[::1]`，HTTP）
  与冻结的 `GLINK_PUBLIC_ORIGIN`；请求自带的 `Host` / `X-Forwarded-*` 不扩大信任范围。
  反代的外部浏览器 origin 必须通过已有的 `GLINK_PUBLIC_ORIGIN` 配置，不依靠转发 Host。
  无这些头的非浏览器客户端（网关、运维脚本）不受影响。所有 POST 在路由/认证/副作用前拒绝
  任何 `Transfer-Encoding`（411，`Connection: close`，不尝试按 Content-Length 排空 chunk 字节）；
  `POST /api/runs` 在认证/CSRF 后、读取体之前要求 `application/json`（否则 415）；真正无体、
  无 Transfer-Encoding 的 stop 维持原调用方式。

### 10.2 owner 与历史数据

`RunStore.create` 在受理时于同一事务内写 `tenant_id` / `creator_user_id`；`get_for` /
`list_for` 按 tenant 过滤。没有 owner 的记录（单用户面写入）不匹配任何租户，因此在多租户读面上始终不可见/不可下载/
不可 SSE/不可取消，也不会被迁移或改写；`local` 模式仍按旧口径可读。取消在同一把 run 锁内先做
owner/role 判定再受理：其他租户得到与未知 run 相同的 404 且无副作用，同租户非创建者的 member 得到 403。

多轮追问遵循同一规则：`parentRunId` 的父 run 必须先通过租户门禁，未知/异租户/无 owner 的 parent 统一
返回 `unknown_run`（404），且在所有者得到确认之前不读取它的状态、会话文件、报告或子 run 计数；
先读后判会让“运行中/已有后续轮次”的父 run 以 `run_busy`/`stale_run` 泄露存在性。同族判定在租户门禁之后、状态检查之前比父记录的 `harness` 字段：父记录带该字段且与本次请求的 harness 不等时，对本租户调用者拒绝为 `session_unavailable`，不读取其会话/状态根；父记录无该字段的旧数据维持原有证明路径（pi 族不加新拒绝，ACP 族由事件流 `session_ready` 帧的 harness 标记证明）。同租户父 run 的
`session_unavailable`、`stale_run`、`report_unavailable` 等具体拒绝保持可解释；子 run 始终继承调用者
自己的 owner、凭据与私有会话/工作区，复制的是已脱敏的会话副本与上一轮报告。

### 10.3 凭据与脱敏边界

租户凭据文件（仓库外、0600）在默认 `GLINK_MODEL_CREDENTIALS=tenant` 策略下为每个租户提供
`modelApiKey`（必需）与 `webSearchKey`（可选）。解析时 `RunCredentials(origin="tenant")` 只允许本租户
的值：缺模型 key 拒绝准入，绝不回退宿主 `.env` 或别的租户；解析出的真实 key 只进入本 run 的出口代理
grant（绑定 `run_id` 与 tenant，§6），执行环境只拿到 token；缺搜索 key 时不签检索授权（删除
`web-search.json`，无 key 检索）。E2B 平台 key 属于宿主资源管理，不是租户模型 key。
每 run 构造一个只包含“宿主已知秘密 + 本 run 凭据”的 `SecretRedactor`，事件投影、提交、报告拷贝、
工作区/会话清扫与报告下载都用它，因此不同租户的秘密不会互相写入对方的边界或产物。工具能读到自己
本 run 的 token（§6.2 残留风险），但读不到任何租户的真实 key；脱敏只做回显保护，不承诺阻止任意编码或网络外传。
代理按 grant 计量用量并执行每租户每 run 配额钩子；多宿主进程之间不共享配额。

显式 `GLINK_MODEL_CREDENTIALS=shared` 是 SaaS 部署的**显式放宽**，不是任何模式的默认兜底：

* 宿主必须在启动时冻结 `OPENAI_API_KEY`、`OPENAI_BASE_URL`、`OPENAI_MODEL`，任一缺失即拒绝启动；
* 每个 run 解析 `RunCredentials(origin="shared")`，即同一套宿主共享凭据（同样只进入出口代理 grant）；共享模式不读取任何
  租户配置：不得配置 `GLINK_TENANT_CONFIG_FILE`（不设置或留空即为无该入口），设置即拒绝
  启动，也不会构造或读取 `TenantCredentialStore`；缺共享 key 时也不会回退到 `tenant` 或 local；
* 可选搜索 key 只来自宿主级 `GLINK_WEB_SEARCH_KEY`（操作员 `~/.pi/web-search.json` 不再被读取）；
* 租户授权仍完全由身份入口决定（网关注入的断言或账号库会话）；共享模式没有租户凭据文件入口，
  也不存在一个可以准入或拒绝租户的文件名单；
* 每 run 脱敏边界仍包含共享 key（它属于宿主已知秘密）；`redactor_for_record` 在 shared 下使用
  宿主/共享边界，不会因为共享模式没有租户凭据条目而漏脱敏，也不会拿其他租户的凭据去清洗共享内容；
* `tenant` 与 `shared` 的差异只限于“凭据从哪里来”：owner 过滤、每租户配额、审计可见性与
  `tenant` 模式完全一致；run 详情/列表/事件/报告从不回显模型凭据。

### 10.4 准入与并发

只有多租户 + Docker/E2B 可并发：默认全局上限 4、单租户上限 2；准入检查与槽位预留（写入执行表 +
打开事件流）在同一把 manager 锁内完成，超额立即返回 `429 quota_exceeded`（带 scope 与 limit，不含
任何 run id），不排队。未确认释放的资源持有**计入槽位**（owner 租户 + 全局；无 owner 的记录与
损坏记录只计全局），因此一个租户的泄漏不会挡住别的租户，但会持续吃掉额度；持有达到全局上限时任何
新准入都被拒绝（fail-closed）。恢复额度靠宿主在线自动对账或离线 `--unreleased` / `--mark-released`
（§4）。已受理的取消仍与终帧提交在同一线性化点上裁决。`inproc` 在两种模式下都维持全局单 run；
多租户配置直接拒绝 `inproc`（它共享宿主文件系统）。

SSE 连接同样有界：全局 `GLINK_MAX_GLOBAL_STREAMS`（默认 64）与每租户
`GLINK_MAX_TENANT_STREAMS`（默认 16），超额在写出流响应头之前返回 `429 stream_limit`（§3.2）。

Docker 面的每个容器实例额外带 `--cpus` / `--memory` / `--pids-limit`（默认 2 / 2g / 512，可配置）与
`--cap-drop ALL` / `--security-opt no-new-privileges`，镜像与模板不变。E2B 面只能限制沙箱生命周期
（`LOCAL_WEB_E2B_TIMEOUT_SECONDS`，默认 task timeout + 300 秒）；SDK 不暴露 CPU/内存控制，因此宿主
只声明自己实际传给云端的限制，不把未请求的限制当作已执行。追问热路径的泊车沙箱另受闲置租约 TTL
约束（`GLINK_CONVERSATION_LEASE_TTL_SECONDS`，默认 7200 秒、0 禁用、硬上限 24 小时，[ADR 0020](adr/0020-conversation-sandbox-lease-hot-path.md)；
paused 沙箱的云端保留期与计费模型在 SDK 内无文档），数量另受全宿主上限约束（`GLINK_MAX_PARKED_SANDBOXES`，
默认 32；只数其它对话仍未到期的租约，本对话的重新泊车是替换；损坏租约按占用计。计数与写租约在同一个全宿主预留临界段内完成（纯本地文件读写，不含云调用），并发泊车不能同时占用最后一个名额），达到上限时本轮不泊车、按冷路径销毁，本对话的旧记录随即结清（全部确认销毁即删除，否则以已过期记录保留待对账重试，不再占名额）。两者都未在本次交付中做 live 验证。

**共享宿主的资源上界**（源码与合成验证，未做压力实测）：内核 stdout 的未结束记录超过
64 MiB（`framing.MAX_RECORD_BYTES`）即中止读取并杀掉进程，run 以 `kernel_exit` 失败并注明协议违规；
E2B 命令输出经有界队列（每路 256 块）交给宿主读取线程，读取落后时反压 SDK 迭代，kill 后不再阻塞；e2b SDK 的 `CommandHandle` 自身累计全部输出并在结束时拼接进结果，宿主只读退出码，因此每转交一块即清空 SDK 的累计缓存（仅当该内部字段存在且为 list）；
Web 处理器每次 socket 读写有 60 秒期限（`REQUEST_SOCKET_TIMEOUT_SECONDS`），空闲 keep-alive、停滞的请求体与
停止接收的 SSE 客户端都会释放线程（空闲的 SSE 不写数据，不受影响）。这是单次操作期限：持续滴流的慢速客户端
仍能占住连接，完整的 slowloris 防护需由反代的连接/读取期限提供。

### 10.5 行为迁移、回退与未覆盖范围

- **迁移**：任何模式都不再允许绑定非 loopback：无认证公网模式被移除，历史的 `LOCAL_WEB_ALLOW_REMOTE`
  不再生效；远端访问必须经过本机认证网关注入身份（`GLINK_TENANCY=multitenant`）。这是本次交付
  明确的行为收紧。该边界与整数配置范围（端口 0–65535、并发/超时/限速/资源上限为正等）在
  `validate_host_config` 里统一校验，构造与 CLI 的 `--host`/`--port` 覆盖都经过它。
- **回退**：关闭 `GLINK_TENANCY` 回到单用户模式；新旧记录都保留，不需要删除、迁移或改写；
  多租户密钥文件也不再被读取。
- **前端不回退**：`/api/me` 除 401 外的任何失败（含 404、网络错误）一律 fail-closed（显示不可达面、
  不探测 `/api/config`），本地工作台绝不会在身份不明时被揭示。先合并前端、后重启宿主期间的旧
  `local` 宿主不再兼容，更新后须重启宿主。
- **未覆盖**：真实网关注入、密钥轮换与撤销；真实 Docker/E2B live 资源上限、沙箱模板的实际隔离强度；
  多宿主租约与共享存储；BYOK、ACP 族接入出口代理（pi 族已接入，§6）、计费与上线运维。合成矩阵通过不等于多租户已经安全上线。

### 10.6 站内账号身份入口（`GLINK_AUTH_MODE=accounts`）

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
  `python -m glink_harness.app --create-invite …` 离线签发（默认策略要求该租户已有模型凭据；`shared`
  策略只校验宿主冻结的共享模型配置，不配置租户凭据文件）；后续管理员通过
  `POST /api/admin/invitations` 只给本租户签发，邀请码原文只在成功响应里出现一次。
- **请求防护**：状态变更 POST 要求 `application/json`、同源 `Origin`/`Referer`/`Sec-Fetch-Site`；
  已登录会话还必须携带与 cookie 绑定的 `X-Glink-Harness-CSRF`（`GET /api/me` 下发，只存在页面内存）。
  `gateway` 模式忽略会话 cookie，`accounts` 模式忽略断言头，登录失败不会回退到 gateway/local；
  缺本租户模型 key 仍拒绝启动 run（§10.3）。
- **未验证**：真实 HTTPS/TLS 代理与外部 Origin、邮件交付、密码重置、账号/租户停用运维入口、生产
  数据迁移与多宿主。合成矩阵（临时账号库、随机 loopback 端口、fake 执行面、真实浏览器手动验收）
  不等于账号模式已通过生产安全验收。
