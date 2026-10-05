# 执行服务 HTTP 契约（glink-harness ↔ 产品后端）

> 本文把 ULegal 对接设计（§2.1 受理面、§3 事件/终态回传、§6 取消语义）固化为可验收的
> 服务契约，供 R2 产品后端（ULegal）消费。R1-5 只固化**现状行为**，不改实现；每条契约
> 的钉住测试见 §5 对应表（新增条款测试在 `tests/test_exec_contract.py`，docstring 以
> 「契约 x.y」开头；部分条款由其他测试文件的既有用例钉住，§5 已列出位置）。
>
> 单一来源约定：**事件词汇、payload 字段与版本 `v` 的唯一来源是
> `host/glink_harness/event_schema.py`**（前端常量由它生成，`scripts/gen-event-schema.py
> --check` 钉住）；本文不复制事件字段表，只固化 HTTP 面的语义。实现语义见
> [architecture.md](architecture.md) §3（事件契约）与 §4（run 生命周期）。

## 1. 受理面：`POST /api/runs`

### 1.1 请求体字段

请求体为 JSON 对象，只解释以下三个字段：

| 字段 | 类型 | 语义 | 约束 |
|---|---|---|---|
| `prompt` | string 必填 | 本轮题面；`/蓝图名 题目` 等价于 `blueprint`+`prompt`（宿主准入层解析） | 非字符串、空白串 400；以 `/` 开头但不是蓝图命令 400 `unsupported_command` |
| `parentRunId` | string 可选 | 追问的父 run id | 须匹配 `^[A-Za-z0-9_-]{1,64}$`，否则 400 |
| `blueprint` | string 可选 | 蓝图名 | 非字符串 400；不存在/不可用 400 `unknown_blueprint`/`invalid_blueprint` |

**模型、凭据、harness/executor 选择均不接受客户端字段**：

- 禁用字段（`FORBIDDEN_RUN_BODY_FIELDS`，匹配大小写与首尾空白不敏感）一律
  `400 invalid_request`，响应体 `fields` 列出被拒字段名，且在创建任何 run 目录之前拒绝：
  `model`、`modelid`、`provider`、`apikey`、`modelapikey`、`baseurl`、`openaiapikey`、
  `openaibaseurl`、`openaimodel`、`websearchkey`、`credentials`。
- 其余未识别字段被忽略。`RunSpec{family, pack, executor, blueprint, credential_origin}`
  永远由准入从**冻结 HostConfig** 解析并随 run 记录原子写入；客户端字段不可能影响它
  （契约 1.1 的测试断言传入 `executor`/`harness` 等字段后记录 spec 仍是冻结配置值）。

### 1.2 成功响应

`201 Created`，体为 `{"run": <run 视图>}`。run 视图字段：

> 偏差说明：对接设计文档（§2.1/§4.1）写的是「200 + run 视图」，现状实现是
> `201 Created`（前端与既有测试已钉住 201）。**以本文为准**，消费方应按 201 判定成功。

| 字段 | 含义 |
|---|---|
| `runId` / `status` / `prompt` / `parentRunId` | 身份、状态（`running`/`finished`/`failed`/`cancelled`）、题面、父 run |
| `createdAt` / `finishedAt` / `durationSeconds` | 时间事实（`finishedAt` 未终态为 `null`） |
| `sessionId` / `stopReason` / `error` / `usage` / `redacted` | 会话与结论事实 |
| `hasReport` / `deliverables` | 报告与交付物清单（见 §3） |
| `eventCount` | 运行中取实时水位，终态后取持久化的终帧 `seq` |
| `blueprint` / `blueprintStages` | 准入时冻结的蓝图事实 |

run 视图不含任何宿主路径与凭据。

### 1.3 拒绝映射

