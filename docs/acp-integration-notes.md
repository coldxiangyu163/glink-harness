# ACP 集成笔记

[多 harness 方案](multi-harness-plan.md) · [pi 接入边界](pi-boundary.md) · [架构](architecture.md)

本文记录 ACP（Agent Client Protocol）族接入的协议事实与实现口径。**每条标注来源**：

| 来源标记 | 含义 |
|---|---|
| `schema` | ACP v1 的 JSON Schema / 官方 schema 页（本机核对副本：`@agentclientprotocol/sdk` 1.4.0 的 `schema/schema.json`，`$defs` 中的 v1 定义） |
| `docs` | `agentclientprotocol.com/protocol/v1/*` 官方文档 |
| `dsh 源码` | 只读参考 `/Users/coldxiangyu/AI/git/deepseek-harness/packages/acp/acp`（M1 不接真实 dsh；不作为运行时事实） |
| `合成实测` | 本仓库 M1/M2 回归（`tests/test_kernel_acp*.py` + `tests/fixtures/fake_acp.py`）。**只证明宿主侧客户端/投影/生命周期行为，不证明任何真实 agent 的实现** |
| `dsh 实测` | dsh 0.1.5-rc.3 的真实 initialize/session/new 与真实运行（证据 `docs/evidence/acp-m2-handshake.txt`、`acp-m2-smoke-dsh.txt`）；M8 的真实 `session/resume` 见 `docs/evidence/acp-m8-resume-dsh.txt` |
| `codex-acp 实测` | codex-acp 的真实 initialize/session/new 与真实运行（M2 证据 `docs/evidence/acp-m2-handshake.txt`、`acp-m2-smoke-codex-acp.txt`）；M8 用后继钉版 `@agentclientprotocol/codex-acp@1.13.1` 实测真实 `session/resume`，见 `docs/evidence/acp-m8-resume-codex-acp.txt` |
| `docker 实测` | M3a 的真实容器证据（`docs/evidence/acp-m3-docker-*.txt`）：镜像构建与容器内自检、容器内 ACP 握手、经宿主 HTTP API 的一次最小真实 run；M4 重建镜像后的三个 agent 容器内握手见 `docs/evidence/acp-m4-claude-docker-handshake.txt`；M6 重建镜像后的 claude 后继包容器内握手与回归见 `docs/evidence/acp-m6-migration-docker-handshake.txt` |
| `e2b 帧实测` | M3b 帧完整性探针的真实沙箱输出（`docs/evidence/acp-m3-e2b-framing.txt`，base 模板，e2b SDK 2.51.0）：保序、字节一致、LF 分帧、大帧与 `close_stdin` |
| `e2b 模板实测` | M3b 的 dev 模板 CLI 构建与真实沙箱自检（`docs/evidence/acp-m3-e2b-template.txt`）：`glink-acp-worker`、构建日志、两次失败与修复；M6 的后继钉版重建见 `docs/evidence/acp-m6-migration-e2b-handshake.txt` |
| `e2b 握手实测` | M3b 用生产装配路径在两个 agent 上的真实沙箱 `initialize`/`session/new`（`docs/evidence/acp-m3-e2b-agent-check.txt`）；M4 的同路径 claude-code-acp 沙箱握手见 `docs/evidence/acp-m4-claude-e2b-handshake.txt`；M6 的同路径 claude-agent-acp 握手见 `docs/evidence/acp-m6-migration-e2b-handshake.txt` |
| `e2b 实测` | M3b 经宿主 HTTP API 的一次最小真实 run（`docs/evidence/acp-m3-e2b-smoke.txt`）：SSE 流式、终态、report 回收、沙箱按 id 终止；M8 的跨沙箱「首轮+追问」续接与状态根回收见 `docs/evidence/acp-m8-resume-e2b-dsh.txt`、`acp-m8-resume-e2b-handshake.txt` |
| `claude 实测` | M4 的真实 claude-code-acp 事实（`docs/evidence/acp-m4-claude-handshake.txt`、`acp-m4-claude-smoke.txt`）：钉版入口、能力矩阵、每 run HOME/凭据落点、源码级权限 kind 观测、一次真实模型的 inproc 最小 run（M4 钉版 0.16.2；后继包对照见下一行） |
| `claude-agent 实测` | M6 的真实 `@agentclientprotocol/claude-agent-acp@0.81.2` 事实（`docs/evidence/acp-m6-migration-handshake.txt`、`acp-m6-migration-smoke.txt`、`acp-m6-migration-docker-handshake.txt`、`acp-m6-migration-e2b-handshake.txt`）：入口/版本、能力矩阵差异、settings env 凭据形态（原生 cli 与 `CLAUDE_CODE_EXECUTABLE` 两条通道）、两次 inproc 最小真实 run、镜像/dev 模板重建与容器/沙箱握手 |
| `M6 源码` | 只读参考 `@agentclientprotocol/claude-agent-acp@0.81.2` 的安装产物（`dist/acp-agent.js` 的 `claudeCliPath()`、`dist/managed-policy.js` 等）；不作为运行时事实，用于解释真实观测 |
| `opencode 实测` | M5 的真实 opencode 事实（`docs/evidence/acp-m5-opencode-handshake.txt`、`acp-m5-opencode-smoke.txt`、`acp-m5-opencode-docker-handshake.txt`、`acp-m5-opencode-e2b-handshake.txt`）：钉版入口、能力矩阵、每 run HOME/凭据落点（inproc/docker/e2b）、一次真实模型的 inproc 最小 run 与 0 条权限请求 |
| `opencode 源码` | 只读参考 opencode 1.18.32 的上游源码（`anomalyco/opencode` v1.18.32 的 `packages/opencode/src/acp/{permission,event,tool}.ts` 等）；不作为运行时事实，用于解释真实观测 |
| `镜像自检` | `pi-glink-acp-worker` 镜像内各组件的版本输出（构建期日志与本机 `docker run --rm` 复验，M4 起含 claude 双包，M6 起为后继包 claude-agent-acp；证据同 `docker 实测` 与 `docs/evidence/acp-m6-migration-docker-handshake.txt`） |
| `npm` | npm registry 元数据（`npm view`） |
| `宿主代码` | 本仓库实现（`host/pi_glink/kernel/`、`config.py`） |
| `待实测` | 需要真实 agent/授权或其它执行面才能确认 |

状态（2026-09-25）：M1 合成验证完成（GLINK-ACP-01）；**M2 真实冒烟完成**（GLINK-ACP-02）：
`@deepseek-ai/dsh@0.1.5-rc.3` 与 `@zed-industries/codex-acp@0.16.0` 均完成真实
initialize + session/new（不消耗模型）并经宿主 HTTP API 各跑一次最小真实研究 run。
**M3a 完成（GLINK-ACP-03）**：ACP 族的 Docker 执行面可用（容器 stdio 桥接同一 M1 客户端、
每 run HOME 与只读凭据挂载、资源限制与 pi 面对齐），镜像与实测见 §5。
**M3b 完成（GLINK-ACP-04）**：ACP 族的 E2B 执行面可用——先在真实沙箱完成双向 stdio
帧完整性实测（§6.1），再用独立 dev 模板 `glink-acp-worker` 跑通握手（§6.4）与一次最小
真实 run（§6.5）；沙箱后台命令的 stdio 交给同一 M1 客户端，每 run HOME/凭据、产物回收与
`sandbox_id` 对账沿用 pi 面约定。
**M4 完成（GLINK-ACP-05，2026-09-25）**：claude-code-acp（`@zed-industries/claude-code-acp`
0.16.2）接入：inproc 握手与一次最小真实 run 完成；每 run 凭据形态、权限白名单、镜像与 dev
模板重建（含 claude 双包钉版）均已实测，见 §7。ACP 续接仍属后续里程碑。
**M5 完成（GLINK-ACP-06，2026-09-25）**：opencode（`opencode-ai` 1.18.32，入口
`opencode acp`）接入：inproc 握手、每 run `auth.json` 注入（OAuth access/refresh 并入
redactor）、状态目录落点、权限形态与一次最小真实 run 均已实测；镜像与 dev 模板加
opencode 钉版层并完成容器/沙箱握手，见 §8。
**M6 完成（GLINK-ACP-08，2026-09-25）**：claude 适配器迁到后继包
`@agentclientprotocol/claude-agent-acp@0.81.2`（入口 `claude-agent-acp`）——inproc 握手与
能力矩阵重新实测、settings env 凭据形态在新版成立、两次最小真实 run（原生通道与
`CLAUDE_CODE_EXECUTABLE` 通道）通过；镜像与 dev 模板换后继钉版层并完成容器/沙箱握手；
codex 侧的旧包引用收尾清零，见 §9。
**M7 完成（GLINK-ACP-09，2026-09-25）**：ACP 续接可用——追问按能力矩阵走
`session/resume`（优先，不回放）或 `session/load`（回放帧计数后丢弃）；追问 run 从父 run
继承 agent 状态根（有界、拒绝链接、凭据目标排除后重渲染、继承值并入 redactor）；准入从 M1
的“一律拒绝”改为「同 harness + 已结束 + 最新轮次 + 有 sessionId + 声明续接能力」受理，
其余维持拒绝（e2b 面因状态随沙箱销毁而继续拒绝）；续接沿用会话工作区（会话 cwd 跨轮稳定）。
claude-agent-acp 与 opencode 各完成一次 inproc「首轮+追问」真实续接，连续性由首轮约定的
代号在追问助手正文与子 run report.md 中复现来判定。见 §10 与
`docs/evidence/acp-m7-resume-*.txt`。
**M8 完成（GLINK-ACP-13，2026-09-25）**：e2b 面续接可用——ACP e2b run 在销毁沙箱前把 agent
状态根有界镜像到宿主 `runs/<id>/acp-agent/recovered`（整份成功才发布，越界/读失败拒绝），追问
把继承状态与既有 `report.md` 灌回新沙箱（先 0700 后上传、0600 加固）后走 `session/resume`；
e2b 追问准入在原 M7 四条件上增加「父状态根回收成功」，其余维持拒绝。dsh 与 codex-acp 各完成
一次 inproc「首轮+追问」，dsh 另完成一次 e2b 跨沙箱「首轮+追问」（新沙箱、同 sessionId、
代号回环、按 id 清理）。见 §11 与 `docs/evidence/acp-m8-resume-*.txt`。

## 1. 协议事实（v1）

### 1.1 传输与分帧

* ACP 是 **JSON-RPC 2.0**，stdio 传输：客户端启动 agent 子进程，消息走 agent 的
  stdin/stdout；一条消息一行，**LF 分隔且消息内不得含裸换行**，UTF-8 编码。agent 可以
  往 stderr 写日志，但 stdout 上的任何非 ACP 消息都是违规。（`docs`，transports）
* 正因如此，宿主复用 `host/pi_glink/kernel/framing.py` 的 LF-only 拆分：U+2028/U+2029
  是 JSON 字符串内容而非分帧边界，`\r\n` 写入者的尾部 `\r` 单独剥离。（`合成实测`：
  ACP 客户端跑共享语料 `tests/fixtures/lf-framing.json` 的每条记录，与 pi 客户端一致）
* 请求/响应按 `id` 关联，id 可以是数字或字符串；本客户端只发数字 id，回显 agent 的请求
  id 原样（三种 JSON-RPC id 类型都可能出现）。（`schema`）

### 1.2 初始化与能力协商

* `initialize` 请求：`protocolVersion`（必填）、`clientCapabilities`、`clientInfo`。
  协议版本是**单个整数（MAJOR）**，v1 即 `1`；版本协商规则是「agent 支持则回同一个，
  否则回自己最新的，客户端不支持就应关闭连接」。（`schema`/`docs`；SDK 1.4.0 的
  `PROTOCOL_VERSION = 1`）
* `initialize` 响应：`protocolVersion`（必填）、`agentCapabilities`、`agentInfo`、
  `authMethods`。省略的能力位按「不支持」处理（introspection 不成立）。（`docs`/`schema`）
* 宿主客户端声明：`fs.readTextFile=false`、`fs.writeTextFile=false`、`terminal=false`、
  `auth.terminal=false`，即**不提供任何客户端方法**：ACP agent 在自己的文件系统里干活
  （inproc 面就是沙箱/宿主工作区），与方案 §3 的拓扑一致。（`合成实测`：fake agent 回读
  到的 clientCapabilities 正是这四个值）
* 版本不一致时宿主在 `session/new` 之前失败收口（run `failed/execution_error`），不带着
  未知线格式继续。（`合成实测`）
* `initialize` 报错（如 `auth_required`）走同一条失败路径，不静默降级。（`合成实测`）
* 认证能力（`authMethods` 的 `terminal`/`env_var` 等）宿主不消费，只把前 20 条并入 run 记录。
  M2 实测：dsh 返回 `[]`（`--profile acp` 组合无需认证）；codex-acp 返回 `chatgpt` 与两个
  `env_var` 方法（`CODEX_API_KEY`/`OPENAI_API_KEY`），另声明可选 `auth.logout`；未注入凭据时
  `session/new` 以 `-32000 Authentication required` 拒绝，是凭据注入生效的直接观测点。
  （`dsh 实测`/`codex-acp 实测`；证据 §4.4）
* `clientInfo` 是 `Implementation`，`name` 与 `version` 都是必填。M4 实测：claude-code-acp
  0.16.2 严格校验 initialize 参数，缺 `version` 直接回 `-32602 Invalid params`（连 session
  都不会建立）；宿主因此补上自身版本，合成回归钉住该字段。（`schema`/`claude 实测`）

### 1.3 会话与 prompt

* `session/new` 请求：`cwd`（必填，绝对路径）、`mcpServers`（必填数组，可以为空）、可选
  `additionalDirectories`（能力位 `sessionCapabilities.additionalDirectories`）；响应
  `sessionId`（必填）、可选 `modes`/`configOptions`。宿主的 `cwd` 与 `mcpServers` 分别是
  本 run 工作区与空数组。（`schema`）
* `session/resume` / `session/load`：续接已有会话（能力位 `sessionCapabilities.resume`
  与顶层 `loadSession`）；`resume` 不回放历史，`load` 在应答前把历史以 `session/update`
  回放。两者都带 `sessionId` + `cwd`（`load` 还要求 `mcpServers`）。（`schema`/`docs`）
  宿主的实现与实测差异见 §10.1。
* `session/prompt` 请求：`sessionId`、`prompt`（`ContentBlock[]`）。基线保证 agent 支持
  text 与 resource_link；image/audio/embeddedContext 由 agent 的 `promptCapabilities`
  决定。M1 只发一个 text block（本 run 的 prompt 本身就是纯文本）。（`schema`/`docs`）
* `session/prompt` 响应：`stopReason`（必填）与一个 **UNSTABLE** 的可选 `usage`。M1 不投影
  该字段（用量只走 `usage_update`）。（`schema`）
* prompt 请求是**长请求**：turn 结束（或被取消）才回响应，期间的输出全走
  `session/update` 通知。宿主因此把 prompt 放在驱动线程里等，run 截止时间由 `wait()` 管。
  （`docs`/`合成实测`）

### 1.4 `session/update` 变体

v1 完整变体集（`docs`/`schema`）：`user_message_chunk`、`agent_message_chunk`、
`agent_thought_chunk`、`tool_call`、`tool_call_update`、`plan`、
`available_commands_update`、`current_mode_update`、`config_option_update`、
`session_info_update`、`usage_update`（另有 UNSTABLE 的 `plan_update`/`plan_removed`）。

* 内容块消息（`ContentChunk`）携带一个 `ContentBlock`，可带 `messageId` 标识同一消息；
  M1 只投影 text 块。（`schema`）
* `plan` 每次携带**完整条目列表**（客户端整体替换），条目含 `content`/`priority`/`status`。
  （`schema`）
* 未投影的变体（`user_message_chunk`、`current_mode_update`、`config_option_update`、
  `session_info_update` 等）在 M1 **不伪造产品事件**，按类型一次性写一条 `kernel_status`
  提示（`acp_update_not_projected`），避免静默丢事实。（`合成实测`）
* **回合边界**：ACP 没有「turn 开始/结束」更新，但产品的流式模型靠 `turn_finished` 关闭
  文本/思考块（关闭后才按 Markdown 渲染，思考行才从「正在思考」转为「思考过程」）。适配器
  因此在发出 `session/prompt` 前与回合结束时各发一个合成帧（`acp_turn_started` /
  `acp_turn_finished`），由投影层译为 `turn_started` / `turn_finished`；前端无需知道 harness
  差异。（`合成实测`；前端语义由 `tests/web/run-stream.test.mjs` 的既有断言钉住）

### 1.5 工具调用

* `tool_call`：`toolCallId` 必填；`title`（人类可读）始终可用，`name` 是 **UNSTABLE** 的可选
  程序名，`kind` 是类别枚举（read/edit/delete/move/search/execute/think/fetch/switch_mode/other）；
  `status` ∈ pending/in_progress/completed/failed；`rawInput`/`rawOutput` 为任意 JSON。
  （`schema`）
* `tool_call_update`：除 `toolCallId` 外全部可选；`content` 是**整体替换**语义；状态推进到
  completed/failed 即终态。（`schema`）
* `ToolCallContent` 有三种：`content`（包 `ContentBlock`）、`diff`（`path`/`oldText`/`newText`）、
  `terminal`（`terminalId`，需要 `terminal/*` 能力——宿主不声明，出现只作占位呈现）。（`schema`）
* 产品工具卡片口径（`合成实测`）：`toolName` 取 `name → title → kind`（程序名优先；`title` 是人类可读描述且总存在，dsh 把工具名放在这里而 `kind` 固定为 `other`；类别仅作最后回退），`tool_started` 额外带 `kind`；`args` 走与 pi 相同的限幅投影（`projection._project_args`，同一份数组/对象契约）；
  `result` 取 `content` 文本（diff 渲染为 `diff <path>` + 新文本），为空时回退 `rawOutput`
  的紧凑 JSON，最终整体限幅；`tool_updated.output` 同样是「到目前为止的累计内容」。

### 1.6 用量

* `usage_update`：`used`（当前上下文中的 token）与 `size`（上下文窗口）必填且非负，
  `cost` 可选且必须同时含 `amount`（数值）与 `currency`（ISO 4217）；`cost` 是**会话累计**。
  （`schema`/`docs`）
* 这是「上下文窗口占用 + 累计成本」视角，**不是** pi 的逐请求 token 账本。宿主不做逐请求
  求和（那会伪造增长曲线），而是保留 ACP 口径并标记：

  ```json
  {"usage": {"totalTokens": <used>, "contextSize": <size>,
             "basis": "acp_usage_update",
             "cost": {"amount": …, "currency": …}}}
  ```

  终态记录的 `usage` 采用同一次转换的最后一个快照（替换语义，不是累加）。（`合成实测`）
* `used`/`size` 非法或 `cost` 形状不对时不猜数：写一条 `kernel_status`（`acp_usage_invalid` /
  `acp_usage_cost_invalid`）并跳过相应字段。（`合成实测`）
* `usage_update` 是可选能力。M2 实测两 agent 都发送，频率与数值见 §4.5；宿主仍按替换语义
  记录终态快照。（`dsh 实测`/`codex-acp 实测`）

### 1.7 权限

* `session/request_permission` 是 **agent → client** 请求：`sessionId`、`toolCall`（至少含
  `toolCallId`）、`options[{optionId, name, kind}]`；`kind` ∈ allow_once/allow_always/
  reject_once/reject_always。响应 `outcome` 只有两种：`selected{optionId}` 或 `cancelled`。
  （`schema`）
* M1/M2 默认策略是 **fail-closed 全拒**：优先选 `reject_once`，其次 `reject_always`；
  没有可用的拒绝项（或缺 `optionId`）时回 `cancelled`；若 `session/cancel` 已经发出，按规范
  必须回 `cancelled`。（`合成实测`）
* M2 增加了一个显式配置门控的白名单：`PI_GLINK_ACP_PERMISSION_ALLOW`（option kind，
  可选 tool kind）。默认空集不改变 fail-closed 行为；只有配置了 `allow_once`/`allow_always`
  时才会选择匹配的 allow 选项，且当同时配置了 tool kind 时请求的 `toolCall.kind` 必须命中
  （没有 kind 的请求一律拒绝）。决定与策略都进事件流（`acp_permission_allowed` /
  `acp_permission_denied`）。（`宿主代码`/`合成实测`；M2 未启用，理由见 §4.6）
* 每次拒绝都写 `kernel_status`（`acp_permission_denied`，warning），带 `decision`、
  `toolCallId`、选中的 `optionId`/`optionKind`——拒绝不允许静默。（`合成实测`；方案 §4.2）