| 响应 | error | 触发条件 |
|---|---|---|
| 400 | `invalid_request` | 禁用字段、字段形状违规、空 prompt |
| 400 | `unsupported_command` | `/` 开头但不是本机蓝图命令（体只给可用蓝图名，不回显输入） |
| 400 | `unknown_blueprint` / `invalid_blueprint` | 蓝图不存在 / 蓝图文件不可用（格式校验失败） |
| 400 | `not_configured` | 宿主缺凭据等配置错误 |
| 403 | `forbidden` | 跨站请求、租户未配置 |
| 404 | `unknown_run` | 追问的父 run 未知、异租户或无 owner（三者不可区分） |
| 409 | `run_busy` | 追问的父轮未终态；或 local 模式全局单 run 槽被占 |
| 409 | `stale_run` | 父轮已有后续追问（追问只允许接在最新一轮后） |
| 409 | `session_unavailable` | 跨族/跨包、能力缺失、会话或状态根不可用（fail-closed） |
| 409 | `resources_unreleased` | 历史 run 持有未确认释放的执行资源 |
| 409 | `idempotency_key_conflict` | 同一 `Idempotency-Key` 被用于不同请求（体不携带占用 run 事实，见 §1.4） |
| 415 | `unsupported_media_type` | 非 JSON 媒体类型 |
| 429 | `quota_exceeded` | 并发配额（体含 `scope`/`limit`，不含他人 run id） |

**`report_unavailable` 不是受理同步拒绝**：追问的报告种子复制发生在受理后的 Prepare
阶段；种子无法从父 run 安全读取时，追问 run 被受理后以 `run_failed` 收口，run 视图
`error` 载明 `report_unavailable`（fail-closed，不产生已续接假象）。消费方按 §2 事件
通道的终帧与 run 视图 error 识别，而不是等一个 409。

追问门禁次序固定：**owner（unknown_run）→ spec.family → spec.pack → capabilities.resume
→ terminal（run_busy）→ stale（stale_run）**，族追加门禁在其后。

### 1.4 创建幂等：`Idempotency-Key` 头

受理面接受可选 `Idempotency-Key` 请求头，让「创建响应丢失后的重试」不变成第二次执行：

- **形状**：`^[A-Za-z0-9._:@-]{1,128}$`。缺头是历史的非幂等创建（行为不变）；空头或畸形头是
  `400 invalid_request`，不静默忽略——自以为请求了幂等的客户端不得默默拿到非幂等创建。
- **作用域**：键属于已验证身份 `(tenant_id, user_id)`（local 模式为单 owner）。异租户或同租户
  异 user 的同键等价于键不存在：各自创建自己的 run，键不可用于探测他人是否用过。
- **请求同一性**：digest 盖三个原始请求字段 `prompt` + `parentRunId` + `blueprint`
  （`/` 命令重写之前；缺省与空串视为同一请求）。`/蓝图名 题目` 命令形式与等价的
  `blueprint`+`prompt` 字段形式虽受理为同一语义（§1.1 的确定性重写），对幂等是
  **不同请求**——digest 盖重写前的原始字段，同键互换得 409 而非重放。
- **重放**：同键同请求同样得到 **`201` + §1.2 的 run 视图、同一 `runId`**——不二次执行、
  不重新判定准入（当下的蓝图/凭据/配额状态不改变已受理事实）。重放响应是**该 run 当前
  状态的视图，不是首次响应的快照**：重放时 run 可能仍在运行也可能已终态，
  `status`/`eventCount`/`finishedAt` 等字段反映重放时刻的持久事实。因此运行中的同键
  重试不是 `409 run_busy`；宿主重启后同键同请求仍重放原 run（键是持久事实，不随进程消失）。
- **冲突**：同键异请求是 `409 {"error": "idempotency_key_conflict"}`，响应体不携带占用 run 的
  id、题面或状态。
- **持久性与原子性**：键行与 run 行在受理事务内原子写入——受理失败不留键；并发同键由 owner
  作用域唯一索引兜底：同进程内 prepare 窗口（准入锁外的文件 IO）竞速的孪生请求，败者的
  admit 失败被收口为对胜者的重放；跨写者的第二个 create 撞索引失败，宁拒不双执行。

## 2. 事件通道：`GET /api/runs/{id}/events`

### 2.1 帧形与次序

SSE 帧为 `id: <seq>` / `event: <type>` / `data: <信封>`；信封是
`{type, v, runId, seq, ts, payload}`，与事件日志一行同形。`seq` 每 run 单调递增。
流的合并次序：**先订阅实时尾，再重放持久日志，按 seq 水位去重**；已结束（含上一进程）
的 run 纯从事件表重放。`seq` 只作去重水位，不作展示排序依据。

### 2.2 续传

- `Last-Event-ID` 头优先于 `?after=<seq>`；两者都给出 seq 下界（严格大于），断流/重启后
  按 `(run_id, last_seq)` 游标精确续传。
- 非法游标（非整数）视为无游标，从 `seq 0` 全量重放；消费方按 seq 水位去重保证正确。