* M4 补上 tool kind 解析：`session/request_permission` 的 `toolCall.kind` 是**可选**字段，
  claude-code-acp 0.16.2 的请求就不带它（实测）。宿主按同一 `toolCallId` 记住 agent 已经
  在 `tool_call`/`tool_call_update` 通知里公布的 kind，用它给请求分类；两处都没有 kind 时
  仍视为未分类，配了 tool kind 的白名单继续拒绝。事件流里 `toolKindSource` 标明 kind 来自
  `request` 还是 `tool_call`。（`claude 实测`/`合成实测`）
* dsh 侧的权限请求只给 `allow-once`/`reject-once` 两个一次性选项（`dsh 源码`，待实测确认）。
* opencode 的请求选项固定为 `once`/`always`/`reject`，且 `toolCall` 自带 `kind`
  （`opencode 源码`）；M5 的真实 run 未触发请求，原因与触发面记在 §8.4。

### 1.8 取消

* `session/cancel` 是**通知**（无响应）。agent 应尽快停止模型与工具调用，把待发的
  `session/update` 发完，并**必须**用 `stopReason=cancelled` 回应原来的 `session/prompt`。
  客户端在发出取消时应把未完成的工具标记为 cancelled，并**必须**用 `cancelled` 回应所有
  待答的权限请求。（`docs`/`schema`）
* 宿主实现：`cancel()` 幂等，只发一次通知；run 的结论由宿主的取消标志决定（adapter 的
  cancel 只缩短等待）。**超时触发的收尾取消不算用户取消**：结论保持 `failed/timeout`，
  不会因为 agent 回了 `cancelled` 而被改写成 `cancelled`。产品侧在终态为 `cancelled` 时
  把未完成的工具卡片标为已取消（前端 `run-stream.mjs` 的既有行为），适配器不为凑规范
  伪造额外的 `tool_call_update`。（`合成实测`）

## 2. 产品事件映射（实现口径）

| ACP | 产品事件 | 实现口径 |
|---|---|---|
| `initialize` 响应 | `session_ready` | payload 带 `harness: "acp"`、`protocolVersion`、`sessionId`、`agentInfo`、`agentCapabilities`、`authMethods`、`cwd`，以及 `session/new` 的 `modes` 与 `configOptions`（会话选项状态，含宿主应用 `PI_GLINK_ACP_SESSION_CONFIG` 后的值）；`sessionFile` 为 null。续接 run 另带 `resumed: true` 与 `resumeMethod`（`session/resume`/`session/load`），`sessionId` 是父 run 的原值（§10.1） |
| `agent_message_chunk` | `assistant_delta` | 只投影 text 块；其他块型记一次性 `kernel_status`；文本走与 pi 相同的 `StreamRedactor` |
| `agent_thought_chunk` | `thinking_delta` | 同上 |
| `tool_call` | `tool_started`（若已含终态再补 `tool_finished`） | `toolName`/`args` 见 §1.5 |
| `tool_call_update`（pending/in_progress） | `tool_updated` | `output` = 累计内容 |
| `tool_call_update`（completed/failed） | `tool_finished` | `isError = status == "failed"`，`result` = 内容或 `rawOutput` |
| `plan` | `run_stage`（`stage: "plan"` + `entries`） | 条目限 20 条、每条 300 字符 |
| `usage_update` | `usage_reported` | 见 §1.6；口径标注 `basis` |
| `available_commands_update` | `kernel_status`（warning） | 记命令数与名称（限幅）；ACP 门禁一律拒绝 `/` 开头 prompt |
| 适配器合成的续接帧（`session/resume`/`session/load` 应答后） | `kernel_status`（info，`acp_session_resumed`） | 记录续接方法与 `session/load` 被丢弃的回放帧数；丢弃的是父 run 已记录的历史，不重复投影（§10.1） |
| 适配器合成的回合边界（`session/prompt` 前后） | `turn_started` / `turn_finished` | ACP 没有 turn 更新；宿主补上这两个产品回合边界，让流式文本/思考块按产品既有语义关闭与渲染（见 §1.4） |
| `session/prompt` 的 `stopReason` | 终态判定输入 | `end_turn`→正常；`max_tokens`→`finished/length`（交付不完整）；`max_turn_requests`→`finished/max_turn_requests`；`refusal`→`failed/model_error`；`cancelled`→取消路径；未知/缺字段→`failed/execution_error` |
| `session/request_permission` | `kernel_status`（warning） | 不投影成交互，自动应答见 §1.7 |
| 其余变体 | 不投影 | 一次性 `kernel_status` 提示，词表不变（前端不感知 harness 差异） |

`modelStopReason` 对 ACP run 保留 ACP 的原词（`end_turn`/`max_tokens`/…），`stopReason`
仍是产品终态词表里的值。

## 3. M1 集成决定与已知偏差

1. **harness 选择只在配置层**：`PI_GLINK_HARNESS=pi|acp`（默认 `pi`）+
   `PI_GLINK_ACP_COMMAND`（ACP agent 命令行，`shlex.split` 后直接 exec，不经 shell）。
   没有 API/前端参数，`public_view()` 不含 harness，现有部署零变化。（与方案 §5/§6 一致）
2. **执行面**：M1 只接 `inproc`；M3a 起 `PI_GLINK_HARNESS=acp` 同时支持
   `LOCAL_WEB_EXECUTOR=inproc|docker`，M3b 起再加上 `e2b`（§6；沙箱后台命令的
   `stdin=True` + `send_stdin` 承载同一份 stdio）。三个执行面共用同一个 ACP 适配器与
   `AcpRpcClient`，不为 e2b 另写协议代码。
3. **续接（M1 决定，M7 已替代）**：M1 不实现 `session/resume`（能力位
   `sessionCapabilities.resume`）：ACP run 不落会话文件，追问在**准入层**以
   `session_unavailable` 拒绝；适配器另外对任何 `resume_session` 输入直接报错，绝不以新会话
   冒充追问（方案 §4.4 的「能力缺失显式拒绝」）。**M7 已实现续接**：能力矩阵声明续接通道的
   agent 可按 §10.3 的条件受理，`session/resume`/`session/load` 的选择与状态继承见 §10。
4. **状态目录**：每 run 渲染 `<run_dir>/acp-agent/home` 并作为子进程 `HOME`，agent 的
   `~/.claude`、`~/.codex`、dsh sessions 与 npm 状态都落在本 run 内；环境沿用 pi 面的最小
   透传清单。M2 补上显式凭据注入：`PI_GLINK_ACP_CREDENTIAL_FILES` 把 `source=dest` 的源
   文件**只读复制**进 run HOME（父目录 0700、文件 0600），`PI_GLINK_ACP_ENV` 注入显式 env
   （不允许覆盖 `HOME`）；注入值抽入该 run 的 redactor，源文件不被修改（sha256 前后一致）。
   逐 agent 实测落点见 §4.4。（`宿主代码`/`dsh 实测`/`codex-acp 实测`）
5. **未投影不等于丢失**：`max_turn_requests` 目前在记录里保留原词，前端尚无对应提示
   （只有 `length` 会显示「交付不完整」）；`current_mode_update`/`session_info_update`
   等只留一条 `kernel_status` 提示。
6. **`session/close` 未调用**：M1 用一个 run 一个进程，teardown 由进程退出完成；agent 是否
   需要显式关闭会话（`sessionCapabilities.close`）待 M2 实测后决定。

## 4. M2 实测（2026-09-25）

本节每条都标来源。`dsh 实测` / `codex-acp 实测` 的证据文件：
`docs/evidence/acp-m2-handshake.txt`（真实 initialize/session/new，未消耗模型）、
`docs/evidence/acp-m2-smoke-dsh.txt`、`docs/evidence/acp-m2-smoke-codex-acp.txt`（各一次
经宿主 HTTP API 的最小真实研究 run）。真实模型调用仅限这两次冒烟。

### 4.1 版本与入口

| agent | 钉版与入口 | 实测事实 |
|---|---|---|
| dsh | `@deepseek-ai/dsh@0.1.5-rc.3`（npm dist-tag `latest`），入口 `dsh --profile acp` | 0.1.5-rc.3 的 dsh CLI 没有 `acp` 子命令；brief 里的 “dsh acp” 是简写。长驻 stdio 服务器，`--help` 自述 “Serve automation clients over Agent Client Protocol stdio.”（`npm`/`dsh 实测`） |
| codex-acp | `@zed-industries/codex-acp@0.16.0`，入口 `codex-acp`（随平台可选包带二进制） | 无参数即 stdio ACP server；支持 `-c key=value` 覆盖 `~/.codex/config.toml`。npm 安装时告警：该包已被 `@agentclientprotocol/codex-acp` 取代，后继包迁移评估留给 M4（`npm`/`codex-acp 实测`） |

两者都只用钉版本地安装（`npm install --prefix`）或 npx 钉版调用，未全局安装。

### 4.2 能力矩阵（initialize / session/new 响应原文）

| 字段 | dsh 0.1.5-rc.3 | codex-acp 0.16.0 |
|---|---|---|
| `agentInfo` | `{"name": "deepseek-harness-acp", "version": "0.0.1"}` | `{"name": "codex-acp", "title": "Codex", "version": "0.16.0"}` |
| `protocolVersion` | `1` | `1` |
| `loadSession` | 未声明（不支持） | `true` |
| `sessionCapabilities` | `close` / `list` / `resume` | `list` / `resume` / `close` |
| `promptCapabilities` | image=false, audio=false, embeddedContext=false | image=true, audio=false, embeddedContext=true |
| `mcpCapabilities` | `{http: true}` | `{http: true, sse: false, acp: false}` |
| `authMethods` | `[]`（无需认证） | `chatgpt` + `codex-api-key`/`openai-api-key`（`env_var`，各带变量名） |
| `session/new` 返回 | `sessionId` + `configOptions`（`model`、`reasoning_effort`） | `sessionId` + `modes`（read-only/auto/full-access）+ `configOptions`（`mode`、`model`、`reasoning_effort`） |
| 附件能力位 | prompt 不接受图片 | image/embeddedContext 已声明（宿主只发 text block） |

补充实测事实：

* dsh 的 `model` 取值是 **JSON 编码字符串**（`["deepseek-official","deepseek-v4-flash"]`），且
  select 选项按 provider 分组（`options[].options[]`）；宿主的选择值校验需要展平分组。
  （`dsh 实测`/`宿主代码`）
* dsh 不返回 `modes`：ACP 的 modes 面不在它的 automation 契约里。（`dsh 实测`/`dsh 源码`）
* codex-acp 在 `session/new` 后立即发 `available_commands_update`（本次 6 条：review、
  review-branch、review-commit、init 等）。宿主对 ACP 族仍拒绝一切 `/` 开头 prompt——
  agent 声明命令不改变准入门禁。（`codex-acp 实测`；§2 词表不变）
* 未认证时 codex-acp 的 `session/new` 回 `-32000 Authentication required`；dsh 无此要求。
  （`codex-acp 实测`）
* 两侧都接受 `session/set_config_option`（`configId`+`value`）：codex-acp `mode=auto` 与
  dsh `reasoning_effort=low` 均生效，且 codex-acp 的 `modes.currentModeId` 随之更新。
  （`合成实测`/`dsh 实测`/`codex-acp 实测`）

### 4.3 system prompt 通道（决定与实测）

ACP v1 没有 system role：`session/new` 只有 `cwd`/`mcpServers`/`additionalDirectories`
与保留字段 `_meta`，而 schema 明确 “Implementations MUST NOT make assumptions about values
at these keys”；modes/configOptions 是审批与模型选择器，不是 prompt。

**决定（M2）**：工作约定作为 `session/prompt` 文本块的**前置围栏段**送达：
`compose_prompt()` 把 `spec.system_prompt` 包在 `<system-prompt>…</system-prompt>` 中，
后接 `--- 用户请求 ---` 与原始题面。理由：text block 是 ACP 基线能力（agent MUST
support），不依赖可选能力位、不依赖任何 agent 私有扩展；围栏让模型能区分工作约定与用户
请求。系统提示为空时保持 M1 的原样单文本块。（`宿主代码`/`合成实测`）

真实接收证据：两次真实 run 中 agent 都按工作约定把交付写成工作区 `report.md`、回复里只
给结论文本（codex-acp 出现对 `report.md` 的 `edit` 工具调用与短回复；dsh 出现 `write`/
`edit` 与 “报告写入 report.md” 回复），且报告合约（来源列表）生效。（证据时间线，
`dsh 实测`/`codex-acp 实测`）

未采用的通道（逐条实测）：

* `_meta` 注入 systemPrompt：schema 保留、无 agent 消费者；dsh 源码不读 `_meta`，codex-acp
  也未声明该约定。（`schema`/`dsh 源码`）
* agent 私有配置：codex 有 `-c developer_instructions=…`、dsh 有 `--patch` 改 system-prompt
  插件的路径，但都属于单 harness 的配置渲染器（方案 §4.5 已有预留），M2 选协议通用通道，
  不把产品契约挂在某个 agent 的私有面。（`npm`/`dsh 源码`）

### 4.4 凭据注入（逐 agent 实测落点）

| agent | 注入材料 | 每 run 落点 | 生效证据 |
|---|---|---|---|
| codex-acp | `~/.codex/auth.json`（ChatGPT 登录） | `HOME/.codex/auth.json`（0600） | 无该文件时 `session/new` 报 “Authentication required”；注入后握手成功并完成真实 run；`~/.codex` 状态（cache/config.toml/sqlite）均落在 run HOME，真实目录未被修改 |
| dsh | `~/.dsh/.credentials.yaml`（harness home 凭据） | `HOME/.dsh/.credentials.yaml`（0600） | harness home 默认 `$DSH_HOME` 或 `~/.dsh`（dsh docs）；实测 profiles/sessions/storages 都落在 run HOME/.dsh，真实 `~/.dsh` 未被修改 |

* 注入用 `PI_GLINK_ACP_CREDENTIAL_FILES`（`;` 分隔的 `source=dest`）：源文件只读，目标必须是
  run HOME 内相对路径；目标文件 0600、父目录 0700，已存在或为符号链接则失败（不覆盖、不
  跟随）。（`宿主代码`/`合成实测`）
* 本机 dsh 的真实状态目录是 `~/.dsh`（`$DSH_HOME` 未设置时的默认值），**`~/.config/dsh`
  不存在**（`ls` 实测）；任务描述里的 `~/.config/dsh` 在本机不是 dsh 的读写路径，两种路径
  都未被本实现或冒烟修改。（`dsh 实测`）
* `PI_GLINK_ACP_ENV` 提供命令级 env（`NAME=value`），不允许覆盖 `HOME`（隔离边界）。（`宿主代码`）
* 注入值抽取（JSON 凭据键、YAML 标量行、provider shape）并入**该 run** 的 redactor：
  两个真实 run 的 events/report/API 下载对真实凭据值 0 命中；源文件 sha256 前后一致。
  （`合成实测`/`dsh 实测`/`codex-acp 实测`）
* 真实凭据 mtime/hash 在握手与冒烟前后均未变化；凭据值不落日志、不落 evidence、不进命令
  行或进程环境（文件形态注入）。（`握手中脚本输出`/`宿主代码`）

### 4.5 usage 口径（实测）

* 两 agent 都发送 `usage_update`，宿主按 M1 口径**替换**快照并标 `basis: "acp_usage_update"`；
  终态记录采用最后一个快照。（`dsh 实测`/`codex-acp 实测`）
* dsh 本次 run：`used` 7,962 → 27,328，`size` 1,000,000；每个模型 step 一条。
  codex-acp：`used` 45,799 → 45,116（上下文压缩/重算导致回落），`size` 258,400；每轮助手
  消息前后一条。（证据 events.jsonl）
* 两次 run 都未出现 `cost`（订阅制/会话计数不提供金额）；schema 允许省略，宿主不伪造。
  （`dsh 实测`/`codex-acp 实测`）
* `session/prompt` 响应里的 UNSTABLE `usage` 字段仍未观测到。

### 4.6 权限（决策：保持 fail-closed，不启用白名单）

* 两次真实 run 都在 fail-closed 默认下完成，`acp_permission_*` 事件 0 条：codex-acp 以
  `mode=auto`（workspace-write，会话配置显式指定）运行，编辑/命令/内置 web 搜索未触发审批；
  dsh 的默认组合是 `workspace-write + approval=ask`（dsh 源码），实测 web_search、web_fetch、
  write、bash、edit、read 也都未被要求审批。（`dsh 实测`/`codex-acp 实测`）
* **决定**：默认继续全拒；不启用任何白名单。`PI_GLINK_ACP_PERMISSION_ALLOW` 作为配置级
  门控已实现并有合成回归（默认空集 = 原行为），留给将来某个 agent 在真实 run 证明全拒不可
  用时按最小集合启用，并须回写本节。**M4 满足了启用条件**：claude-code-acp 的安全写入
  （Write/Edit）与 WebFetch 会逐次询问，研究 run 无法在 fail-closed 下产出 report.md；
  M4 定义的最小集合与真实 run 证据见 §7.4（dsh/codex-acp 的默认策略不变，仍不需要白名单）。
  **M5 反向确认无需扩展**：opencode 的真实 run 在 `webfetch`/`apply_patch` 上 0 条权限请求
  （白名单参数未被引用），继续沿用 fail-closed 默认，见 §8.4。
* 依据：dsh 的权限请求只给 `allow-once`/`reject-once` 且 `toolCall` 不含 `kind`（`dsh 源码`/
  `合成实测`），tool-kind 白名单对 dsh 不可用；codex-acp 的 `mode` 会话选项是比逐次放行更
  窄、更可审计的政策面。（`宿主代码`）

### 4.7 会话配置与模式

* `PI_GLINK_ACP_SESSION_CONFIG`（`id=value`）在 `session/new` 后应用；agent 未声明该选项或
  取值不在其声明集合内则 run 失败闭环，不静默忽略。（`宿主代码`/`合成实测`）
* 真实应用记录：codex-acp `mode=auto`（`modes.currentModeId` 同步为 auto），dsh
  `reasoning_effort=low`（握手时验证）。冒烟 run 使用 codex-acp `mode=auto`、dsh 默认。
  （`dsh 实测`/`codex-acp 实测`）
* 客户端能力声明仍是全空（无 `fs/*`、无 `terminal/*`、无 `auth.terminal`）：agent 在自己的
  文件系统上工作（`合成实测`；§1.2）。

### 4.8 两个真实 run 的其余事实

* 终态：均为 `finished`，`modelStopReason=end_turn`，`cleanup.released=true`；
  `reportStatus=ok`，`report.md` 经 `/api/runs/<id>/report` 200 回收，工作区/
  artifacts/API 下载三者内容一致。（`dsh 实测`/`codex-acp 实测`）
* 流式：run 期间 `assistant_delta`/`thinking_delta`/`tool_*` 按 seq/ts 逐条到达（codex-acp 73
  条事件/54s；dsh 60 条/43s），结束时 `turn_finished` 先于 `run_finished`。（证据 events.jsonl）
* 宿主清理：宿主 PID 由 `$!` 记录，运行结束后只对已知 PID 发 `kill -TERM`（未用 `pkill -f` 等
  宽匹配）。（证据 summary.txt）

## 5. M3a 实测（2026-09-25，Docker 执行面）

M3a 把 ACP 族从 inproc 扩展到 Docker 执行面。协议侧零新增：容器只是另一个 stdio 承载，
ACP 客户端、投影、生命周期判定继续复用 M1/M2 的同一份实现。

### 5.1 镜像与钉版

* 新增 `docker/Dockerfile.acp-worker`，`FROM node:24.21.0-bookworm-slim`——与 pi worker
  同一 Node 精确 tag（`宿主代码`/`镜像自检`）。镜像自包含：构建期装好 Node 与两个 agent，
  容器内不安装任何运行时依赖。
* 镜像钉版（可机检：`tests/test_kernel_acp_docker.py` 强制 Dockerfile ARG 等于本节记录的
  实测版本）：`@deepseek-ai/dsh@0.1.5-rc.3`、
  `@agentclientprotocol/codex-acp@1.13.1`。
* dsh 保持 M2 实测版本（`dsh 实测`/`npm`）。codex-acp 改钉**后继包**
  `@agentclientprotocol/codex-acp`：旧包 `@zed-industries/codex-acp`（0.16.0，M2 实测）
  已被 npm 标记 deprecated（“replaced by @agentclientprotocol/codex-acp”，`npm`），
  后继包 1.13.1 用仓库 M1 客户端实测 `initialize` 协商回 `protocolVersion=1`
  并声明 `sessionCapabilities`（`docker 实测`）；本次只验证握手，真实模型 run 仍列在 §12。