### 2.3 连接预算：429 `stream_limit`

超额在**写出任何流响应头之前**拒绝：`429`，JSON 体
`{"error": "stream_limit", "scope": "global"|"tenant", "limit": <n>}`，附
`Retry-After: 5`，体不含 run id、不透露他租户。服务端消费者（ULegal 观察 worker）能读
JSON 体，应退避重连；浏览器 EventSource 读不到该体，是其已知取舍。流的每条结束路径
（正常结束、客户端断开、断言过期、慢消费者断开）都归还额度。

### 2.4 可见性与身份

未知 run、异租户 run、无 owner 记录与 run 详情同口径 404 `unknown_run`，流不能用于探测
存在性。身份断言在流中途过期时流干净结束，客户端重新认证后按 §2.2 续传。

### 2.5 终帧集合与消费纪律

- 终帧集合恰为 **`run_finished` / `run_failed` / `run_cancelled`**
  （`event_schema.TERMINAL_TYPES`），与 run 状态一一对应；终帧是日志的**最后一帧且唯一**，
  终帧之后不再有任何帧（终态行在同事务拒绝再写）。
- `tool_updated.output` 是**累计快照**：每次替换而非拼接。
- `assistant_message` 可被截断（`truncated: true`）：截断帧同样以权威副本为准替换流式正文，
  内容块标记交付不完整，不保留更长的流式拼接。
- `assistant_message` 带 `turnScoped: true` 时（ACP 族）权威文本覆盖**整个回合**：消费方用它
  **合并替换本轮全部文本块**（正文段可能已被工具/plan 项夹断成多个块），而不是 pi 族逐
  message 的「替换最后一个未闭文本块」；缺省该字段的帧（pi 族及旧日志）保持逐 message 语义。
- `stopReason=length` 时 run 仍 `finished`，但 `stopReason`/`modelStopReason` 为
  `length`，表示**交付不完整**，消费方不得按普通完成呈现。
- `usage_reported.usage` 为 run 累计值。
- **消费方契约测试**：各消费方 fold 的一致性由 glink 权威事件样例库的契约测试钉住
  （[ADR 0021](adr/0021-event-fixture-contract.md)）——新事件形态先入样例库，消费方 fold
  对同一样例的输出快照必须一致，不过即红。

## 3. 报告与产物端点

### 3.1 `GET /api/runs/{id}/report`

再脱敏后返回 report.md：`200`，`Content-Type: text/markdown; charset=utf-8`，
`X-Redacted: true|false` 头标记本次是否发生脱敏。无报告（含被拒、种子 digest 相同、
宿主产物副本缺失）一律 `404 {"error": "no_report"}`。可见性口径与 run 详情相同。

### 3.2 `GET /api/runs/{id}/deliverables/{index}`

ACP `present` 声明的交付物，**至多 8 件**，`index` 取值 `0-7`（越界不成路由，404）。
只服务封存到宿主产物目录且 `status=ok` 的副本：`200` 字节流 + `X-Redacted` 头；
其余（声明被拒、副本缺失、未知 run）一律 `404 {"error": "no_artifact"}`。

### 3.3 run 视图的 `hasReport` / `deliverables`

- `hasReport` 与 report 端点共用同一位置规则与同一拒绝集：标志不会承诺端点拒绝的文件。
- `deliverables` 是声明清单（含被拒项与原因），每项附 `available` 标记（副本是否可读）。
- **种子 digest 规则**：追问轮会把父轮报告复制进本 run 工作区作为修订上下文；收尾时
  工作区报告与种子 digest 相同则视为**本轮没有新报告**——`hasReport=false`，report
  端点 404。消费方展示报告时应沿父链（execution_id → 父轮）回溯最近一份有报告的轮次。

## 4. 取消：`POST /api/runs/{id}/stop`

取消与结论在同一把 run 锁上裁决（终帧前是线性化点）：

| 响应 | 含义 |
|---|---|
| `200 {"requested": true, "status": ...}` | 受理；该 run 的结论**必为 `cancelled`** |
| `404 {"error": "unknown_run"}` | 未知/不可见 run |
| `403 {"error": "forbidden"}` | 无权取消 |
| `409 {"error": "run_not_cancellable", "requested": false, "status", "reason"}` | 已越过提交点：`reason=terminal`（终态已提交）或 `reason=finalizing`（终帧尚未提交成功） |