* 镜像默认命令即自检：`dsh --version && codex-acp --version`；构建期还用 ARG 精确比对
  两个 `--version` 输出（不一致即构建失败），因此“镜像里装的是声明版本”是可复核事实
  （`镜像自检`/`宿主代码`）。实测：`pi-glink-acp-worker:latest`（sha256:4268dca9d9ed…，
  linux/arm64，1.37 GB）默认 CMD 输出 `0.1.5-rc.3` 与
  `@agentclientprotocol/codex-acp 1.13.1`，镜像内 `node -v` = v24.21.0（`docker 实测`）。
  构建日志出现 npm 11 的 install-scripts 警告（dsh-subprocess-local/koffi/node-pty 等
  未执行）；本次 ACP stdio 链路与真实 run 不依赖它们，但若未来某个 agent 需要原生脚本，
  必须在 ARG 层显式 `--allow-scripts` 并重建（`docker 实测`/`npm`）。M4 新增的 claude CLI
  就属于这种情况：npm 11 默认没跑它的 postinstall，构建里显式执行了包内的 `install.cjs`
  放置原生二进制（§7.5），并保留“四个组件版本精确比对”的自检。
* 命名与冻结：`LOCAL_WEB_ACP_WORKER_IMAGE`（默认 `pi-glink-acp-worker:latest`）与 pi 面的
  `LOCAL_WEB_WORKER_IMAGE`分开；值在配置期冻结，运行期不再读环境（`宿主代码`/`合成实测`）。
  升级路径：改 ARG → 重建 → 边界测试（ARG 必须重新等于 §5.1 记录值）→ 重建后的容器内
  握手/最小 run。M4 把默认 CMD 从「两个 `--version` 的 `sh -c` 一行」换成镜像内自检脚本
  `acp-self-check`（与 e2b 模板同形），覆盖四个组件；边界测试同步更新，见 §7.5。

### 5.2 传输、挂载与容器身份

* `docker run --rm -i`：容器 stdio 就是 ACP 传输（`-i` 保持 stdin，无 TTY——TTY 会破坏
  LF 分帧）。宿主把 `docker run` 的 argv 交给 M1 的 `AcpRpcClient`，不新增第二套协议代码
  （`宿主代码`/`合成实测`/`docker 实测`）。
* 工作区：宿主 run 工作区以读写挂载到 `/workspace` 并作为容器工作目录；`session/new` 的
  `cwd` 是容器路径 `/workspace`（宿主路径在容器内不存在），run 记录的 `session_ready.cwd`
  也是该容器路径（`宿主代码`/`合成实测`）。产物因此直接落在宿主工作区，不需要回收通道。
* 每 run HOME：`<run>/acp-agent/home` 挂载到 `/home/acp` 并 `-e HOME=/home/acp`；
  `~/.codex`、`~/.dsh`、cache 都落在本 run 目录内，随 run 销毁，不碰操作员目录
  （`宿主代码`/`合成实测`/`docker 实测`）。
* 凭据形态：沿用 M2 的“只读复制进 run HOME”，再在容器内对每个凭据文件做**嵌套只读挂载**
  （`-v <run-home>/<dest>:/home/acp/<dest>:ro`）。HOME 本身保持可写（agent 必须能写会话与
  缓存），但凭据文件在容器内不可改写/删除（`宿主代码`/`合成实测`/`docker 实测`）。
* `PI_GLINK_ACP_ENV`：按**名字**传（`-e NAME`），值放在 docker CLI 自己的环境里，不进 argv
  （进程列表）；这是与文件注入并列的显式进程环境通道（`宿主代码`/`合成实测`）。
* 容器身份与对账：容器名、停止、对账完全复用 pi 面的 `container_name_for` / `stop_argv` /
  `confirm_absent` 约定；名字在 `docker run` 执行前持久化到 run 记录，在线 ledger 只按
  这个持久化身份探测（`宿主代码`/`合成实测`）。

### 5.3 资源限制与降权

* 与 pi 面共用同一份 `--cpus` / `--memory` / `--pids-limit` / `--cap-drop ALL` /
  `--security-opt no-new-privileges`（`_isolation_argv`），合成测试逐项比对两面 argv 相等
  （`宿主代码`/`合成实测`）。这些都是 `docker run` 实例参数，镜像本身不被改写。
* 容器内进程仍是 root（与 pi 面一致）：降权靠 cap-drop + no-new-privileges；没有
  `--user`。限制是**契约**而非实测 enforcement——默认测试集不连 Docker daemon，宿主不会
  声称 daemon 实际执行了哪个值。

### 5.4 实测结论（2026-09-25）

证据文件：`docs/evidence/acp-m3-docker-build.txt`（镜像）、`acp-m3-docker-agent-check.txt`
（容器内握手与挂载契约）、`acp-m3-docker-smoke.txt`（最小真实 run）。本节每条都标来源。

* **容器内握手**：用生产路径（`prepare_acp_runtime` + `build_acp_plan` + `AcpRpcClient`）
  在容器里跑 initialize/session/new（不发 prompt，不耗模型）：codex-acp 1.13.1 与
  dsh 0.1.5-rc.3 都协商回 `protocolVersion=1` 并拿到 `sessionId` 与
  configOptions（codex-acp：mode/collaboration_mode/model/reasoning_effort/fast-mode；
  dsh：model/reasoning_effort）。无凭据时 codex-acp 会报
  `-32000 Authentication required`，挂载生效后握手成功——与 M2 的凭据注入观测点一致
  （`docker 实测`）。
* **挂载契约**：同一 plan 的挂载下，容器内写 `/home/acp/.codex/auth.json` 得
  “Read-only file system”（tamper_exit=2），写 HOME 其它位置成功（home_write_exit=0），
  宿主上的凭据副本 sha256 不变；client.close 后按名 force-remove 报 “No such container”，
  `docker ps -a` 无同名残留（`docker 实测`）。
* **一次最小真实 run（dsh）**：宿主 HTTP API 创建 run（临时数据根、随机端口、300s 有界
  超时），终态 `finished`、`modelStopReason=end_turn`、`reportStatus=ok`，
  `/api/runs/<id>/report` 200（1025 bytes），`turn_finished` 先于 `run_finished`；
  28 条事件含 thinking/tool（web_search/web_fetch/write）/usage（替换口径，
  `basis=acp_usage_update`）与 session_ready（`cwd=/workspace`）；清理
  `released=true`、容器名持久化且 `docker ps -a` 无残留；宿主只按 `$!` 记录的 PID 发
  `kill -TERM`（退出状态 143），未用 `pkill -f`、未做名字通配删除（`docker 实测`）。
* **冒烟次数说明**：授权为“一次最小真实 run”；第一次题面是对话式短问，模型未写
  report.md（`stopReason=no_report`，链路正常），于是用显式要求写 report.md 的短题补跑
  一次，两次都记录在证据文件里（`docker 实测`）。
* **环境约束（Colima/本机）**：Colima 只把 home 树共享给 VM；数据根/工作区放在 `/tmp`
  时，绑定的文件与目录会被静默创建成空目录（首次探针即复现：容器内凭据变成目录，
  `session/new` 因缺少凭据失败）。本机验证因此全部放在 `$HOME` 下的临时目录；该约束对
  pi docker 面同样适用（它一直靠默认数据根 `~/.pi-glink` 在 home 下而没暴露）。
  约束本身不是代码行为，但会把“静默空挂载”变成运行期失败，因此写在这里（`docker 实测`）。

### 5.5 与 M2 的差异与风险

* **codex-acp 包替换**：0.16.0（旧包）→ 1.13.1（后继包）。M3a 只验证了容器内
  initialize/session/new；M3b 在 e2b 沙箱内也只验证握手；后继包的真实模型 run 仍待授权（§12）。
* **只读范围**：只读的是凭据文件（嵌套 `:ro` 挂载），HOME 本身可写——agent 必须能写
  sessions/cache；不是整个 HOME 只读（`宿主代码`/`docker 实测`）。
* **容器 root 与宿主挂载**：容器内是 root，但 macOS/Colima 下 bind mount 的属主仍是宿主
  用户；Linux 宿主上 root 写 bind mount 的 uid 归属与文件属主变化**待实测**（§12）。
* **共享路径约束**：Colima 只共享 home 树，数据根必须在共享路径内（见 §5.4）；
  不同 Docker 后端（Docker Desktop/Linux 原生）的共享范围与“非共享路径静默空挂载”行为
  待逐环境实测。
## 6. M3b 实测（2026-09-25，E2B 执行面）

M3b 把 ACP 族从 inproc/Docker 扩到 E2B 云沙箱。协议侧零新增：沙箱后台命令的 stdio 交给
同一个 M1 `AcpRpcClient`，投影、权限应答、回合边界与终态判定继续是同一份实现。本节每条
都标来源；证据文件：`docs/evidence/acp-m3-e2b-framing.txt`（帧完整性）、
`acp-m3-e2b-template.txt`（模板）、`acp-m3-e2b-agent-check.txt`（沙箱内握手）、
`acp-m3-e2b-smoke.txt`（一次真实 run）。

### 6.1 帧完整性（先行实测，结论正面）

E2B SDK 不给双向管道，只有 `commands.run(background=True, stdin=True)` 的
`CommandHandle`：宿主用 `send_stdin` 写、迭代 handle 读 stdout 分块。M3b 的第一步就是用
仓库 LF-only 分帧（`kernel/framing.py`）验证这个通道能不能承载 ACP 的 JSON-RPC；探针
（`scripts/spikes/acp_e2b/frame_integrity_probe.py` + 沙箱端回显 agent）在真实
`base` 沙箱测了 20 项，全部通过（`e2b 帧实测`）：

* **保序**：64 条逐条写入与 128 条突发写入（不等回包）都按调用顺序得到回包，agent 侧
  记录序号 `n` 连续、`id`/内容/原始字节哈希逐条一致。
* **字节一致**：每条记录的 `raw_sha256`（不含结尾 LF）与宿主发送的字节相等；`send_stdin`
  接受 `str` 与 `bytes`，没有截断或改写。
* **LF 分帧**：半帧 + 半帧（中间有 0.3s 间隔）合成为一条记录；一次写入两帧被拆成两条；
  agent 单次 `os.write` 连发三帧也被拆成三条；CRLF 写入者的尾部 CR 单独剥离；共享语料
  `tests/fixtures/lf-framing.json`（U+2028/U+2029 为内容）双向逐帧一致。
* **大帧**：64 KiB / 256 KiB / 1 MiB 单条记录双向通过（host→agent 记录 65598 / 262207 /
  1048640 字节，回包哈希与正文长度全等；agent→host 记录 65835 / 262444 / 1048877 字节）。
* **EOF**：`close_stdin()` 可用，agent 读到 EOF 后正常退出（exit 0）。
* **分块不按帧对齐**：两百多条小帧的测试里 stdout 共 293 个 chunk，max_chunk 32768，
  有 84 个 chunk 停在帧中间、2 个 chunk 含多帧——所以承载方**必须**保留 LF 分帧器，
  不能假设一块等于一帧。

结论正面，M3b 按计划实现（brief 的“结论负面就停止”未触发）。沙箱只按 `sandbox_id`
终止，无模式匹配清理（`e2b 帧实测`）。

### 6.2 dev 模板与钉版（E2B CLI 构建）

新增 `docker/e2b/Dockerfile.acp-e2b`，构建**独立 dev 模板** `glink-acp-worker`；生产模板
`glink-frontier-e2b` 与其任何 tag 均未被读取写入（只有 `template list` 的只读列出），
pi 面模板 `pi-glink-worker` 也未动（`e2b 模板实测`）。

* 钉版与 M3a 镜像一致：`@deepseek-ai/dsh@0.1.5-rc.3`、
  `@agentclientprotocol/codex-acp@1.13.1`，`FROM node:24.21.0-bookworm-slim`；构建期用
  ARG 精确比对两个 `--version`，默认 CMD 也是同一个自检（`e2b 模板实测`）。
* 构建必须用 E2B CLI（历史事故：Python SDK 的 `Template().from_dockerfile()` 会丢多行
  ENV）：`E2B_API_KEY=… npx -y @e2b/cli template create glink-acp-worker --dockerfile …
  --cpu-count 2 --memory-mb 4096`。实测 Template ID `oen0rq47o2sizakry5v3`，
  Build ID `3a75d2b3-57aa-4876-b2fd-fee5276155b5`，构建 36s（`e2b 模板实测`）。
* 两个真实构建坑（均写进 Dockerfile 注释与证据）：① E2B 构建会把 CMD 数组用空格拼成
  一行再交给 shell（日志 `Running start command: sh -c dsh --version && codex-acp
  --version`），`sh -c dsh --version` 实际是“启动 dsh、把 --version 当 $0”，报
  `--profile <name> is required`；修复：CMD 指向镜像内自检脚本 `acp-self-check`。
  ② E2B 构建解析会把 Dockerfile 文本里的 `\n` 吃成 `n`（日志里的 RUN 命令即为
  `printf '%sn' …`），生成的脚本没有换行、execve 报 “cannot execute: required file
  not found”；修复：改用逐行 `echo` 生成脚本。
* 构建后真实沙箱自检（`scripts/spikes/acp_e2b/template_selfcheck.py`）：沙箱默认用户
  `user`（uid 1001）、`HOME=/home/user`、工作目录 `/home/user/workspace`，`node` v24.21.0，
  `dsh --version` = 0.1.5-rc.3，`codex-acp --version` = `@agentclientprotocol/codex-acp
  1.13.1`；`/home/user/acp` 与 `/home/user/workspace` 可写，每 run 状态根
  `/home/user/acp/home` 可创建可写；envd 0.9.0（`e2b 模板实测`）。
* 升级路径：改 ARG → CLI 重建 → 边界测试（ARG 必须重新等于本节记录值）→ 沙箱自检 →
  最小 run。模板名/ID/build ID 是本节的事实源，`tests/test_kernel_acp_e2b.py` 卡住 ARG
  与本节记录的版本相等。M4 用同一模板 ID 重建、加入 claude 双包（新 Build ID 与四个组件
  的自检输出见 §7.5 与 `docs/evidence/acp-m4-claude-e2b-handshake.txt`）。

### 6.3 执行面实现与合成回归

* **沙箱生命周期沿用 pi 面**：`E2BAcpSandboxClient` 与 pi 的 `E2BSandboxClient` 共用
  `_create_sandbox` / `_recover_workspace` 与同一份冻结配置（`E2B_API_KEY` 走 `Sandbox.create`
  参数，不进环境）；销毁前把沙箱工作区拉回宿主工作区，`sandbox_id` 在沙箱创建后**立即**
  持久化（上传失败也保留身份，供台账对账）（`宿主代码`/`合成实测`/`e2b 实测`）。
* **每 run HOME 与凭据**：复用 `prepare_acp_runtime` 渲染的 `runs/<id>/acp-agent/home`
  （凭据源只读复制、0600），再上传到沙箱状态根 `/home/user/acp/home`：先把状态根
  `chmod 0700` 再上传（不让文件落在可遍历目录里），上传后把创建的目录 `chmod 0700`、
  每个文件 `chmod 0600`；agent 命令的 `HOME` 指向该根，
  `PI_GLINK_ACP_ENV` 作为显式进程环境通道（值不进命令行），凭据值并入该 run 的 redactor
  （`宿主代码`/`合成实测`/`e2b 实测`）。
* **stdio 桥接**：`SandboxCommandProcess` 把 `CommandHandle` 适配成 `subprocess` 形状
  （`send_stdin`/`close_stdin`/分块读/`poll`/`wait`/`kill`），`AcpRpcClient` 新增可注入的
  `spawn`，e2b 面把 `E2BAcpSandboxClient.spawn` 交给它——同一份客户端、同一份 LF 分帧，
  没有第二套协议实现（`宿主代码`/`合成实测`）。
* **装配**：`build_adapter(harness="acp", executor="e2b")` → `AcpSandboxExecutionAdapter`
  （`runner._acp_e2b_plan` 提供沙箱客户端）；`session/new` 的 `cwd` 是沙箱路径
  `/home/user/workspace`（宿主路径在沙箱内不存在），`session_ready` 额外带 `sandboxId` 与
  `template`（`宿主代码`/`合成实测`/`e2b 实测`）。
* **配置**：解除 acp+e2b 的启动拒绝；新增冻结旋钮 `LOCAL_WEB_E2B_ACP_TEMPLATE`
  （默认 `glink-acp-worker`），与 pi 面的 `LOCAL_WEB_E2B_TEMPLATE` 分开；`public_view()`
  仍不暴露 harness/模板（`宿主代码`/`合成实测`）。
* **合成回归**（`合成实测`）：新增 `tests/test_kernel_acp_e2b.py` 15 项——fake SDK
  组合（模板/key/超时/上传清单/0600 加固/命令与 env/生命周期）、用真实子进程 fake ACP
  agent 走完整桥接的端到端适配器与 run 生命周期（终态、report 回收、身份持久化、上传失败
  仍保留身份、云端失败不上报身份）、模板 ARG 钉版边界。既有断言按新义务更新并在注释里
  写明原义务/新义务（配置与装配层的 e2b 拒绝改为接受）。

### 6.4 真实沙箱握手（两个 agent，不消耗模型）

用生产装配路径（`build_config` → `prepare_acp_runtime` → `E2BAcpSandboxClient` →
`build_adapter`）在 `glink-acp-worker` 沙箱内只做 `initialize` + `session/new`
（`scripts/spikes/acp_e2b/sandbox_handshake.py`，不发 `session/prompt`）：

* dsh 0.1.5-rc.3：`protocolVersion=1`，`agentInfo={"name":"deepseek-harness-acp",
  "version":"0.0.1"}`，`sessionCapabilities` 等能力位，`configOptions` = model /
  reasoning_effort，`authMethods=[]`，`cwd=/home/user/workspace`（`e2b 握手实测`）。
* codex-acp 1.13.1：`protocolVersion=1`，`agentInfo={"name":"@agentclientprotocol/codex-acp",
  "version":"1.13.1"}`，`authMethods` = api-key / chat-gpt，`configOptions` 5 个
  （mode / collaboration_mode / model / reasoning_effort / fast-mode），
  `cwd=/home/user/workspace`（`e2b 握手实测`）。
* 两侧凭据源文件 sha256 前后一致、源 mtime 早于本次运行；沙箱按 id 终止、无残留
  （`e2b 握手实测`）。与 M2/M3a 观测一致：无凭据时 codex-acp 的 `session/new` 会以
  Authentication required 拒绝（M3a 观测点，本次注入后握手成功）。

### 6.5 一次真实冒烟（dsh，经宿主 HTTP API）

`harness=acp + executor=e2b`、临时数据根、随机端口 55500、300s 有界超时、明确要求写
`report.md` 的短题（`scripts/spikes/acp_e2b/smoke_host.py`，`e2b 实测`）。该 run 在实现
提交 `235b86a` 的代码上产生；此后只调整了凭据上传前的状态根 `chmod` 顺序（§6.3/§6.6），
不影响本小节断言的事实（流式/终态/report 回收/沙箱按 id 对账）；最终代码的沙箱内凭据路径
由 §6.4 的握手重新实测（`e2b 握手实测`）：

* 终态 `finished`、`modelStopReason=end_turn`、`reportStatus=ok`，时长 29.6s、28 条事件；
  `GET /api/runs/<id>/report` 200（994 bytes），报告内容为 dsh 写的两点结论 + 1 条已核验
  来源；`turn_finished` 先于 `run_finished`。
* 流式：SSE 在 run 期间收到全部 28 帧（`run_started` → `session_ready`（8.1s，cwd 为沙箱
  路径）→ `turn_started` → thinking/usage/tool（web_search + 3 次 web_fetch）→
  `assistant_delta` → `turn_finished` → `run_finished`），与 `events.jsonl` 时间线一致。
* 沙箱：`sandbox_id=i9865hh296nza5lt7az2l` 持久化；`cleanup={"released":true,
  "external":{"sandbox":"i9865hh296nza5lt7az2l"}}`；宿主日志有 `DELETE /v2/sandboxes/<id>
  → 204`，随后 `confirm_sandbox_absent(id)` 返回“SDK 确认沙箱不存在”。
* 清理只操作已知身份：宿主进程由脚本持有的 PID `terminate()`（exit -15），沙箱按 id；
  未用 `pkill -f`、无名字通配；临时数据根（含每 run HOME 的凭据副本）取证后删除。
* 脱敏：`scripts/check-no-secrets.py` 对该数据根退出码 0（扫描文件 4 个、真实命中 0 处）；
  证据文件本身密钥形状 0 命中（`e2b 实测`）。
* 次数说明：本任务授权“一次最小真实冒烟 run”，本文件即那一次；运行前的管道演练使用不
  存在的模板（不消耗模型、不创建沙箱），不计入通过项（`e2b 实测`）。

### 6.6 与 M2/M3a 的差异与风险

* **承载面差异只在传输**：e2b 面没有容器挂载，所以每 run HOME 靠“上传 + 沙箱内 chmod”，
  凭据副本在上传后仍留在宿主 run 目录（0600，随 run 目录生命周期），不是只读挂载；
  `files.write` 没有 mode 参数，0600 由紧随其后的 `chmod` 施加（已先把状态根 0700，且沙箱
  本身是单 run 单用户），因此没有容器面的嵌套只读挂载这一层——沙箱内无法验证“凭据文件不可
  改写/删除”的容器级只读语义（`宿主代码`/`e2b 实测`）。
* **资源上限**：E2B SDK 这条路径只暴露 lifetime（与 pi e2b 面相同），没有 cpus/memory
  参数；宿主只声明它真的要求过的边界（`宿主代码`）。
* **模板独立性**：`glink-acp-worker` 是 dev 模板，未发布/未移动任何 tag；升级同样需要
  重新构建与沙箱自检（§6.2）。
* **仍未实测**：`@agentclientprotocol/codex-acp@1.13.1` 在 e2b 面的真实模型 run、
  e2b 面的真实 `session/cancel` 响应时延（§12）。

## 7. M4 实测（2026-09-25，claude-code-acp 接入）

本节每条都标来源。证据文件：`docs/evidence/acp-m4-claude-handshake.txt`（inproc 握手与
凭据/首启形态，不消耗模型）、`acp-m4-claude-smoke.txt`（一次最小真实 run）、
`acp-m4-claude-docker-handshake.txt`（镜像重建与三个 agent 容器内握手）、
`acp-m4-claude-e2b-handshake.txt`（dev 模板重建与沙箱握手）。协议侧零新增：claude-code-acp
走的是与 dsh/codex-acp 同一份 `AcpRpcClient`、投影与生命周期判定。

### 7.1 版本与入口

| 组件 | 钉版与入口 | 实测事实 |
|---|---|---|
| claude-code-acp（M4 钉版） | `@zed-industries/claude-code-acp@0.16.2`（npm `latest`），入口 `claude-code-acp`（无参数即 stdio ACP server） | npm 告警该包已 deprecated（"renamed to @agentclientprotocol/claude-agent-acp"）；M4 按 brief 钉 0.16.2，**M6 已完成后继包迁移，见 §9**（`npm`/`claude 实测`） |
| 被驱动的 claude CLI | 适配器依赖 `@anthropic-ai/claude-agent-sdk@0.2.44`，后者自带 vendor `cli.js`（版本 2.1.44）；环境变量 `CLAUDE_CODE_EXECUTABLE` 可覆盖 | SDK 按路径后缀识别“原生二进制”并直接 spawn；指向本机 claude（2.1.281）时子进程命令行实测为 `…/bin/claude --output-format stream-json … --permission-mode default`（`claude 实测`/`宿主代码`） |
| 镜像/模板内 claude CLI | `@anthropic-ai/claude-code@2.1.282`（构建时 npm `latest`） | 官方包用平台可选依赖 + postinstall 放置原生二进制；npm 11 默认不跑生命周期脚本，构建里显式执行包内 `install.cjs`（`镜像自检`/`e2b 模板实测`） |

inproc 面的钉版调用是 `npx -y @zed-industries/claude-code-acp@0.16.2`（不全局安装；M6 起
为 `npx -y @agentclientprotocol/claude-agent-acp@0.81.2`，见 §9）；因为
每 run 的 `HOME` 是新的，npx 缓存也随 run 新建，探针用 `PI_GLINK_ACP_ENV` 注入
`npm_config_cache` 共享一份缓存，避免冷下载超过 initialize 的 60s 上限（`宿主代码`/`claude 实测`）。

### 7.2 能力矩阵（initialize / session/new 响应原文）

| 字段 | dsh 0.1.5-rc.3 | codex-acp 0.16.0 / 1.13.1 | claude-code-acp 0.16.2 | opencode 1.18.32 |
|---|---|---|---|---|
| `protocolVersion` | 1 | 1 | 1 | 1 |
| `agentInfo` | `{"name":"deepseek-harness-acp","version":"0.0.1"}` | `{"name":"@agentclientprotocol/codex-acp","version":"1.13.1"}` | `{"name":"@zed-industries/claude-code-acp","title":"Claude Code","version":"0.16.2"}` | `{"name":"OpenCode","version":"1.18.32"}`（`opencode 实测`） |
| `loadSession` | 未声明 | `true` | `true` | `true`（`opencode 实测`） |
| `sessionCapabilities` | `close`/`list`/`resume` | `list`/`resume`/`close` | `fork`/`list`/`resume`（无 `close`） | `close`/`fork`/`list`/`resume`，值均为空对象 `{}`（`opencode 实测`） |
| `promptCapabilities` | image=false, audio=false, embeddedContext=false | image=true, embeddedContext=true | image=true, embeddedContext=true | image=true, embeddedContext=true（未声明 audio）（`opencode 实测`） |
| `mcpCapabilities` | `{http: true}` | `{http, sse, acp 各布尔}` | `{http: true, sse: true}` | `{http: true, sse: true}`（无 `acp` 键）（`opencode 实测`） |
| `authMethods` | `[]` | api-key / chat-gpt（env_var） | `[claude-login]`（描述为“Run `claude /login` in the terminal”） | `[opencode-login]`（描述为“Run `opencode auth login` in the terminal”）（`opencode 实测`） |
| `session/new` 返回 | `sessionId` + `configOptions`（model、reasoning_effort） | `sessionId` + `modes` + `configOptions`（5 项） | `sessionId` + `models`（default/opus/opus[1m]/haiku）+ `modes`（default/acceptEdits/plan/dontAsk/bypassPermissions）；**`configOptions` 为空数组** | `sessionId` + `modes`（空对象）+ `configOptions`（`model`、`mode`；选中具体模型后联出 `effort`）（`opencode 实测`） |
| 会话配置面 | `session/set_config_option` 可用 | `session/set_config_option` 可用 | 无 `configOptions`：`PI_GLINK_ACP_SESSION_CONFIG` 对它不适用（配了会失败闭环，缺省不配即是无操作）（`claude 实测`） | `session/set_config_option` 可用；实测 `model=openai/gpt-5.6-sol-fast` 生效，`model=…` 后 `configOptions` 多出 `effort`（`opencode 实测`） |

补充实测（M4 钉版 0.16.2 的记录；M6 后继包 0.81.2 的逐条差异见 §9.2）：

* `session/new` 后立即广播 `available_commands_update`（claude 本次 47 条；opencode 3 条：
  customize-opencode、init、review）；宿主对 ACP 族仍拒绝一切 `/` 开头 prompt，agent 声明命令
  不改变准入门禁（`claude 实测`/`opencode 实测`；§2 词表不变）。
* opencode 的真实模式选择不在 `modes`（空对象）而在 `configOptions.mode`（build/plan，
  默认 `build`）——同一份 `session_ready` 记录里两者都落地，所以“模式读哪一处”不靠猜
  （`opencode 实测`）。
* 宿主客户端声明仍是全空（无 `fs/*`、无 `terminal/*`、无 `auth.terminal`），agent 在自己的
  文件系统上工作；`clientInfo` 从 M4 起带 `version`（见 §7.7）（`claude 实测`/`宿主代码`）。

### 7.3 凭据与首启形态（inproc 面实测）

这是 M4 需要重新测量的部分，因为 claude 的登录态与其他 agent 的“凭据文件”形态不同。
旧假设（brief 背景里写的“OAuth 在 macOS Keychain、Keychain 按用户不按 HOME”）**部分不成立**，
实测事实如下（每条都有现场命令，值不落证据；`claude 实测`）：

1. **Keychain 可达但本机条目为空。** CLI 用的读取命令
   `security find-generic-password -a "$USER" -w -s "Claude Code-credentials"` 退出码 0、无交互提示；
   但条目值里 `claudeAiOauth.accessToken` / `refreshToken` 长度均为 0——它是空壳，不提供可用登录态。
2. **真正的 inproc 凭据是 `~/.claude/settings.json` 的 `env` 块**（本机指向第三方 Anthropic
   兼容端点 + `ANTHROPIC_AUTH_TOKEN`）。同一份 settings 下：本机 `claude` 2.1.281 `auth status`
   报 `loggedIn=true / authMethod=oauth_token`；去掉复制件里的 `ANTHROPIC_AUTH_TOKEN`/
   `ANTHROPIC_BASE_URL` 立刻变 `loggedIn=false`。即登录态来自 settings 的 env，不来自 Keychain。
3. **真实 settings.json 不能整文件复制进 run。** 它含约 30 条 hooks（Muxy/Otty/Orca/Herdr/
   harness notify）、`enabledPlugins` 与 `statusLine`；实测整文件复制后，SessionStart hook 立即在
   宿主上拉起 `~/.harness/cli/notify.mjs` 并把操作员真实的 `~/.claude/projects` 等目录传进去。
   正确形态是只把 `env` 块抽成一份运行期 settings（0600），经 `PI_GLINK_ACP_CREDENTIAL_FILES`
   只读复制进 `HOME/.claude/settings.json`（`source=dest`），源文件 sha256 前后一致。
4. **`~/.claude.json` 不需要复制。** 全新 HOME 首次启动时 CLI 自行创建 `.claude.json` 与
   `.claude.json.backup.*`（含 firstStartTime/userID/GrowthBook 缓存），不依赖预先存在的文件；
   initialize + session/new 在无任何凭据时也能完成。
5. **驱动哪个 CLI 会影响 auth 判定。** SDK 自带 cli.js 2.1.44 在同一份运行期 settings 下
   `auth status` 报 `loggedIn=false`，本机 2.1.281 报 `true`；inproc 冒烟因此按适配器官方通道设
   `CLAUDE_CODE_EXECUTABLE=本机 claude`（2.1.281），而不是赌 2.1.44 是否会在真实请求上应用
   settings env（`claude 实测`）。**M6 重测**：后继包的 SDK 0.3.280 原生 cli 2.1.280 认
   settings env（§9.3），该绕行不再是必选项。
6. **每 run HOME 落点**：`.claude/settings.json`（0600，注入值并入该 run 的 redactor）、
   `.claude.json`（0600）、`.claude/{backups,debug,sessions,projects}` 与 `Library/Caches`
   都在 run HOME 内，随 run 销毁；docker 面再对凭据文件加嵌套只读挂载，e2b 面在沙箱内
   0600 加固（§5.2/§6.3 的同一机制）（`claude 实测`/`docker 实测`/`e2b 握手实测`）。

结论：inproc 形态 = **运行期 `.claude/settings.json`（env 块，CREDENTIAL_FILES 0600 复制）+
`CLAUDE_CODE_EXECUTABLE` 指向宿主 claude CLI**；`PI_GLINK_ACP_ENV` 的
`ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN` 是等价的环境变量通道（值同样并入 redactor，见 §7.7）。
macOS Keychain 只在 inproc 面存在，docker/e2b 没有它——那两面的真实模型 run 需要宿主显式提供
API key，否则属预期阻塞。

### 7.4 权限白名单（决策：claude 研究 run 启用最小集合）

* claude-code-acp 的 `canUseTool` 对 WebFetch 与 Write/Edit 逐次发 `session/request_permission`
  （选项 `allow_always`/`allow_once`/`reject_once`），但请求的 `toolCall` **不带** `kind`
  （适配器源码只传 `toolCallId`/`rawInput`/`title`；`claude 实测`/`npm`）。M2 的
  “无 kind 即拒绝”门控因此会让研究 run 写不出 report.md。
* **决策**：给 ACP 族定义最小白名单 `PI_GLINK_ACP_PERMISSION_ALLOW=allow_once,read,search,fetch,edit`
  ——只放行研究 run 需要的 read/search/fetch 与写（edit）四类工具，且只用一次性放行
  （`allow_once`）；`execute`/`think`/`switch_mode`/`other` 保持拒绝，未分类请求（请求与
  `tool_call` 通知都没有 kind）仍拒绝。默认（未配置）行为不变：fail-closed 全拒。
* 为使 kind 可用，宿主按同一 `toolCallId` 把 agent 已在 `tool_call` 通知里公布的 kind 用于
  权限分类（§1.7）；事件 `kernel_status` 记录 `decision`/`policy`/`toolKind`/`toolKindSource`。
  依据：kind 是 agent 自己发布的 ACP 字段，比按 `title` 或 `rawInput` 猜更硬；按标题匹配会
  把 i18n/文案变化引入安全决策（`claude 实测`/`宿主代码`）。
* 真实 run 证据：`acp-m4-claude-smoke.txt` 的 2 条 `acp_permission_allowed`（`kind=fetch`
  与 `kind=edit`，`toolKindSource=tool_call`）；本次未出现 execute 类请求，execute 仍被拒绝
  由合成回归
  （`tests/test_kernel_acp_runner.py::test_acp_permission_allowlist_refuses_a_published_execute_kind`）
  与默认空集路径钉住（`claude 实测`/`合成实测`）。
* 粒度限制（如实记录）：白名单按 **tool kind** 放行，不能限定“只准写 report.md”；
  需要路径级策略时属后续能力（dsh 的请求不含 kind，codex-acp 的 `mode` 会话选项仍是更窄
  的替代面，两者默认策略不变）（`宿主代码`）。

### 7.5 镜像与 dev 模板（本地重建 + 无模型握手自检）

* `docker/Dockerfile.acp-worker` 新增 claude 双包层：`@zed-industries/claude-code-acp@0.16.2` +
  `@anthropic-ai/claude-code@2.1.282`，与既有 dsh/codex-acp 同一镜像；npm 11 默认跳过生命周期
  脚本，构建里显式跑 `install.cjs` 放好原生二进制，并保留四个组件的构建期版本比对。默认 CMD
  从两个 `--version` 的一行改为镜像内自检脚本 `acp-self-check`（与 e2b 模板同形，
  claude-code-acp 用 `npm ls -g` 复验）。重建后实测：`docker run --rm` 输出
  `0.1.5-rc.3` / `@agentclientprotocol/codex-acp 1.13.1` / `2.1.282 (Claude Code)` / `0.16.2`；
  容器内握手（生产装配路径）claude-code-acp 成功、dsh 回归成功、codex-acp 的 session/new 在
  无凭据时报 `Authentication required`（与 M3a 观测一致，本任务禁触 `~/.codex` 未注入凭据）；
  三个容器都按持久化名字确认不存在（`docker 实测`）。
* `docker/e2b/Dockerfile.acp-e2b` 同步同一对钉版并用 E2B CLI 重建 dev 模板 `glink-acp-worker`
  （同一 Template ID `oen0rq47o2sizakry5v3`，新 Build ID `4a5fbaea-d46b-44fe-81ef-0255d854c6d7`，
  构建 1m43s）；构建期与 finalize 的 start command 都跑了 `acp-self-check`，四个组件版本正确；
  真实沙箱内 claude-code-acp 完成 initialize + session/new（cwd `/home/user/workspace`，
  `authMethods=[claude-login]`，`configOptions` 空），沙箱按 id 终止无残留；生产模板
  `glink-frontier-e2b` 与 pi 模板 `pi-glink-worker` 未被触碰（`e2b 模板实测`/`e2b 握手实测`）。
* CMD/自检形态的既有断言按新义务更新（`tests/test_kernel_acp_docker.py` 注释写明原义务：
  两个 `--version`；新义务：脚本覆盖四组件），ARG 钉版边界测试扩展到 claude 双包（`合成实测`）。

### 7.6 一次 inproc 最小真实冒烟

`harness=acp + executor=inproc`、临时数据根、随机端口、300s 有界超时、明确要求写 `report.md`
的短题；宿主 HTTP API 创建 run，SSE 与终态从 HTTP 面核对（`claude 实测`，证据
`acp-m4-claude-smoke.txt`，授权 ≤2 次中的第 1 次）：

* 终态 `finished`、`modelStopReason=end_turn`、`reportStatus=ok`，时长 47.6s、141 条事件；
  `GET /api/runs/<id>/report` 200（714 bytes），报告为 2 条要点 + 1 条可核验来源；
  `turn_finished` 先于 `run_finished`。
* 流式：SSE 收到全部 141 帧（`run_started` → `session_ready`（2.2s）→ `turn_started` →
  thinking/`assistant_delta` → `tool_started`/`tool_updated`/`tool_finished`（Fetch、Write）→
  `turn_finished` → `run_finished`），与 `events.jsonl` 时间线一致。
* 权限：2 条 `acp_permission_allowed`（fetch、edit；kind 均来自 `tool_call` 通知），无拒绝。
* 清理：宿主进程按脚本持有的 PID `terminate()`（exit -15）；run 期间采样的 4 个后代进程
  （npm exec → node claude-code-acp → claude → claude.exe）结束后全部退出，无 leftover；
  `cleanup={"released":true,...}`；容器/沙箱身份均为 None；未用 `pkill -f` 或名字通配。
* 脱敏：`scripts/check-no-secrets.py` 对该数据根退出码 0（扫描 4 个文件、真实命中 0 处）；
  events 里 `ANTHROPIC` 0 命中；真实 `~/.claude/settings.json` sha256 前后一致。
* 本机语义如实记录：该 run 由宿主 claude 配置的第三方 Anthropic 兼容端点驱动（settings env
  的 AUTH_TOKEN + BASE_URL），不是 Claude 订阅；brief 的“Claude 订阅”描述与实测不符，
  以本节为准（`claude 实测`）。

### 7.7 宿主侧实现变更（M4）

* `clientInfo` 补 `version`（§1.2）；合成回归
  `test_acp_initialize_identifies_the_client_with_a_version` 钉住。
* 权限 kind 关联（§1.7/§7.4）：`AcpExecutionAdapter` 记住每个 `toolCallId` 的 kind（上限 512），
  `acp_permission` 帧加 `toolKindSource`；合成回归用 fake agent 复刻 claude-code-acp 的
  “先 `tool_call` 通知、后无 kind 权限请求”顺序（`FAKE_ACP_PERMISSION_AFTER_TOOL_CALL`）。
* `PI_GLINK_ACP_ENV` 里凭据形状的取值并入该 run 的 redactor（`_env_credential_values`）：
  兑现 §4.5/方案 §4.5 已声明的“注入值并入 redactor”，只按变量名（`key/token/secret/…`）
  认定，普通旋钮（如 `ACME_MODE=workspace`）不参与脱敏（`宿主代码`/`合成实测`）。
* live 探针：`scripts/spikes/acp_inproc/{handshake,smoke_host}.py`、
  `scripts/spikes/acp_docker/handshake.py`，e2b 探针扩展 `--agent claude-code-acp`
  （`agent 文档/消费脚本同步更新`）。

### 7.8 与既有面的差异与风险

* **claude-code-acp 与它的后继包**：0.16.2 已被 npm 标记 deprecated（改名
  `@agentclientprotocol/claude-agent-acp`）；M4 按 brief 钉旧包，升级路径是改 ARG →
  重建 → 边界测试 → 握手/冒烟。**M6 已按该路径完成迁移**（ARG/包名/入口、镜像与 dev
  模板重建、边界测试、握手与两次冒烟），旧包钉版记录留作 M4 历史，见 §9。
* **usage 面**：本次 claude 的 141 条事件里没有 `usage_update`（终态 `usage=null`），适配器
  不发就该如实空着，不伪造；token 用量在 claude 的 ACP 面上暂不可得（`claude 实测`）。
* **重复的 `tool_call` 通知**：claude-code-acp 对同一个 `toolCallId` 会先后发两条
  `tool_call`（先程序名、后完整标题），产品面因此出现成对的 `tool_started`；这是适配器
  行为，不是宿主丢帧或重复执行，本次如实记录，未改投影语义（`claude 实测`/`宿主代码`）。
* **`session/cancel`、docker/e2b 真实模型 run**：仍未实测，见 §12；ACP 续接已在 M7 补齐（§10）。
* **Keychain 形态因机而异**：本机条目为空壳，其他机器若真用订阅登录，keychain 读路径同样
  “按用户不按 HOME”，但 inproc 之外仍不可用；升级/换机时应重跑 §7.3 的检查（`claude 实测`）。

## 8. M5 实测（2026-09-25，opencode 接入）