**受理不等于已见终态**：消费方拿到 200 后仍须等事件流的 `run_cancelled` 终帧再落终态。

## 5. 契约 ↔ 测试对应表

| 条款 | 测试 |
|---|---|
| 1.1 禁用字段 400 | `test_exec_contract.py::test_forbidden_run_body_fields_are_refused_400`、`test_forbidden_field_matching_ignores_case_and_space` |
| 1.1/1.2 受理字段、忽略额外字段、冻结 spec、201 run 视图 | `test_exec_contract.py::test_accepted_shape_ignored_extras_and_frozen_spec` |
| 1.1 字段形状 400 | `test_exec_contract.py::test_field_shape_violations_are_400` |
| 1.3 追问拒绝映射 | `test_exec_contract.py::test_followup_unknown_parent_is_404`、`test_followup_to_a_running_parent_is_409_run_busy`、`test_followup_after_a_followup_is_409_stale_run`、`test_followup_without_a_resumable_session_is_409` |
| 1.3 report_unavailable（受理后 fail-closed） | `test_exec_contract.py::test_followup_with_unreadable_seed_report_fails_closed` |
| 1.3 蓝图两码 | `test_blueprints.py:681`（unknown_blueprint）、`test_blueprints.py:689`（invalid_blueprint） |
| 1.3 not_configured / unsupported_command / 415 / resources_unreleased | `test_runs_api.py` 既有用例；resources_unreleased 另见 `test_event_replay.py:237` |
| 1.3 quota_exceeded | `test_multitenant_api.py:1063/1098`；429 的 tenant 维度见 `:1330` |
| 1.4 幂等键 | `test_exec_contract.py`（契约 1.4 区：同键重放同 run 且重放视图反映当前状态、同键异请求 409、运行中重放赢过 run_busy、畸形键 400、重启后重放、命令形式与字段形式是不同请求、prepare 窗口并发孪生败者收口为重放）、`test_multitenant_api.py::test_idempotency_keys_are_scoped_to_the_verified_owner` 与 `::test_idempotency_conflict_body_discloses_no_run` 与 `::test_concurrent_same_key_twins_replay_instead_of_500`（owner 作用域、冲突体不泄漏、多租户并发孪生不 500）、`test_store.py`（idempotent creation 区：键行与 run 行同事务、并发同键恰一个成功、旧库补表、digest 语义、指向不可读 run 时 fail-closed）、`test_reference_client.py::test_the_reference_client_replays_with_an_idempotency_key`（--idempotency-key 端到端） |
| 1.3 追问门禁 owner 优先 | `test_runner.py::test_foreign_parent_state_is_not_probed`；family→pack→capabilities→terminal→stale 的相互优先级**暂无专测**（诚实缺口，次序语义由 `runs/admission.py` 门禁代码钉住） |
| 2.1 合并次序 | `test_event_replay.py:315/360` |
| 2.2 续传 | `test_exec_contract.py::test_after_resumes_precisely_and_last_event_id_wins` |
| 2.3 429 语义 | `test_exec_contract.py::test_stream_limit_429_carries_a_readable_json_body` |
| 2.4 流 404 与断言过期 | `test_multitenant_api.py:900/1287/1305/1438` |
| 2.5 终帧集合 | `test_exec_contract.py::test_terminal_frame_set_and_finality`、`test_failed_run_ends_with_run_failed`、`test_cancelled_run_ends_with_run_cancelled` |
| 2.5 消费纪律（累计快照/截断/usage 累计） | `test_kernel_projection.py:174`（tool_updated 快照）、`test_kernel_rpc.py:360` 与 `test_kernel_projection.py:289`（assistant_message 截断）、`test_runner.py:652`（usage 累计） |
| 2.5 交付不完整 | `test_exec_contract.py::test_stop_reason_length_is_finished_with_incomplete_delivery` |
| 3.1/3.3 report 与 hasReport | `test_exec_contract.py::test_report_endpoint_and_has_report`、`test_missing_report_is_404_no_report` |
| 3.2/3.3 deliverables | `test_exec_contract.py::test_run_view_deliverables_and_download` |
| 3.3 种子 digest | `test_exec_contract.py::test_followup_seed_report_does_not_masquerade_as_new_report` |
| 4 取消两态 | `test_exec_contract.py::test_stop_mapping_terminal_and_unknown`、`test_stop_while_finalizing_is_409` |