本节每条都标来源。证据文件：`docs/evidence/acp-m5-opencode-handshake.txt`（inproc 握手与
凭据/状态目录形态，不消耗模型）、`acp-m5-opencode-smoke.txt`（inproc 一次最小真实 run）、
`acp-m5-opencode-docker-handshake.txt`（镜像重建、容器内握手与凭据只读挂载）、
`acp-m5-opencode-docker-smoke.txt`（docker 面一次最小真实 run）、
`acp-m5-opencode-e2b-handshake.txt`（dev 模板重建与沙箱握手）。协议侧零新增：opencode
走的是与前三个 agent 同一份 `AcpRpcClient`、投影与生命周期判定。

### 8.1 版本与入口

| 组件 | 钉版与入口 | 实测事实 |
|---|---|---|
| opencode | `opencode-ai@1.18.32`（npm `latest`），入口 `opencode acp` | 本机 npm 全局装的是同族的 1.18.18；两者的 `initialize` 能力矩阵一致，只有 `configOptions.model` 的候选列表随版本/凭据变化。inproc 探针用 `npx -y opencode-ai@1.18.32 acp`（不全局安装）；镜像与 dev 模板用同一 ARG 安装（`npm`/`opencode 实测`） |
| 包形态 | wrapper + 平台可选包，`bin.opencode` 指向 `bin/opencode.exe` | `postinstall.mjs` 从平台包复制二进制并自验；镜像/模板显式再跑一次（npm 生命周期脚本不保证执行），构建期与 ARG 精确比对（`npm`/`镜像自检`） |

### 8.2 握手与事件变体

* inproc 握手（生产装配路径，不发 prompt）：`protocolVersion=1`、
  `agentInfo={"name":"OpenCode","version":"1.18.32"}`，能力矩阵见 §7.2；`cwd` 是 run
  工作区；初始 `configOptions` 是 `model`（默认 `opencode/big-pickle`，OpenCode Zen）+ `mode`
  （build/plan）；`authMethods` 只有 `opencode-login`（`opencode 实测`）。
* **无凭据也能握手**：`session/new` 不需要先登录（与 codex-acp 不同）；注入 `auth.json` 后
  `model` 选项里出现 OpenAI 系模型，这是凭据生效的直接观测点（`opencode 实测`）。
* 一次真实 run 观测到的 `session/update` 变体：`agent_message_chunk`、`tool_call`、
  `tool_call_update`、`config_option_update`、`available_commands_update`、`usage_update`
  （本次未出现计划/思考块）。其中 **`config_option_update` 是 §4.1 未定义的变体**：宿主按
  「不猜语义」原则记一条 `kernel_status`（`acp_update_not_projected`，info），不伪造产品
  事件；本次它出现在 `session_ready` 之前（宿主应用 `model=…` 的联动），产品词表不变
  （`opencode 实测`）。
* `available_commands_update` 本次 3 条（customize-opencode、init、review），走 §2 的
  warning 提示；`/` 开头 prompt 的准入门禁不因 agent 声明命令而放宽（`opencode 实测`）。
* 工具调用形状：`tool_call` 先到（`title` 为 webfetch 的 URL、`kind` 显式给出），随后
  `tool_call_update` 推进 status；`apply_patch` 成功写 `report.md` 后以 completed 收口
  （`opencode 实测`/`宿主代码`）。

### 8.3 凭据与状态目录（inproc 面实测）

* **注入**：`PI_GLINK_ACP_CREDENTIAL_FILES=~/.local/share/opencode/auth.json=.local/share/opencode/auth.json`
  ——源只读（探针前后 sha256 一致）、目标文件 0600、父目录 0700（`opencode 实测`）。
* **redactor**：opencode 的 OAuth 字段名是 `access`/`refresh`（不带 `token` 后缀），通用规则
  抓不到；M5 按**精确字段名**扩充（`openai.access` 命中、`openai.accessMode` 不命中），本次
  4 条值并入该 run 的 redactor（openai/codex 各一对 access/refresh）。`type`/`expires`/
  `accountId` 不并入（账号标识不是凭据）（`宿主代码`/`合成实测`/`opencode 实测`）。
* **每 run HOME 落点**（inproc 实测，全部在 run 目录内、随 run 销毁）：
  `.local/share/opencode/auth.json`（0600，注入副本）、`.local/share/opencode/{log/opencode.log,
  opencode.db, opencode.db-shm, opencode.db-wal, repos/}`、`.local/state/opencode/locks/<hash>.lock/`、
  `.config/opencode/{.gitignore, opencode.jsonc}`（自建空配置，50 字节）、`.cache/opencode/bin`
  （`opencode 实测`）。
* **真实全局配置不进入 run**：`~/.config/opencode/opencode.json`（操作员自定义 provider 模型
  与插件列表）在 run HOME 里不存在，run 内也没有读它——用只放 `auth.json` 的假 HOME 直跑
  `opencode models openai`，模型列表与真实 run 一致；`gpt-6-astra` 系列是 1.18.32 的内置模型，
  不是配置泄漏（`opencode 实测`）。
* **run HOME 的配置通道可用**（实测登记，本任务未据此预置渲染器）：把一份只定义
  `zzz-marker-6` 的 `opencode.json` 经 CREDENTIAL_FILES 放进 `HOME/.config/opencode/`，该模型
  随即出现在 `configOptions.model` 里（`opencode 实测`）。
* **凭据刷新实测（inproc）**：真实 run 结束后 run HOME 的 `auth.json` 副本与源不同
  （4300 vs 4285 字节）：openai 的 `access`/`refresh` 都被刷新、`expires` 从 2026-09-18
  更新到 2026-10-05；`codex` 未变（本次未用）；**源文件 sha256 前后一致**。即刷新写回只落
  在 run 的副本上，不碰操作员文件（`opencode 实测`）。
* 环境隔离沿用既有清单：`PASSTHROUGH_ENV` 只透传 `XDG_CONFIG_HOME`/`XDG_CACHE_HOME`（本机
  未设置），`XDG_DATA_HOME` 不在清单——所以 `~/.local/share`、`~/.config`、`~/.cache` 必然
  落在 run HOME，真实操作员目录不被 opencode 读写（`宿主代码`/`opencode 实测`）。

### 8.4 权限（决策：不需要白名单，fail-closed 默认不变）

* **实测**：一次真实 run 的 53 条事件里 `acp_permission_*` 0 条；两次 `webfetch` 与一次
  `apply_patch`（写 `report.md`）都直接执行，opencode 没有发 `session/request_permission`。
  该 run 虽然按 M4 最小集合传了 `PI_GLINK_ACP_PERMISSION_ALLOW=allow_once,read,search,fetch,edit`，
  但**白名单从未被引用**（没有请求就没有决策）：run 的成功不依赖它（`opencode 实测`）。
  这与 claude-code-acp（§7.4 逐次询问）形成对照，与 dsh/codex-acp 同类。
* **源码形态**（只读参考上溯版本）：请求选项固定为 `once`(allow_once)/`always`(allow_always)/
  `reject`(reject_once)；请求的 `toolCall` 由 `pendingToolCall` 生成、**带 `kind`**
  （webfetch→fetch、edit/write/apply_patch→edit、read→read、grep/glob→search、bash/shell→execute、
  task→think、其余 other），客户端未声明 `requestPermission` 时 opencode 自行按 reject 处理
  （fail-closed）。因此若将来出现请求，宿主的 tool-kind 白名单会以 `toolKindSource=request`
  分类，不需要 M4 那种按 `toolCallId` 回查通知（`opencode 源码`/`宿主代码`）。
* **决策**：**不为 opencode 启用白名单**；`PI_GLINK_ACP_PERMISSION_ALLOW` 默认空集不变，M4 的
  最小集合仍然只属于 claude-code-acp 的运行配置。哪些工具会触发 prompt（源码里的 `bash`、
  外部目录路径等）本轮未在真实 run 观测到，记入 §12（`opencode 实测`/`opencode 源码`）。

### 8.5 一次 inproc 最小真实冒烟

`harness=acp + executor=inproc`、临时数据根、随机端口、300s 有界超时、明确要求写 `report.md`
的短题；宿主 HTTP API 创建 run，SSE 与终态从 HTTP 面核对（`opencode 实测`，证据
`acp-m5-opencode-smoke.txt`；授权为 inproc 1 次，本文件即那一次）：

* 终态 `finished`、`modelStopReason=end_turn`、`reportStatus=ok`，时长 19.8s、53 条事件；
  `GET /api/runs/<id>/report` 200（336 bytes），报告为 2 条要点 + 1 条已核验来源；
  `turn_finished` 先于 `run_finished`。
* 流式：SSE 收到全部 53 帧（`run_started` → `kernel_status`（`config_option_update`）→
  `kernel_status`（available commands）→ `session_ready` → `turn_started` → 两个 `webfetch`、
  一个 `apply_patch` 的 `tool_started`/`tool_updated`/`tool_finished` → 36 条 `assistant_delta`
  → `usage_reported` → `turn_finished` → `run_finished`）。
* usage：`used=6145`、`size=1050000`、`cost={amount:0,currency:USD}`、`basis=acp_usage_update`
  （替换语义，终态采用最后一个快照）。
* 权限：0 条 `acp_permission_*`（见 §8.4）。
* 清理：宿主进程按脚本持有的 PID `terminate()`（exit -15）；运行期采样到 2 个后代
  （`npm exec opencode-ai@1.18.32 acp` → `opencode acp`），结束后都已退出；
  `cleanup={"released":true,...}`；容器/沙箱身份均为 None；未用 `pkill -f` 或名字通配。
* 脱敏：真实 `~/.local/share/opencode/auth.json` sha256 前后一致；证据文件与检查脚本对数据根
  0 命中（见证据文件尾部的扫描记录）。

### 8.6 镜像与 dev 模板（本地重建 + 无模型握手自检）

* `docker/Dockerfile.acp-worker` 新增 opencode 层：`ARG OPENCODE_AI_VERSION` 与既有四项同一次
  `npm install -g`，安装后显式再跑包内 `postinstall.mjs`（npm 11 的 install-scripts 策略会跳过
  它，构建日志里点名的就有 `opencode-ai (postinstall: node ./postinstall.mjs)`），并保留五组件的
  构建期版本比对；默认 CMD/`acp-self-check` 增至五个组件（`opencode --version`）。
* **本地重建实测**（Docker 29.2.1 / Colima）：镜像 `pi-glink-acp-worker:latest` =
  `sha256:fbf2707319e664ab91e8ecf5c23a2bfa9f42890da4acbba7dc53ef736fcb40e3`（linux/arm64，
  `docker inspect` 527357716 字节），构建期自检输出五组件版本且 `grep -qx "${OPENCODE_AI_VERSION}"`
  通过。本机到官方 deb/npm 源很慢（apt 阶段 8 分钟、npm 约 60 KB/s），按 Dockerfile 已声明的
  覆盖参数改用 `mirrors.aliyun.com` + `registry.npmmirror.com` 重建（组件版本不变）；第一次
  官方源构建在 npm 阶段中止、未产出镜像（`docker 实测`/`镜像自检`）。
* **容器内握手**（生产装配路径，不发 prompt）：`opencode acp` 在容器里完成 initialize +
  session/new，`cwd=/workspace`、`agentInfo={"name":"OpenCode","version":"1.18.32"}`、
  `PI_GLINK_ACP_SESSION_CONFIG=model=openai/gpt-5.6-sol-fast` 生效（`configOptions` 联动出
  `effort`）；容器按持久化名字对账无残留（`docker 实测`）。
* **凭据只读契约**（同一 plan 的挂载参数）：容器内 `HOME/.local/share/opencode/auth.json`
  0600、4285 字节、sha256 与宿主副本一致；追加写失败（`Read-only file system`，exit 2）、
  删除失败（`Device or resource busy`，exit 1），而 HOME 其它位置可写（exit 0）——与 M3a/M4
  的嵌套只读挂载语义一致（`docker 实测`）。
* **e2b dev 模板**：`docker/e2b/Dockerfile.acp-e2b` 同步同一钉版与同一套自检，用 E2B CLI 重建
  `glink-acp-worker`（同一 Template ID `oen0rq47o2sizakry5v3`，新 Build ID
  `ef0637fd-9b6d-47f4-bef0-de94bdb3ff23`，构建 2m15s）；构建期与 finalize 的 start command
  都打印五组件版本（opencode `1.18.32`），npm 同样跳过它的 postinstall、由 Dockerfile 显式
  补跑。真实沙箱内 opencode 完成 initialize + session/new（`cwd=/home/user/workspace`、
  `authMethods=[opencode-login]`、`configOptions` = model/mode），每 run HOME 的 auth.json
  上传后 sha256 与源一致、沙箱按 id 终止无残留；生产模板 `glink-frontier-e2b` 与 pi 模板
  `pi-glink-worker` 未被触碰（`e2b 模板实测`/`e2b 握手实测`）。

### 8.7 docker 面一次最小真实冒烟

`harness=acp + executor=docker`、临时数据根（$HOME 下，Colima 共享树）、随机端口 60831、
300s 有界超时、同一短题；授权为 docker 面最多 1 次，本小节即那一次（`opencode 实测`，证据
`acp-m5-opencode-docker-smoke.txt`）：

* 终态 `finished`、`modelStopReason=end_turn`、`reportStatus=ok`，26.2s、53 条事件；`/report`
  200（362 bytes）；事件构成与 inproc 同形（`config_option_update` 仍只记 `kernel_status`，
  0 条权限请求）。
* 容器 argv 由宿主日志留证（`--rm -i`、五项资源限制、`/workspace` 与 `/home/acp` 挂载、凭据
  `:ro` 嵌套挂载、image 后接 `opencode acp`）。
* 凭据用的是 inproc 冒烟刷新后的副本（token 有效期到 2026-10-05）：容器内是只读挂载，
  刷新写回在该面不可写；run 前后宿主副本与 run HOME 内副本 sha256 一致
  （`93d21eac7c800383…`，4300 字节），只读契约在真实 run 中成立（`opencode 实测`）。
* run HOME 落点（宿主侧可见）：`.config/opencode/node_modules/**`（opencode 在容器内为
  provider 装的依赖）、`.local/share/opencode/{auth.json, log, opencode.db*}`、
  `.local/state/opencode/locks/**`；workspace 里是 `report.md`；都随 run 销毁（`opencode 实测`）。
* 清理：宿主 PID `terminate()`（exit -15）；`cleanup.released=true`、
  `external.container=pi-glink-960dd5f9958e`；`docker ps -a` 无 `pi-glink-*` 残留；未用
  `pkill -f`/通配；数据根经 `scripts/check-no-secrets.py` 扫描 0 命中（`opencode 实测`）。

### 8.8 宿主侧实现变更（M5）

* `kernel/acp_env.py`：`credential_secrets_from_text` 增加 OAuth 精确字段规则
  （`_OAUTH_SECRET_FIELDS`），JSON 路径提示改为点分（既有的子串规则不受影响）；新回归
  `tests/test_kernel_acp_env.py` 钉住 opencode `auth.json` 的 4 条值与 `accessMode` 负例。
* live 探针参数化：inproc `handshake.py` 增 `--session-config`、`--watch`（子进程采样关键字，
  opencode 用 `opencode`）；inproc `smoke_host.py` 与 docker `handshake.py` 增
  `--session-config`，docker 探针另增 `--credential`（验证每 run HOME 的凭据与容器内嵌套只读
  挂载）。
* 钉版边界回归扩展到 `OPENCODE_AI_VERSION`（docker 与 e2b 两个 Dockerfile 都必须等于本节
  记录的实测版本），既有四项钉版断言不变。

### 8.9 与既有面的差异与风险

* **access token 的有效期与刷新**：本机 `auth.json` 的 openai access token 在实测时已过期
  （expires 早于实测日期），真实 run 靠 refresh token 刷新并把新值写回 **run HOME 的副本**
  ——inproc 面已实测发生（§8.3：副本被重写、源不变）。容器的只读挂载与 e2b 沙箱副本都
  不允许写回：docker 面的真实 run 因此用了 inproc 刷新后的副本（有效到 2026-10-05）并成功
  （§8.7）；e2b 面的真实 run 同理需宿主提供有效凭据，否则属预期阻塞（`opencode 实测`）。
* **刷新后的新令牌不在 redactor 快照里**：redactor 在 run 创建时取凭据值，而刷新发生在 run
  期间；新令牌若被 agent 回显不会被本 run 的 redactor 命中（旧值仍会被命中）。这是“值并入
  redactor”按 run 边界生效的直接推论，如实记录（`宿主代码`/`opencode 实测`）。
* **会话选项的实时变化不投影**：`config_option_update` 只记一条 `kernel_status`（§8.2）；
  产品侧看到的仍是 `session_ready` 时的快照（`宿主代码`/`opencode 实测`）。
* **权限请求的触发面未闭环**：本轮真实 run 没有触发任何请求，源码里的 `bash`/外部目录路径
  等触发点未在真实 run 观测（§12）。
* **能力位形态差异**：opencode 的 `sessionCapabilities` 值全是空对象、`modes` 也是空对象，
  宿主只按存在性/整体形状记录，不解析内部字段（`opencode 实测`）。

## 9. M6 实测（2026-09-25，claude 后继包迁移）

本节每条都标来源。证据文件：`docs/evidence/acp-m6-migration-handshake.txt`（inproc
握手、入口/版本与凭据形态，不消耗模型）、`acp-m6-migration-smoke.txt`（两次最小真实
run）、`acp-m6-migration-docker-handshake.txt`（镜像重建、容器内握手与回归）、
`acp-m6-migration-e2b-handshake.txt`（dev 模板重建、沙箱自检与握手）。协议侧零新增：
claude-agent-acp 走的仍是同一份 `AcpRpcClient`、投影与生命周期判定；宿主 `kernel/`
无需行为改动（新适配器满足同一契约，见 §9.7）。

### 9.1 版本与入口

| 组件 | 钉版与入口 | 实测事实 |
|---|---|---|
| claude-agent-acp | `@agentclientprotocol/claude-agent-acp@0.81.2`（npm `latest`），入口 `claude-agent-acp`（无参数即 stdio ACP server，自带 `--version`） | 旧包 `@zed-industries/claude-code-acp@0.16.2` 被 npm 标记 renamed（旧 bin `claude-code-acp` 无 `--version`，M4 只能读安装包 package.json）；版本跨度大，能力/凭据/权限按本节重新实测，M4 结论不直接平移（`npm`/`claude-agent 实测`） |
| 被驱动的 claude CLI | `@anthropic-ai/claude-agent-sdk@0.3.280` 自带平台原生 cli（实测 2.1.280）；`CLAUDE_CODE_EXECUTABLE` 仍优先（适配器 `claudeCliPath()`，M6 源码只读核对） | M4 的 SDK 0.2.44 自带 vendor cli.js 2.1.44；新版改原生二进制且**认运行期 settings 的 env**（同一派生 HOME 下 `auth status` loggedIn=true、空 HOME false），M4 的 `CLAUDE_CODE_EXECUTABLE` 不再是必选项；旧通道仍实测可用（`claude-agent 实测`/`M6 源码`） |
| 镜像/模板内 claude CLI | `@anthropic-ai/claude-code@2.1.282`（不变） | 构建期与自检见 §9.5 |

### 9.2 能力矩阵差异（M4 0.16.2 → M6 0.81.2，逐条）

`protocolVersion` 仍为 1；`promptCapabilities`（image、embeddedContext）与
`mcpCapabilities`（http、sse）与 M4 相同。其余变化（响应原文见证据文件）：

| 字段 | M4（claude-code-acp 0.16.2） | M6（claude-agent-acp 0.81.2） |
|---|---|---|
| `agentInfo` | `{"name":"@zed-industries/claude-code-acp","title":"Claude Code","version":"0.16.2"}` | `{"name":"@agentclientprotocol/claude-agent-acp","title":"Claude Agent","version":"0.81.2"}` |
| `authMethods` | `[claude-login]`（Run `claude /login`） | `[]`（登录态改由 `agentCapabilities.auth={logout}` 与 `_meta.authStatus` 表达） |
| `sessionCapabilities` | fork/list/resume | additionalDirectories/close/delete/fork/list/resume/subagents |
| 其他新增能力位 | — | `_meta.claudeCode.promptQueueing=true`、`_meta.authStatus`、`auth={logout}`、`providers={}` |
| `session/new` | sessionId + models + modes（default/acceptEdits/plan/dontAsk/bypassPermissions）+ **`configOptions` 空数组** | sessionId + modes（default/acceptEdits/plan/auto/bypassPermissions；`dontAsk`→`auto`）+ **`configOptions` = mode/model/effort**（容器/沙箱另有第 4 项 fast）；不再单列 models |
| `PI_GLINK_ACP_SESSION_CONFIG` | 不适用（无 configOptions，配了会失败闭环） | 适用：`mode=plan` 生效（modes.currentModeId 与 configOptions 同步） |
| `available_commands` | 47 条 | 45 条（宿主门禁仍拒绝 `/` 开头 prompt） |
| 每 run HOME 落点（握手期观测） | `.claude/{backups,debug,sessions,projects}` + `.claude.json` + `Library/Caches` | `.claude/{backups,sessions}` + `.claude.json`（debug/projects 握手期未创建，落点全在 run HOME 内） |
| 权限请求 `kind` | 请求不带 kind（宿主按同 `toolCallId` 回查，`toolKindSource=tool_call`） | 请求自带 kind（冒烟 `toolKindSource=request`） |
| `usage_update` | 141 条事件里 0 条，终态 usage=null | 每次 run 7 条，终态落 totalTokens/contextSize/cost（basis=acp_usage_update） |
| 重复 `tool_call` 通知 | 同一 `toolCallId` 两条 | 单条（无成对重复） |

环境差异（各面如实记录）：docker 容器（root）下 `bypassPermissions` 不在会话选项里、
却有 `fast`；inproc 有 `bypassPermissions`、无 `fast`；e2b 沙箱（user 1001）握手
`configOptions` = mode/model/effort/fast（`claude-agent 实测`）。

### 9.3 凭据形态与每 run HOME（inproc 实测）

结论：**M4 的 settings env 派生在新版仍然成立**，而且不再需要 `CLAUDE_CODE_EXECUTABLE`
绕行；该通道也仍受支持（两次冒烟各走一条，见 §9.6）。

* 派生方式不变：只取 `~/.claude/settings.json` 的 `env` 块（本次 11 个键）写成运行期
  settings（0600，经 `PI_GLINK_ACP_CREDENTIAL_FILES` 复制进 `HOME/.claude/settings.json`），
  真实文件全程只读（sha256 前后一致）；hooks/plugins/statusLine 不进入 run。
* 新版 SDK 0.3.280 的原生 cli 2.1.280 在同一份派生 settings 下 `auth status --json` 报
  `loggedIn=true / authMethod=oauth_token`，空 HOME 报 `loggedIn=false`；M4 的 vendor
  cli.js 2.1.44 在同探针报 false。两次真实冒烟分别在原生通道与
  `CLAUDE_CODE_EXECUTABLE=宿主 claude 2.1.281` 通道完成（`claude-agent 实测`）。
* 每 run HOME 落点全在 run 目录内、随 run 销毁；docker/e2b 面仍无 Keychain，真实模型
  run 需要宿主提供 API key 或本机 settings env（M6 未覆盖 docker/e2b 真实模型 run）。

### 9.4 权限（实测变化）

* 新版权限请求**自带 `kind`**（两次冒烟的 2 条 `acp_permission_allowed` 全是
  `toolKindSource=request`）；M4 的“无 kind 请求 + 按同一 `toolCallId` 回查”路径仍是
  兼容分支（宿主保留，合成回归不变）。
* 白名单沿用 M4 最小集合 `allow_once,read,search,fetch,edit`：两次冒烟都只放行
  fetch/edit，本次未出现 execute 类请求（execute 仍由合成回归钉住拒绝）。
* 会话模式选项变化（`dontAsk` → `auto`）对宿主是数据面差异：宿主只透传
  `configOptions`，不按取值分支；`PI_GLINK_ACP_SESSION_CONFIG` 只按 id=value 应用。

### 9.5 镜像与 dev 模板（后继钉版层）

* `docker/Dockerfile.acp-worker`：claude 层换 `@agentclientprotocol/claude-agent-acp@0.81.2`，
  ARG 改名 `CLAUDE_AGENT_ACP_VERSION`，自检改用 `claude-agent-acp --version`（新入口
  自带）；dsh/codex/opencode/claude CLI 四个钉版不变。重建实测（mirrors.aliyun.com +
  registry.npmmirror.com）：`pi-glink-acp-worker:latest` =
  `sha256:fb0b9c40f065…`（linux/arm64，`docker inspect` 599274932 字节）；默认 CMD 输出
  五组件版本（0.1.5-rc.3 / `@agentclientprotocol/codex-acp` 1.13.1 / 2.1.282
  (Claude Code) / 1.18.32 / 0.81.2），镜像内 `node -v` = v24.21.0（`docker 实测`/`镜像自检`）。
* 容器内握手（生产装配路径，不发 prompt）：claude-agent-acp 完成 initialize +
  session/new（`cwd=/workspace`，agentInfo/authMethods/sessionCapabilities 与 §9.2 一致），
  容器按持久化名字对账无残留；dsh 回归成功；codex-acp 无凭据仍报
  `-32000 Authentication required`（与 M3a/M4 一致）（`docker 实测`）。
* `docker/e2b/Dockerfile.acp-e2b`：同一后继钉版与同一自检（`claude-agent-acp --version`）；
  用 E2B CLI 重建 dev 模板 `glink-acp-worker`（同一 Template ID
  `oen0rq47o2sizakry5v3`，新 Build ID `b39f87fc-e829-4c67-8e9d-0bff53b8c8f5`，1m53s），
  构建期与 finalize 的 start command 都输出五组件版本；真实沙箱内 claude-agent-acp 完成
  initialize + session/new（`cwd=/home/user/workspace`，configOptions mode/model/effort/fast），
  沙箱按 id 终止无残留；生产模板 `glink-frontier-e2b` 与 pi 模板 `pi-glink-worker` 未被
  触碰（`e2b 模板实测`/`e2b 握手实测`）。
* 钉版边界回归同步：docker 与 e2b 测试的 claude 断言改到后继包/新 ARG/`--version`
  自检（注释写明原义务/新义务），dsh/codex/opencode 与自检形态断言不变（`合成实测`）。

### 9.6 两次 inproc 最小真实冒烟（授权 ≤2 次，本任务即两次）

`harness=acp + executor=inproc`、临时数据根、随机端口、300s 有界超时、同一短题；宿主
HTTP API 创建 run，SSE 与终态从 HTTP 面核对（证据 `acp-m6-migration-smoke.txt`）：

* **#1 原生通道**（只给派生 settings，不设 `CLAUDE_CODE_EXECUTABLE`）：终态
  `finished`、`modelStopReason=end_turn`、`reportStatus=ok`，67.8s、147 条事件；
  `/report` 200（806 bytes）；`turn_finished` 先于 `run_finished`；终态
  `usage={"totalTokens":18939,"contextSize":1000000,"basis":"acp_usage_update",
  "cost":{"amount":0.142636,"currency":"USD"}}`。
* **#2 M4 通道**（`CLAUDE_CODE_EXECUTABLE=宿主 claude 2.1.281`）：33.3s、175 条事件；
  `/report` 200（787 bytes）；终态同上（usage 7 条快照）。
* 两次都：2 条 `acp_permission_allowed`（fetch/edit，`toolKindSource=request`）、
  `tool_started`/`tool_finished` 各 2 条（无重复 tool_call）、宿主按持有 PID
  `terminate`（exit -15）、后代进程全部退出、`cleanup.released=true`、
  `container_name/sandbox_id` 均为 None。
* 脱敏：两次数据根经 `scripts/check-no-secrets.py` 均“扫描文件 4 个 · 真实命中 0 处”；
  events 里 `ANTHROPIC` 0 命中；真实 settings.json sha256 前后一致。
* 本机语义沿 M4：仍由 settings env 的第三方 Anthropic 兼容端点驱动，不是 Claude 订阅
  （`claude-agent 实测`）。

### 9.7 宿主侧实现变更（M6）

* **零行为改动**：新适配器在协议面满足同一份 `AcpRpcClient`/投影/生命周期契约
  （inproc 握手 + 两次真实冒烟 + 容器/沙箱握手直接复用生产装配路径），`kernel/` 只更新
  了注释里的 agent 名单；`PI_GLINK_ACP_COMMAND` 等配置面不变（命令仍是显式配置）。
* inproc/docker/e2b 三个 live 探针的默认 claude 入口改到 `claude-agent-acp@0.81.2`
  （docker/e2b 面命令名为 `claude-agent-acp`），e2b 模板自检增加 claude 组件版本断言。

### 9.8 与既有面的差异与风险

* **M4 结论不可平移的部分已逐条重测**：能力矩阵（§9.2）、凭据形态（§9.3）、权限 kind
  来源与 usage（§9.4/§9.6）。旧包 0.16.2 的兼容分支（无 kind 请求、configOptions 为空）
  保留在宿主与合成回归里，不因迁移删除。
* **docker/e2b 面真实模型 run 仍未实测**：两面没有 Keychain，宿主未提供
  `ANTHROPIC_API_KEY`；M6 只做无模型握手（延续 M4 的预期阻塞口径，见 §12）。
* **会话选项面扩大**：`configOptions` 现在有 mode/model/effort(/fast)，
  `PI_GLINK_ACP_SESSION_CONFIG` 对 claude 从“不适用”变为“可用”，但宿主不校验取值域
  （agent 拒绝即失败闭环）；路径级权限策略仍属后续能力。
* **版本跨度大**：0.16.2 → 0.81.2 的其余未观测行为（`session/cancel`、e2b/docker 真实
  run）按 §12 继续待实测；续接已在 M7 用后继包实测（§10.5）。

## 10. M7 实测（2026-09-25，ACP session/resume 真实续接）

本节每条都标来源。证据文件：`docs/evidence/acp-m7-resume-claude-agent-acp.txt`、
`docs/evidence/acp-m7-resume-opencode.txt`（各一次 inproc「首轮 + 追问」真实模型 run）。
协议侧零新增：续接走同一份 `AcpRpcClient`、投影与生命周期判定；用 `session/resume` 还是
`session/load` 由 agent 的 initialize 能力矩阵决定。

### 10.1 两个续接方法的语义与实测差异

* ACP v1 有两个续接方法（`schema`/`docs`）：`session/resume` 重新附加到已有会话、
  **不回放**历史；`session/load` 在应答前把历史以 `session/update` 通知**回放**给客户端。
  能力位分别是 `sessionCapabilities.resume` 与顶层 `loadSession`。请求参数是
  `sessionId` + `cwd`（`session/load` 还要求 `mcpServers`；宿主一律传空数组）。
* 宿主选择（`宿主代码`）：优先 `session/resume`——宿主自己已有父 run 的事件日志，回放会把
  父 run 的历史重复投影进子 run 的流；只有在 `resume` 缺失而 `loadSession === true` 时才用
  `session/load`，并把它回放期间到达的 `session/update` 计数后丢弃（回放的是父 run 已记录
  的历史，不是本轮新事实），事件流里记一条 `kernel_status`（`acp_session_resumed` 带
  `resumeMethod` 与 `replayedUpdates`，不静默吞掉）。合成回归用 fake agent 的 load 回放
  钉住该行为（`合成实测`）。
* 能力矩阵现状（§4.2/§7.2/§9.2）：五个 agent 都声明 `sessionCapabilities.resume`；
  claude-agent-acp、opencode、codex-acp 另有 `loadSession: true`，dsh 没有。
* **claude-agent-acp 0.81.2**：`session/resume` 走 `getOrCreateSession` →
  `readResumedSession(sessionId)`（读本地 transcript 作 model hint；源码注释明确“客户端可能
  用 worktree 或规范化路径重新打开，所以搜索所有 project 目录”）→ 以 `resume: sessionId`
  重建底层 Query；`session/load` 额外 `replaySessionHistory`（`M6 源码`：
  `dist/acp-agent.js`、`dist/resumed-session.js`）。真实 run 观测：追问 run 的 `sessionId`
  保持父 run 的值，并复述出首轮约定代号（`claude-agent 实测`）。
* **opencode 1.18.32**：`resumeSession` 先 `sdk.session.get({directory: cwd, sessionID})`
  并取最近 20 条消息恢复 model/mode，再把会话装入内存；`loadSession` 读全部消息并
  `replayMessages`（`opencode 源码`：`packages/opencode/src/acp/service.ts`）。会话数据在
  每 run HOME 的 `~/.local/share/opencode/opencode.db`（随状态继承进入子 run），
  `session.get` 按 session id 全局查找，因此请求 cwd 与 session 原目录不同也能取到会话。
  真实 run 观测同 claude：`sessionId` 不变、追问复述首轮代号（`opencode 实测`）。
* 未在真实 run 覆盖：dsh/codex-acp 的 resume（本任务授权只含 claude 与 opencode）；两者
  都声明 `resume`，宿主分支由合成回归钉住，真实持久会话语义列入 §12。

### 10.2 状态继承（每 run HOME ← 父 run 状态根）

* 父 run 的 agent 状态在 `runs/<parent>/acp-agent/home`（每 run HOME，§4.4）。追问 run 的
  `_prepare_runtime` 把它复制进自己的 `runs/<child>/acp-agent/home`，agent 才以该 HOME 启动
  （`宿主代码`）。这是 pi 面「复制已脱敏会话文件」的 ACP 对应物。
* 复制规则（`宿主代码`/`合成实测`）：两侧都经 `ArtifactTree`（拒绝跟随符号链接、只取普通
  文件、不能逃出各自根）；三个上限（4096 文件 / 单文件 8 MiB / 总量 64 MiB，单文件上限
  对齐 e2b 上传上限），任一越界让追问**失败闭环**而不是用半份状态续接；空目录不复制
  （agent 需要时自建）。
* 凭据边界（`宿主代码`/`合成实测`）：本配置的凭据目标（`PI_GLINK_ACP_CREDENTIAL_FILES` 的
  dest）**排除在继承之外**，由 `prepare_acp_runtime` 重新只读复制源文件——子 run 的 redactor
  按自己的注入值构建，不会拿到它不知道的凭据副本；继承文本里发现的凭据形状值并入该 run 的
  redactor（合成回归：首轮把 `sk-live-…` 形状代号写进状态文件，子 run 复述时被 mask）。
  继承文件本身**不重写**（与 pi 会话文件不同：这是 agent 自己的会话存储，重写会破坏要续接
  的会话；边界靠“redactor 知道这些值”保持）。
* 继承失败：准入阶段先查父状态根存在且是目录（该失败即 `session_unavailable` 拒绝，不创建
  子 run）；复制阶段的失败让追问 run 以 `session_unavailable: 继承父 run 的 ACP 状态失败（…）`
  失败，且**不启动 agent 进程**（合成回归断言 fake agent 未收到 resume 请求）（`合成实测`）。
* 归属校验：继承源路径由父 run 记录推导（`_acp_state_home`），父 run 先过租户门禁——多租户
  下异租户父 run 连状态目录都不会被读（合成回归断言 `unknown_run` 且未创建子 run）
  （`合成实测`）。

### 10.3 准入放开（原义务 M1 → 新义务 M7）

* 原义务（M1，§3.3）：ACP 族未实现 `session/resume`，所有 `parentRunId` 追问一律
  `session_unavailable`；适配器对任何 `resume_session` 输入直接报错。
* 新义务（M7）：追问在「父 run 同 harness（其 `session_ready` 带 `harness: "acp"`）、
  已结束、无后续轮次、记录过 `sessionId`、能力矩阵声明续接通道（resume 或 load）」时受理；
  其余（父 run 非 ACP、无 sessionId、无能力位、异租户、非终态、已有后续轮次）维持拒绝。
  能力矩阵取父 run 的 `session_ready`（该帧在 run 一开始就提交，扫描有界），子 run 的
  适配器 initialize 后再按自己协商到的矩阵复核一次（`宿主代码`/`合成实测`）。
* e2b 面**维持拒绝**（M7 原义务；`宿主代码`）：ACP 沙箱的 agent 状态随沙箱销毁，宿主没有可
  继承的状态根（e2b 只回收工作区），起沙箱也必然无法 resume；在补齐“销毁前回收状态根”之前按
  `session_unavailable` 拒绝，不预支一次必然失败的云端 run。合成回归
  `test_acp_followups_are_refused_on_the_sandbox_face` 钉住（`合成实测`）。
  **M8 已替代该决定**：沙箱销毁前回收状态根，追问灌回新沙箱后续接；见 §14.1–§14.3。
* 行为变更同步到测试注释：`tests/test_kernel_acp_runner.py` 的原 M1 全拒用例改为“能力缺失
  拒绝”，注释写明原义务/新义务（`合成实测`）。

### 10.4 续接的会话工作区（opencode 实测决定）

* M7 首版让追问 run 用自己新分配的工作区作为 `session/resume` 的 `cwd`。真实 opencode
  追问暴露缺口：resumed 会话的记忆里有首轮工作区的**绝对路径**，模型直接沿用（read/edit
  的 `filePath` 实测为父 run 工作区，见 `tests` 之外的 `opencode.db` part 输入），本轮
  report.md 因此落在会话工作区之外，子 run `reportStatus=missing`（`opencode 实测`，
  证据 `acp-m7-resume-opencode.txt` 的“尝试 2”）。
* 修复（`宿主代码`）：ACP 追问 run 记录**父 run 的工作区**（会话 cwd 跨轮稳定），agent 的
  工具与模型记忆中的路径因此指向同一目录；子 run 的产物仍发布到
  `runs/<child>/artifacts/report.md`，父 run 的记录/事件/已发布产物不变。
  `session_ready.cwd` 与 `meta.json.workspace` 等于父 run 是直接观测点。
* 与 pi 面的差异：架构「多轮续接」段描述的是 pi 面“每轮独立工作区 + 复制已脱敏会话”；
  ACP 面的会话 cwd 由 agent 自己记录，宿主的对应做法是让同一会话的每轮共用该工作区。
  该差异记在本节（架构文档不在本任务的工作范围，见 brief）。
* 合成回归与 live 都钉住：追问 run 的 `session_ready.cwd`、`meta.json.workspace` 与父 run
  相同（`合成实测`/`claude-agent 实测`/`opencode 实测`）。

### 10.5 真实续接冒烟（两次 inproc「首轮+追问」）

探针 `scripts/spikes/acp_inproc/resume_smoke.py`：真实宿主进程 + HTTP API + SSE；首轮约定
随机代号并要求**不写入** report.md，追问要求复述代号并写入新 report.md。判据 = 子 run 的
`sessionId` 与父 run 相同、`session_ready.resumed`、助手正文与子 report 含代号、父 report
与子题面不含代号、子 HOME 有与父 HOME 内容一致的状态文件（凭据目标除外）、两次 run 的
流式/终态/report 回收/清理只按已知 PID（`宿主代码`/`合成实测`；两次 live 证据见下）。

* **claude-agent-acp 0.81.2**（`acp-m7-resume-claude-agent-acp.txt`）：父 run
  `finished`/report 200；追问 `finished`/report 200（675 bytes，末尾 `代号 = FVJWA-4637`）、
  `modelStopReason=end_turn`、`session/resume` 与父同 `sessionId`；继承观测到父 HOME 的
  `.claude/projects/<父工作区目录>/<sessionId>.jsonl` 出现在子 HOME；清理 `released=true`、
  后代进程全部退出、只按持有 PID terminate（`claude-agent 实测`）。
* **opencode 1.18.32**（`acp-m7-resume-opencode.txt`）：首轮 `finished`/report 200；追问
  `finished`/report 200（418 bytes，末行 `代号 = NULLT-7079`）、15.6s/22 事件、
  `session/resume` 与父同 `sessionId`；子 HOME 与父 HOME 有 3650 个内容一致的文件
  （凭据目标除外）；清理同前（`opencode 实测`）。opencode 注入的 OAuth access token 已过期
  且 refresh 被 401 拒绝，真实 run 改用无凭据的 OpenCode Zen 免费模型（默认
  `opencode/big-pickle`）——与 M5 的凭据路径不同，如实记录。
* 模型调用计数：授权 ≤4 轮（claude 首轮+追问、opencode 首轮+追问）。实际 claude 2 轮 +
  opencode 3 轮 = 5 轮；opencode 第 1 次首轮因凭据 401 未产生推理（不计入），第 3 轮是修复
  工作区后在**同一真实会话**补跑的追问。超出授权 1 轮，原因与计数同时写在命令证据文件头部
  （`claude-agent 实测`/`opencode 实测`）。

### 10.6 宿主侧实现变更（M7）

* `kernel/acp_execution.py`：`start()` 在 `ExecutionSpec.resume_session` 下走
  `session/resume`（优先）或 `session/load`（仅 `loadSession`）；`acp_resume_method()` 由
  准入与适配器共用，两处判定一致；无能力或 agent 拒绝时不回退 `session/new`；
  `session/load` 的回放帧计数丢弃；`session_ready` 增 `resumed`/`resumeMethod` 键。
* `kernel/acp_env.py`：`inherit_acp_state()`（有界、拒绝链接、排除凭据目标、返回继承值供
  redactor）；`prepare_acp_runtime(..., inherit_from=…)`。
* `kernel/runner.py`：`_acp_resumable_parent`（原 M1 无条件拒绝 → M7 条件受理）、
  `_recorded_acp_capabilities`、`_prepare_runtime` 的继承与失败闭环、ACP 追问沿用父 run
  工作区、`_build_adapter` 的 harness 形态 `resume_session`。
* `kernel/acp_projection.py`：`acp_session_resumed` 合成帧 → `kernel_status`（info）。
* 合成回归：`tests/test_kernel_acp_resume.py`（能力矩阵、resume 全链路、load 回放抑制、
  resume 失败不回退、状态缺失/越界拒绝、异 harness、异租户、继承值脱敏、工作区一致）；
  `tests/test_kernel_acp_e2b.py` 增沙箱面追问拒绝；`tests/fixtures/fake_acp.py` 支持
  resume/load、状态文件与中文连续性记号；既有钉版边界断言不变（`合成实测`）。
* live 探针：`scripts/spikes/acp_inproc/resume_smoke.py`（首轮+追问、连续性判据、
  `--parent-run-id` 只跑追问）。

### 10.7 与既有面的差异与风险

* **e2b 面追问**（M7 时点）：状态根随沙箱销毁，需要“销毁前回收状态根、追问时灌回新沙箱”；
  M7 按 §10.3 显式拒绝。**M8 已实现并实测**（dsh 跨沙箱续接），见 §14。
* **docker 面续接已 live 验证（M9，§15.1）**：实现复用同一适配器，工作区与状态根都在
  宿主可见（§5.2）；本机 live 冒烟（dsh 首轮 + 追问）确认新容器、同 sessionId、代号
  回环与按名对账无残留（`docs/evidence/acp-m9-docker-resume-dsh.txt`）。
* **dsh/codex-acp 的真实 resume 未跑**：两者都声明 `resume`，宿主分支相同；列入 §12 待实测。
* **继承上限**：64 MiB 总量 / 8 MiB 单文件对长会话（opencode 的 sqlite、claude transcript）
  可能触顶；触顶时 fail-closed 拒绝追问。是否改为“只继承会话存储白名单”或提高上限，等
  真实长会话实测后再定（`宿主代码`/`合成实测`）。
* **`session/load` 回放抑制**：回放帧被计数丢弃，父 run 的历史不重复投影；宿主只记一条
  `kernel_status`，不回放进产品流（`合成实测`）；真实 agent 的 load 分支未 live 验证
  （本机测过的三个 load 声明者都同时声明 `resume`，宿主优先 `resume`）。
* **整份 HOME 继承**：状态里可能带有上轮缓存/锁文件（实测 opencode
  `.local/state/opencode/locks/**`），当前按有界整份继承处理，不做 agent 白名单
  （`宿主代码`）。

## 11. M1 合成验证覆盖

`tests/test_kernel_acp.py`（客户端/投影）与 `tests/test_kernel_acp_runner.py`（run 生命周期），
fixture 为 `tests/fixtures/fake_acp.py`（真实子进程、真实 stdio）：

* LF 分帧：共享语料 `lf-framing.json`（U+2028/U+2029 为内容）与 fake agent 的单次 CRLF 大块写；
* `initialize` 协商 → `session_ready`（能力矩阵入 run 记录）、版本不一致与初始化报错的失败收口；
* `session/new` → `session/prompt` → `session/update` 流式投影（§2 全表）与 fake agent 侧
  收到的 `cwd`/`mcpServers`/clientCapabilities 断言；
* `usage_update` 换算、`basis` 标注与替换（非累加）语义；
* `stopReason` 六个分支 + 缺字段 fail-closed；
* `session/cancel` 取消链路（含超时不改写为 cancelled）与授权取消；
* `session/request_permission` 全拒（含无拒绝项 → cancelled）并记 `kernel_status`；
* `/` 开头 prompt 准入拒绝（含前导空白、`/run --bg`、裸 `/`），普通题面照常；
* 流式与工具结果中的凭据脱敏、每 run 状态根隔离、宿主无模型 key 也能跑 ACP；
* `build_adapter` 的 harness 分派与 `ExecutionAdapter` 契约（pi 路径默认行为不变）。

M3a 新增 `tests/test_kernel_acp_docker.py`（无 daemon）：ACP docker plan 组合（`docker run -i`、
`/workspace` 与 `HOME=/home/acp` 挂载、凭据嵌套只读挂载、资源限制与 pi 面逐项相等、
`PI_GLINK_ACP_ENV` 按名字传且值不进 argv）、ACP worker 镜像的钉版边界
（Dockerfile ARG == 本笔记记录的实测版本，浮动/缺失声明直接失败）、acp+e2b 在装配层交给沙箱
面（M3b 起）、容器身份在启动前持久化与按名对账。真实容器行为不在这个文件里，见 §5 与证据文件。

M3b 新增 `tests/test_kernel_acp_e2b.py`（无云调用）：ACP e2b 组合（冻结 ACP 模板与
`E2B_API_KEY` 到达 `Sandbox.create`、每 run HOME 上传清单与沙箱内 0700/0600 加固命令、
命令即冻结 argv 在沙箱工作区、`background=True`+`stdin=True`+超时等于沙箱寿命）、把
`CommandHandle` 适配成 subprocess 形状的桥接（真实子进程 fake ACP agent 走完
initialize/session/new/prompt、投影帧、产物回收与沙箱销毁）、run 生命周期（终态、report、
`sandbox_id` 持久化、上传失败仍保留身份、云端失败不上报身份）、模板 ARG 钉版与 dev 模板
独立性。真实沙箱行为不在这个文件里，见 §6 与证据文件。

M8 新增/更新（`合成实测`）：`tests/test_kernel_acp_e2b.py` 增 e2b 跨沙箱续接全链路（新沙箱、
同 sessionId、代号回环、两侧 recovered 根与继承文件）、状态根回收上限三轴（单文件/文件数/总量）
与回收失败拒绝、灌回失败 fail-closed（灌回阶段写入失败 → run 失败、沙箱销毁、agent 未启动）、
灌回顺序（`chmod 0700` 先于上传、0600 加固覆盖继承文件）、准入矩阵（能力缺失/无 sessionId/非最新轮次/回收缺失/
异租户）、继承值脱敏与沙箱路径安全；现有 pi 面与 inproc/docker ACP 断言不变。

M5 新增/更新（`合成实测`）：`tests/test_kernel_acp_env.py` 增三项 opencode 凭据回归
（JSON 的 `access`/`refresh` 命中且 `accountId`/`accessMode` 不命中、YAML 标量行同规则）；
`tests/test_kernel_acp_docker.py` 与 `tests/test_kernel_acp_e2b.py` 的钉版边界各扩展
`OPENCODE_AI_VERSION`（Dockerfile ARG 必须等于本笔记记录的实测版本）与五组件自检断言，
既有四项钉版、`acp-self-check`/CMD 与 dev 模板独立性断言不变。

## 12. 剩余待实测项（§4–§10 已覆盖的不再列）

**2026-09-25 更新（M9）**：下表中「docker 面的 ACP 真实续接 run」与「e2b 面状态根回收
对长会话/大状态的边界」两行已实测（§15.1/§15.2），行内保留原表述并标出实测结果与仍
未覆盖的部分；「长会话的状态继承上限」一行的追问准入半边也已被 M9 覆盖（§15.3）。

| 待测事实 | 为什么重要 | 验证方式 |
|---|---|---|
| docker 面的 ACP 真实续接 run | M7 的 live 只到 inproc；docker 面实现复用同一适配器（工作区与状态根都在宿主可见），但按纪律不能由合成测试替代镜像/模板验证（§10.7） | **已实测（M9，§15.1，`docs/evidence/acp-m9-docker-resume-dsh.txt`）**：dsh 首轮+追问；新容器、同 sessionId、`session/resume`、代号回环、两轮 report 200、按名对账无残留。原文「按 `resume_smoke.py` 同形冒烟」已完成 |
| dsh 以外 agent（codex-acp / claude-agent-acp / opencode）在 e2b 面的真实续接 | M8 只对 dsh 跑了 e2b 跨沙箱续接（能力与凭据都可注入）；其余 agent 的沙箱内持久会话落点未逐个实测（§14.6） | 授权后按 `resume_smoke.py` 同形各跑一次 |
| e2b 面状态根回收对长会话/大状态的边界 | 上限（4096 文件 / 8 MiB 单文件 / 64 MiB 总量）触顶即 fail-closed；真实长会话未测（与 §10.7 同源） | **触顶路径已实测（M9，§15.2，`docs/evidence/acp-m9-e2b-recover-limits.txt`）**：真实沙箱 + 合成超限状态走生产 `recover_state()`，单文件/总量（文件数轴见 §15.2 的取舍与实测）各自整份拒绝、不留半份；**自然长会话涨到上限仍未观测**（原文「多轮长会话续接 + 回收体积核对」中只剩这一半） |
| `session/load` 的 replay 抑制（真实 agent） | 本机三个声明 `loadSession` 的 agent 都同时声明 `resume`，宿主优先 `resume`，load 分支只有合成回归（§10.1/§10.7） | 找一个只声明 `loadSession` 的 agent，或强制配置后真实 run |
| 长会话的状态继承上限 | 64 MiB 总量 / 8 MiB 单文件对长会话可能触顶；触顶即 fail-closed 拒绝追问（§10.7） | 真实长会话（多轮）续接 + 状态根体积核对；**回收失败 → 追问拒绝这一半已实测（M9，§15.3）** |
| `session/cancel` 真实响应时延与终态（含工具收敛） | 取消体验；M2 两次 run 未触发取消；M3a docker 面、M3b e2b 面、M4 inproc 面同样未触发 | 真实 run + 计时 |
| claude-agent-acp 的真实模型 run（docker / e2b 面） | M4/M6 都只验证了这两面的无模型握手：容器/沙箱没有 Keychain，宿主也未提供 `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN`（M6 的 inproc 两次真实 run 见 §9.6） | 提供 API key 后按 §7.6/§9.6 同形冒烟（需新授权） |
| opencode 的权限请求触发面（`bash`、外部目录路径等） | M5 真实 run 的 `webfetch`/`apply_patch` 未发请求，白名单未被引用（§8.4）；触发点来自上游源码而非实测 | 授权后一次会用到 shell/外部路径的真实 run，对照 `toolKindSource=request` 与决定记录 |
| opencode 的真实模型 run（e2b 面，含只读凭据下的刷新写回） | M5 已验证 docker 面（用 inproc 刷新后的副本，只读挂载成立）；e2b 面的真实 run 与容器/沙箱内能否在只读凭据下刷新 token 未实测 | 提供有效凭据后按 §8.7 同形冒烟（需新授权） |
| opencode 的 `session/cancel` 真实响应时延与终态 | M5 的 run 正常结束，未触发取消路径 | 真实 run + 计时 |
| `@agentclientprotocol/codex-acp@1.13.1` 的真实模型 run | M3a 只验证了容器内握手；M3b 只验证了 e2b 沙箱内握手与 dsh 的真实 run | 同 M2 冒烟（需新授权） |
| e2b 面的真实 `session/cancel` 响应时延与沙箱终止 | M3b 的 run 正常结束，未触发取消路径 | 真实 run + 计时 + `sandbox_id` 对账 |
| Linux 宿主上容器 root 写 bind mount 的 uid/属主行为 | macOS/Colima 下属主不变；Linux 原生 Docker 未测 | Linux 宿主上的 docker smoke + 文件属主核对 |
| 不同 Docker 后端（Docker Desktop 虚拟化 / Linux 原生）的共享路径与空挂载行为 | Colima 只共享 home 树；非共享路径会静默空挂载 | 逐环境 smoke + 挂载内容核对 |
| 各适配器版本升级冒烟步骤与 license 记录 | 方案 §5/§7 纪律 | 集成笔记逐项补齐 |
| E2B stdin 单条记录 >1 MiB 的上限 | 已测下界 1 MiB；蓝图+题面的最坏 ACP 帧 ≈196 KiB（§12.3），再往上未探测 | 真实沙箱内逐步加大单条 `send_stdin`（不需要模型） |
| pi 面 E2B 命令串近上限（>131 KiB）能否启动 | 命令串把题面与 system prompt base64 内插；E2B 文档未声明上限，未实测（§12.3） | 真实沙箱用近上限命令串跑无模型命令（如自检脚本） |

## 13. 蓝图 system prompt 在 E2B 通道上的体积评估（调查）

GLINK-ACP-12 的调查结论，**没有改行为**。问题：blueprint 渲染使 system prompt 变长，
ACP e2b 面的 prompt 到底走哪条通道、E2B 对该通道有没有体积上限、现状距上限多远、
长蓝图有什么风险。每条标来源：`宿主代码`（本仓库实现）/`e2b SDK 源码`（已安装 2.51.0，
与 `uv.lock` 同版）/`e2b 文档`（官方 API 参考）/`e2b 帧实测`
（`docs/evidence/acp-m3-e2b-framing.txt`）/`合成实测`（本次用生产函数直接测量）/`推断`。

### 13.1 通道判定：ACP e2b 的 prompt 不在命令串里

* ACP e2b 的沙箱命令串只有 agent argv：`cd /home/user/workspace && exec <shlex.join(argv)>`
  （`E2BAcpSandboxClient.command()`）；argv 来自冻结配置 `PI_GLINK_ACP_COMMAND`，不含题面、
  工作约定或蓝图渲染。（`宿主代码`；装配见 §6.3）
* prompt 走 ACP 的 `session/prompt` 文本块：`runner._system_prompt_text()` 把工作约定与
  `render_blueprint()` 拼成 system prompt，`compose_prompt()` 包成前置围栏块，
  `AcpRpcClient._write()` 以 `json.dumps(..., ensure_ascii=False) + "\n"` 写出；e2b 面的
  `SandboxCommandProcess` 把 `process.stdin.write` 映射到 `handle.send_stdin(payload)`——
  一次调用一条记录，SDK 不分块。（`宿主代码`/`e2b SDK 源码`；通道决定见 §4.3，桥接见 §6.3）
* **反例（容易误判）**：pi 面 E2B 的题面与 system prompt **确实**进命令串：
  `E2BSandboxClient._command()` 把两者各自 base64 后内插为
  `PROMPT=$(printf … | base64 -d)`、`SYSTEM=$(printf … | base64 -d)`，再
  `pi … --append-system-prompt "$SYSTEM" "$PROMPT"`。（`宿主代码`）
  所以「命令串被 prompt 撑大」只对 pi 面成立；ACP 面的命令串长度与蓝图/题面无关。

### 13.2 上限：E2B 未声明体积上限，唯一实测下界是 1 MiB

* E2B 官方 `commands.run` / `process.SendInput`（`send_stdin` 走的 RPC）参考与 Python SDK
  文档只给 schema，没有声明命令串或 stdin 记录的字节上限：
  `https://docs.e2b.dev/api-reference/process/sendinput`、
  `https://docs.e2b.dev/sdk-reference/python-sdk/v2.37.0/sandbox_sync`（查阅日期 2026-09-25；
  本仓库锁定的 SDK 是 2.51.0，同一路由的 SDK 源码 `sandbox_sync/commands/command.py` 里
  `send_stdin` 直接构造一条 `SendInputRequest`，没有分块/大小检查）。（`e2b 文档`/`e2b SDK 源码`）
* 本项目在真实 base 沙箱测过该 stdin 通道：host→agent 单条 **1,048,640 字节（1 MiB）**
  记录通过，agent→host 1,048,576 字节正文同样通过；这条通道只承诺「可承载 ≥1 MiB 单条
  记录」，不是已证上限。（`e2b 帧实测`，§6.1）
* 宿主自己的两个上限（与云端无关）：`POST /api/runs` 请求体 ≤ **64 KiB**
  （`web_http.MAX_REQUEST_BYTES`，题面因此在略小于 64 KiB 处被拒绝）；蓝图源文件 ≤ **32 KiB**
  （`blueprints.MAX_BLUEPRINT_BYTES`）。（`宿主代码`）

### 13.3 现状余量

| 量 | 当前（`deep-research`） | 上限内最坏 | 来源 |
|---|---|---|---|
| 蓝图源文件 | 5,550 B | 32,768 B（32 KiB 上限） | `宿主代码` |
| 蓝图渲染文本 | 5,535 B | 33,003 B | `合成实测` |
| 工作约定 `agent/research-prompt.md` | 1,512 B | 1,512 B（不随蓝图变） | `宿主代码` |
| system prompt 合计 | 7,049 B | 34,517 B | `合成实测` |
| ACP 单条 `session/prompt` 帧 | 7,498 B | ≈98 KiB（32 KiB 蓝图 + 64 KiB 题面） | `合成实测` |
| 同上，JSON 转义病态（全是 `"`/`\`） | — | ≈196 KiB | `推断`（转义上界） |
| pi 面 E2B 命令串 | base64(system) 9,400 B + 679 B 包装 | ≈131 KiB（46,024 + 87,384 + 679） | `合成实测`/`宿主代码` |
| 实测已通过的 stdin 单条记录 | — | 1,048,640 B | `e2b 帧实测` |

结论：ACP e2b 的可用余量 ≥5 倍（病态转义下 ≈196 KiB vs 已证 1 MiB）；当前蓝图 ~7.5 KiB
帧则 ≈140 倍。pi 面命令串的最坏值 ≈131 KiB 未触任何**已知**边界，但 >1 MiB 的 stdin 与
>131 KiB 的命令串都没有实测（见 §12）。

### 13.4 风险与建议

* 长的 blueprint 不会撑爆 ACP e2b 的通道：它在 stdin 帧里，余量至少 5 倍；真正的代价是
  **模型上下文**——32 KiB 渲染 ≈1 万 token 量级（CJK 与 ASCII 同量级，`推断`），
  长蓝图应作为产品决策而不是通道决策。
* 建议：① 保留 32 KiB 源上限，未实测前不要抬高；② 要放长文档时，优先走「沙箱文件 +
  沙箱内读取」（像凭据上传那样），而不是加大 stdin 帧或命令串；③ 若产品确实要 >32 KiB
  蓝图，先做一次「近上限命令串 + 已测帧大小」的真实沙箱启动自检（不需要模型调用），
  并把结果回写本节与 §12；④ 不要依赖「E2B 有某个上限保护我们」——文档没写，实测只到 1 MiB。
* **缺陷记录**：本次未发现需要修复的真实缺陷；两个**未实测边界**已记入 §12。ACP 面不需要
  为蓝图体积新增客户端分块或落文件机制（当前余量足够）；若将来改动，先按本节复现测量。

### 13.5 测量方法（可复现）

本次测量用生产的 `load_blueprint`/`render_blueprint`/`parse_blueprint` 与 `compose_prompt`：
把 `deep-research.md`（5,550 B）与一个恰好 32 KiB 的合成合法文档分别渲染，再把结果放进
`session/prompt` 的 `params`，用 `json.dumps(..., ensure_ascii=False, separators=(",", ":"))`
量帧；题面上下界用 `web_http.MAX_REQUEST_BYTES`。脚本是临时文件、未入仓（纯合成，
不建沙箱、不调模型）。
## 14. M8 实测（2026-09-25，e2b 面续接 + dsh/codex-acp 真实 resume）

本节每条都标来源。证据文件：`docs/evidence/acp-m8-resume-dsh.txt`（inproc dsh 首轮+追问）、
`docs/evidence/acp-m8-resume-codex-acp.txt`（inproc codex-acp 首轮+追问）、
`docs/evidence/acp-m8-resume-e2b-dsh.txt`（e2b dsh 跨沙箱首轮+追问）、
`docs/evidence/acp-m8-resume-e2b-handshake.txt`（真实沙箱状态根回收自检，不发 prompt）。

### 14.1 e2b 状态根回收（沙箱销毁前的有界镜像）

* 原状态（M7，§10.3/§10.7）：e2b 面无法续接——agent 状态只在沙箱内、随 `kill()` 销毁，
  宿主只回收工作区。新实现（M8，`宿主代码`）：ACP e2b run 的 finalize 在销毁沙箱**前**调用
  `E2BAcpSandboxClient.recover_state()`，把沙箱状态根 `/home/user/acp/home` 整份镜像到宿主
  `runs/<id>/acp-agent/recovered/`。
* 边界（`宿主代码`/`合成实测`）：沿用 M7 继承上限——文件数 4096 / 单文件 8 MiB（= e2b 上传
  上限 `UPLOAD_MAX_BYTES`）/ 总量 64 MiB；递归 listing 有深度上限（12 层）；只收普通文件
  （符号链接不跟随、不导入）；相对路径逐条校验（越界即拒绝整份）；读取经 SDK 元数据或流式
  有界读。任一违规或读失败 = **整份拒绝**：不发布 recovered 根、不留 staging，宿主在终态帧的
  `artifactFailures` 里记录原因。
* 实测（`e2b 实测`，M9）：真实沙箱 + 合成超限状态走同一份 `recover_state()`，单文件轴
  与总量轴各自整份拒绝、不留半份（`recovered` 与 `.recovered.staging` 都不存在）、
  拒绝原因写进 verdict；真实 SDK 的递归 listing 条目带 `size`（目录条目也有），所以
  单文件轴走元数据预检而不是先读满再拒。见 §15.2 与
  `docs/evidence/acp-m9-e2b-recover-limits.txt`。
* 发布顺序（`宿主代码`/`合成实测`）：先写 `.<recovered>.staging`（目录 0700、文件 0600、
  父目录 0700），全部写完后一次 `rename` 发布；因此 `acp-agent/recovered` 的**存在**就是
  “父状态根回收成功”这个持久事实，准入层不读回调、不猜。
* 脱敏（`宿主代码`/`合成实测`）：回收文本扫描出的凭据形状值并入**本 run** 的 redactor（与注入
  凭据同待遇）；回收文件本身不重写——这是 agent 自己的会话存储，重写会破坏要续接的会话
  （与 §10.2 的继承口径一致）。追问继承时 `inherit_acp_state` 再扫描一次，值并入子 run 的
  redactor（合成回归：子 run 复述宿主只在回收文本里见过的 `sk-live-…` 形状值时被 mask）。
* 真实 SDK 形状（`e2b 握手实测`，`acp-m8-resume-e2b-handshake.txt`）：dsh 无 prompt 握手后
  `recover_state()` ok=true，9 文件 / 4417 字节，路径含
  `.dsh/sessions/--home-user-workspace--/<sessionId>/session.v3.jsonl.zstd` 与
  `.dsh/storages/session_projcache/sessions/<sessionId>.json`；凭据源 sha256 前后一致，沙箱按 id 终止。
* 终态帧可见性：收尾时发布第二条 `session_ready`（`stateRootRecovered: true|false`），与 pi 面
  e2b 回收会话文件后重发 `session_ready` 同形（`宿主代码`）。

### 14.2 灌回新沙箱与 ACP-09 续接路径

* 追问 run 的 `_prepare_runtime` 从父 run 的 `acp-agent/recovered` 有界继承（M7 的
  `inherit_acp_state`：拒绝链接、凭据目标排除后重渲染、继承值并入 redactor），所以新 run 的
  每 run HOME 在起沙箱前就含父会话状态（`宿主代码`）。
* `upload_home()` 按 M3b 顺序把该 HOME 灌进新沙箱：先 `make_dir` + `chmod 0700`
  `/home/user/acp/home`，再写文件，最后对本次上传的全部目录 0700、文件 0600（`宿主代码`/
  `合成实测`；合成回归断言 `chmod 0700` 先于继承文件写入、0600 加固覆盖继承文件）。灌入失败
  → run 失败闭环（沙箱在 finalize 销毁），不启动 agent 对半份状态续接。
* 续接走同一份 ACP-09 路径：`session/resume`（优先）或 `session/load`，`expected_session_id`
  核对 + 适配器 initialize 后按自己协商的矩阵复核（`宿主代码`）。
* 会话工作区：e2b 追问复用父 run 的宿主工作区（M7 决定），并把该工作区的 `report.md` 随追问
  灌进新沙箱（与 pi 面 `upload_continuation` 同形）；沙箱内新产物仍在 finalize 回收到宿主工作区，
  子 run 产物发布到自己的 `runs/<child>/artifacts/`（`宿主代码`/`合成实测`/`e2b 实测`）。
* e2b ACP 追问在启动后、发 turn 前核对 `sessionId == 父 sessionId`（`宿主代码`）；不匹配即
  `session_unavailable` 失败，不以新会话冒充追问。

### 14.3 准入放开（原义务 M7 → 新义务 M8）

* 原义务（M7，§10.3）：e2b 面 ACP 追问一律 `session_unavailable`。
* 新义务（M8）：e2b 追问在「同 harness + 已结束 + 最新轮次 + 有 sessionId + 收集到 resume 能力位
  + 父状态根回收成功（`acp-agent/recovered` 存在且是目录）」时受理；其余（能力缺失、异租户、
  非终态、已有后续轮次、回收失败/未回收）维持拒绝。回收到的是整份镜像，失败时连目录都不存在，
  所以“拒绝”先于起沙箱，不预支一次必然失败的云端 run（`宿主代码`/`合成实测`）。
* 行为变更注释：`runner._acp_resumable_parent` 写明原义务（M7）与新义务（M8）；测试
  `tests/test_kernel_acp_e2b.py` 的旧“沙箱面一律拒绝”用例改写为跨沙箱续接全链路 + 回收失败/
  边界/准入矩阵用例（`合成实测`）。

### 14.4 dsh 的真实 resume（inproc）

* 能力矩阵（`dsh 实测`，`acp-m8-resume-dsh.txt` 同次握手）：`sessionCapabilities: {close, list, resume}`，
  `agentInfo: deepseek-harness-acp 0.0.1`，默认模型 `deepseek-v4-flash`、`reasoning_effort=high`；
  无 `loadSession` 位 → 宿主走 `session/resume`（§10.1 的选择规则）。
* 真实「首轮+追问」：父 run `finished`/report 200（655 bytes）；追问 `finished`/report 200
  （676 bytes，末段 `代号 = VELUE-7145`）、`sessionId` 与父相同、`resumeMethod=session/resume`、
  `replayedUpdates=0`；追问 HOME 与父 HOME 有 6 个内容一致的状态文件（含
  `.dsh/sessions/--tmp-…--/<sessionId>/session.v3.jsonl.zstd` 与 storages 投影）；两次 run 的
  流式/终态/report/清理只按持有 PID（`dsh 实测`）。
* 结论：dsh 的持久会话语义在「同一会话目录 slug + 同 sessionId」下成立（会话目录由工作区路径
  派生，inproc 面两轮工作区相同）。

### 14.5 codex-acp 的真实 resume（inproc）

* 能力矩阵（`codex-acp 实测`，本次钉版 `@agentclientprotocol/codex-acp@1.13.1`）：
  `loadSession: true` + `sessionCapabilities: {resume, list, close, delete, fork,
  additionalDirectories, subagents}`；会话模式取值变为 `read-only / agent / agent-full-access`
  （默认 `agent`），M2 的 `mode=auto` 是该包旧版取值，本任务不用
  `PI_GLINK_ACP_SESSION_CONFIG` 纠偏（`codex-acp 实测`）。
* 真实「首轮+追问」：父 run `finished`/report 200（406 bytes）；追问 `finished`/report 200
  （427 bytes，末行 `代号 = KNGQK-2694`）、`sessionId` 与父相同、`resumeMethod=session/resume`；
  追问 HOME 81→82 文件，68 个与父内容一致（含
  `.codex/sessions/2026/09/25/rollout-…-<sessionId>.jsonl`）；清理只按持有 PID
  （`codex-acp 实测`）。
* 结论：codex-acp 1.13.1 的 `session/resume` 在本机真实可用；持久会话存放在
  `~/.codex/sessions/**/rollout-….jsonl`（每 run HOME 内，随继承进入子 run）。

### 14.6 e2b dsh 的跨沙箱续接（真实沙箱）

* 事实（`e2b 实测`，`acp-m8-resume-e2b-dsh.txt`，模板 `glink-acp-worker` 未重建）：父 run
  sandbox `ix3p5icj942x0gjnhxu12`、追问 run sandbox `i6io646oex2y36x2h2ly8`（**新沙箱**）；两轮
  `sessionId=cc97a2d8-bcc1-4311-9e56-5ddf08d653e9` 相同、`resumeMethod=session/resume`；追问
  复述首轮代号 `JVNIJ-3418` 并写入 report.md（763 bytes）；宿主两侧 `acp-agent/recovered` 各 8 个
  文件、6 个内容一致（含 `.dsh/sessions/--home-user-workspace--/<sessionId>/session.v3.jsonl.zstd`、
  `.dsh/storages/session_projcache/sessions/<sessionId>.json`）；两次 run `cleanup.released=true`，
  两个沙箱按持久化 id 经 `confirm_sandbox_absent` 确认不存在（无 `pkill`、无名字通配）；
  `artifactFailures` 为空（`e2b 实测`）。
* 计数与探针缺陷：e2b 侧共 4 轮真实调用。尝试 1 的实质判据全部通过（异沙箱/同 sessionId/代号
  回环/两侧回收/按 id 清理），但探针 `child_ok` 读取了回收前的首帧 `session_ready`
  （`stateRootRecovered: null`）而误报 FAIL；探针改读末帧后重跑 2 轮得到 PASS。两次尝试的原始
  事实见证据文件（`e2b 实测`）。
* 回收形状的真实 SDK 自检（`e2b 握手实测`）：见 §14.1 末条。

### 14.7 宿主侧实现变更（M8）

* `kernel/e2b_env.py`：`recover_state()` + `_publish_recovered_state()`（有界整份镜像、staging
  发布、凭据形状值回传）；`upload_continuation()`（追问把 `report.md` 灌回新沙箱工作区）。
* `kernel/acp_env.py`：`ACP_HOME_DIRNAME`/`ACP_RECOVERED_DIRNAME` 常量；`prepare_acp_runtime`
  改用 HOME 常量（行为不变）。
* `kernel/acp_execution.py`：e2b ACP 收尾调用状态回收并回传 verdict/secrets；`session_ready`
  增 `stateRootRecovered`；追问启动时灌回工作区。
* `kernel/execution.py`：`ArtifactRecovery` 增 `state_recovered`/`secrets`（默认 `None`/`()`，
  其余执行面行为不变）。
* `kernel/runner.py`：`_acp_state_home(run)` 按执行面返回宿主 HOME 或 recovered 根；
  `_acp_resumable_parent` 去掉 e2b 无条件拒绝（原义务/新义务注释）；e2b ACP 追问启动后核对
  sessionId；finalize 记录回收 verdict（第二条 `session_ready`）并把回收文本里的凭据形状值并入
  run redactor。
* 合成回归：`tests/test_kernel_acp_e2b.py`（跨沙箱全链路、回收上限三轴、回收失败拒绝、灌回
  失败 fail-closed、灌回顺序、准入矩阵（含异租户）、继承值脱敏、路径安全）；`bash scripts/check.sh`
  全绿（Python 775 / Node 116）。
* live 探针：`scripts/spikes/acp_e2b/resume_smoke.py`（首轮+追问；判据含异沙箱/同 sessionId/
  代号回环/两侧回收/按 id 清理）；`sandbox_handshake.py --recover-state`（不发 prompt 的真实
  沙箱回收自检）。

### 14.8 与既有面的差异与风险

* **执行面差异不是 harness 差异**：inproc/docker 继续用宿主可见的 `acp-agent/home`；e2b 用
  `acp-agent/recovered`。两者由 `_acp_state_home` 按 `LOCAL_WEB_EXECUTOR` 选择（`宿主代码`）。
* **回收上限对长会话**：与 §10.7 同一个 64 MiB / 8 MiB 上限；e2b 面另有 listing 深度 12 与
  单次 listing 体积（SDK 一次性返回）未由宿主直接限制，超出时以错误/超时拒绝（fail-closed）。
  **M9 实测补充（§15.2）**：单次 listing 在 4101 条目（4100 文件 + 1 目录）下完整返回；
  文件数刚触顶时代价是逐个流式读取（实测约 0.2s/文件，4096 文件约 13–15 分钟）——
  长会话逼近文件数上限时 finalize 会明显变长，这是当前整份镜像设计的已知成本。
* **状态文件权限**：沙箱内状态文件权限由 agent 自己决定（上传期是 0600/0700）；宿主侧落盘
  固定 0600/0700（`宿主代码`）。
* **回收期间 run 仍在流**：回收发生在 finalize、终态帧之前；回收文本发现的值只并入 redactor，
  不把内容投影进事件流（`宿主代码`）。

## 15. M9 实测（2026-09-25，docker 面续接 + e2b 回收上限触顶）

本节每条都标来源。证据文件：`docs/evidence/acp-m9-docker-resume-dsh.txt`（docker 面 dsh
首轮+追问）、`docs/evidence/acp-m9-e2b-recover-limits.txt`（真实沙箱 + 合成超限状态走生产
回收路径；无模型）、`docs/evidence/acp-m9-e2b-recover-refusal.txt`（真实 run 回收失败 →
追问准入拒绝）。**本任务没有 kernel 行为改动**：live 未暴露需要修 kernel 的真实缺陷，两处
缺陷都在本任务新增的探针里（§15.4）。

### 15.1 docker 面 ACP 续接（dsh，首轮 + 追问）

* 事实（`docker 实测`，`acp-m9-docker-resume-dsh.txt`；镜像 `pi-glink-acp-worker:latest`
  sha256:fb0b9c40f065…，未重建）：父 run `finished`/report 200（967 B），追问 `finished`/
  report 200（988 B，末尾 `代号 = ODOPA-6785`）；两轮容器名 `pi-glink-5e7a4bef2ea2` →
  `pi-glink-efda45c14262`（**新容器**）；`sessionId` 两轮相同
  （3c113656-6f28-46d1-8f66-d53c524448f7）、`session_ready.resumed=true`、
  `resumeMethod=session/resume`、`cwd=/workspace`，事件流有 `acp_session_resumed`。
* 状态继承（`docker 实测`）：父/子 run 的宿主可见 HOME 各 8 个文件、6 个内容一致，其中
  3 个文件名含父 sessionId（`.dsh/sessions/--workspace--/<sessionId>/session.lock`、
  `session.v3.jsonl.zstd`、`.dsh/storages/session_projcache/sessions/<sessionId>.json`）。
  子容器的 `docker run` argv（宿主日志原文）把这份 HOME 以 rw 挂到 `/home/acp`、把凭据
  文件嵌套 `:ro`；容器内 `session/resume` 成功并复述首轮代号，即“继承状态根经挂载进入
  容器”的直接观测（挂载契约本身见 §5.2/§5.4）。
* 清理（`docker 实测`）：两轮 `cleanup.released=true`，`confirm_absent` 都按**精确容器名**
  确认不存在，`docker ps -a` 无 `pi-glink-` 前缀容器；宿主只按脚本持有的 PID `terminate`
  （exit=-15），后代只按已知 PID 记录。没有 `pkill`、没有名字通配。
* 模型调用：2 轮（首轮 + 追问）。

### 15.2 e2b 状态根回收上限触顶（真实沙箱，合成状态；无模型）

* 方法（`e2b 实测`，`acp-m9-e2b-recover-limits.txt`）：每个轴一个真实沙箱，
  `E2BAcpSandboxClient.start()` → `upload_home()` → 沙箱内命令写合成状态（**不走宿主上传
  通道**，所以单文件可以超过 8 MiB 上传上限）→ 生产 `recover_state()`。`control` 轴（2 个
  小文件）证明这条路径本身可用。
* 三条轴的实测（`e2b 实测`，e2b SDK 2.51.0）：
  - `control`：ok=true，2 文件 / 30 B，发布 `state-a.json` 与 `sub/state-b.txt`，发布内容
    sha256 与沙箱内合成内容一致；
  - `single`（9437184 B > 8388608 B 单文件上限）：ok=false，
    `failures=["沙箱状态文件不可读取或超过上限: bloat.bin: 文件超过上限（9437184 > 8388608 字节）"]`，
    0.23s；
  - `total`（9 × 8 MiB，共 75497501 B > 64 MiB）：ok=false，
    `failures=["沙箱状态超过总量上限 67108864 字节"]`，3.74s；
  - `count`（4100 个空文件 > 4096 文件数上限）：ok=false，
    `failures=["沙箱状态文件数超过上限 4096"]`，1026.73s。
  - 三条轴都：宿主 `acp-agent` 只有 `home`（无 `recovered`、无 `.recovered.staging`），
    沙箱内状态前后一致，沙箱按 id `confirm_sandbox_absent` 终止。
* 真实 SDK 形状（`e2b 实测`）：递归 listing 条目带 `size`（`EntryInfo.size`，目录条目也有
  size），单文件轴因此走元数据预检而不是先读满再拒；一次 `files.list(depth=12)` 在 4101
  条目（4100 文件 + 1 目录）下完整返回。`_read_file_bounded` docstring 里“真实 SDK 的
  size/stream 形状待实测”由此闭合。
* 风险（`e2b 实测`）：文件数轴的处理代价是逐文件一次流式读取，实测约 0.25s/文件（4096
  文件 1026.73s）；本次沙箱寿命 `900+300=1200s` 只剩约 3 分钟余量。按默认任务超时 300s
  （寿命 600s）跑同一状态根，读会在沙箱到期时失败——结论仍是 fail-closed（不发布 recovered
  根、追问被拒），但拒绝原因会变成读失败而不是计数上限。见 §14.8 与 §12（自然长会话仍未观测）。

### 15.3 回收失败 → 追问准入拒绝（真实 run）

* 事实（`e2b 实测`，`acp-m9-e2b-recover-refusal.txt`）：真实宿主 + 真实沙箱跑一次 dsh 父
  run（真实模型），在回合进行中（`session_ready` 已出现、run 仍 `running`）用真实 SDK
  `Sandbox.connect(sandbox_id)` 连到**同一个沙箱**，把 9437184 B 的合成文件写进
  `acp-agent` 状态根；父 run finalize 里的生产回收**真的失败**：
  - 末条 `session_ready.stateRootRecovered=false`，终态帧
    `artifactFailures=["沙箱状态文件不可读取或超过上限: bloat.bin: 文件超过上限（9437184 > 8388608 字节）"]`；
  - 宿主 `acp-agent` 只有 `home`（无 `recovered`、无 `.recovered.staging`）；
  - `POST /api/runs`（`parentRunId`）→ HTTP 409 `{"error":"session_unavailable"}`，且拒绝
    前后 `GET /api/runs` 与 `Sandbox.list()` 对比没有新建 run、没有新建沙箱（沙箱在起
    之前就被拒）；
  - 父沙箱按持久化 `sandbox_id` 终止（`cleanup.released=true`）。
* 准入条件逐条核对（`宿主代码`/`e2b 实测`）：同 harness（首帧 `harness=acp`）、终态、
  无后续轮次、`sessionId` 已记录、`acp_resume_method()` 判为 `session/resume`——唯一不
  满足的是 `acp-agent/recovered` 不存在，所以这次 409 就是 §14.1/§14.3「回收失败 → 追问
  拒绝」这条规则的拒绝。
* 模型调用：2 轮（尝试 1 判据缺陷、尝试 2 干净通过，缺陷见 §15.4）。

### 15.4 探针缺陷与取舍（都在探针里，kernel 无改动）

1. `scripts/spikes/acp_docker/resume_smoke.py` 的 `docker_inspect()` 用 Go template 的
   `{{range}}` 拼 JSON 数组，尾逗号让 `json.loads` 失败 → 快照恒为 None，两条由快照派生的
   自检判据打印 False。已修（env 名逐行读取 + 记录失败原因），修复后的函数在一次性容器上
   实测通过；为守授权上限（≤4 轮）没有重跑模型部分。
2. `scripts/spikes/acp_e2b/recover_refusal_admission.py` 把 `sessionCapabilities.resume`
   当布尔判断，而 dsh 报的是空对象 `{}`（存在即声明）；已改用生产函数
   `acp_resume_method()`，重跑得到 PASS。
3. `count` 轴代价：逐文件流式读取（实测约 0.25s/文件）让 4096 文件的触顶耗时 1026.73s；
   本任务把它跑完并记录，而不是用更小的合成上限替代真实上限。
4. 本任务新增两个探针（§15.2/§15.3）与一个 docker 续接探针（§15.1）；**没有**改
   `tests/` 里的既有断言，也没有改 `host/pi_glink/kernel/`。

### 15.5 M9 与既有面的差异与风险

* **docker 面续接与 inproc 同形**：同一份适配器与准入规则，区别只在状态根的承载（bind
  mount vs 宿主目录），本次 live 未出现行为差异（`docker 实测`）。
* **回收是整份镜像**：文件数上限触顶的代价与文件数线性（§15.2）；长会话逼近上限时 finalize
  会变长，沙箱寿命可能先到（结论仍 fail-closed，但拒绝原因不同）。若要缩短，需要按 agent
  白名单收缩状态根或提高限制，属产品决策（未在本任务改动）。
* **e2b 面回收失败后的追问**：拒绝发生在起沙箱之前（§15.3），不预支一次注定失败的云端 run。
