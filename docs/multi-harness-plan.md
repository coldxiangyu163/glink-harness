# 多 Harness 平台化：设计构想与推进计划

[架构](architecture.md) · [工程约束](engineering.md) · [pi 边界](pi-boundary.md)

本文记录「从单一 pi 内核走向多 harness 平台」的方向决定、目标架构、复用策略与里程碑。
状态（2026-09-24）：方向已经用户确认；**M1（GLINK-ACP-01）已完成合成验证**
（协议事实、映射口径与实测边界见 [ACP 集成笔记](acp-integration-notes.md)）。宿主默认
harness 仍是 `pi`，ACP 族只在显式配置下启用且限 inproc；真实 agent 与真实模型调用（M2）
未执行。本文是该方向的事实源：实现与本文出现偏差时回写本文，而不是让代码与文档各自漂移。

## 1. 决定与定位

* 宿主（run 编排、事件总线、存储、SSE、租户、脱敏、资源台账）保持 **harness 无关**；
  `kernel/` 从「pi 接入层」泛化为「harness 适配层」。
* 目标候选：deepseek-harness（dsh，原生 ACP server）、Claude Code、Codex，以及一切
  ACP 兼容 agent（Gemini CLI、OpenCode、Kimi 等）。
* **不变量**：产品事件词汇（`events.py`）语义不改；前端不感知 harness 差异；多租户、
  凭据边界、脱敏、资源台账的既有承诺不因 harness 增加而削弱；run 生命周期与终态判定表
  （架构 §4）继续是唯一定论。
* **明确不做**：
  * 不为尚无消费者的 harness 预建空适配器或抽象框架（工程约束：不预建无消费者的抽象）；
    抽象随第一个真实 harness（ACP 族）的接入而产生。
  * 不 fork / patch / vendor 任何 harness；一切接入走对方官方文档化的协议或扩展点。
  * 不以终端文本解析为接入机制（coder/agentapi 的 PTY 屏幕解析路线与本仓库
    「前端不解析终端文本」的底线冲突，排除）。
  * M1 不引入真实模型调用；合成验证先行，真实冒烟单独授权。

## 2. 现状接缝（为什么可行）

宿主约 12k 行中 pi 特异约 2.5k 行，全部集中在 `kernel/` 与 `agent/`，边界清晰：

| 已 harness 无关（不动） | pi 特异（泛化对象） | 泛化后的形态 |
|---|---|---|
| 产品事件词汇、EventBus、RunStore、提交水位、SSE 重放 | `kernel/projection.py`（pi 事件 → 产品事件） | 每族一个 projector |
| 租户、准入、资源台账、数据根所有权 | `kernel/agent_env.py`（渲染 models/auth/settings、argv、每 run 配置目录） | 每 harness 一个配置渲染器；**每 run 独立配置目录、凭据走文件不走 env 的模式照用** |
| `redaction.py` 流式脱敏（作用于文本） | `kernel/rpc.py` + `framing.py`（pi 双向 RPC） | 每族一个协议客户端；LF 分帧复用 |
| report.md 产物契约（prompt 约束） | `agent/`（pi settings、扩展、prompt 模板） | 每 harness 一份可版本化配置包 |
| `ExecutionAdapter` 契约（start / deliver_input / wait / cancel / recover_artifacts / close / identity） | 宿主 `/` 命令准入门禁、stopReason 词汇、会话文件复制续接 | 每 harness 各自的门禁/权限策略与续接能力声明 |

执行面（inproc / docker / e2b）与 harness 是**正交两维**：适配器数量是
「N 个 harness 族 × 2~3 个传输族」，不是全矩阵。

## 3. 目标架构：三个适配族

```
HarnessAdapter（抽象随 M1 落地）
├─ pi 族   — 现有 kernel/ 原样：rpc（双向 stdio NDJSON，inproc+docker 共用）、
│            json（单向流，e2b）；作为其它族的参照系与回归基线
├─ ACP 族  — 一次 ACP client（JSON-RPC over stdio）覆盖：
│            dsh 原生 acp server、claude-code-acp、codex-acp（@zed-industries/codex-acp）、
│            gemini --acp、opencode；适配器进程与 agent 同容器/沙箱，stdio 桥接
└─ SDK 直连族（可选，暂不建）— 如 claude-agent-sdk Python 进程内集成
```

ACP（Agent Client Protocol，Zed 主导的开放协议）关键能力（v1 schema 已核实）：
`initialize` 能力协商、`session/new`、`session/load`（带历史回放）、`session/resume`
（不回放，能力位 `sessionCapabilities.resume`）、`session/prompt`（返回 stopReason）、
`session/cancel`、`session/update` 通知（`agent_message_chunk` / `agent_thought_chunk` /
`tool_call` / `tool_call_update` / `plan` / `available_commands_update` /
`current_mode_update` / `usage_update`）、`session/request_permission`（agent → client）、
客户端方法 `fs/*` 与 `terminal/*`（默认全部不声明，agent 自行操作自身文件系统——
正好契合沙箱内运行拓扑）。

## 4. ACP 族设计要点

### 4.1 事件投影映射（ACP → 产品事件）

| ACP | 产品事件 | 语义备注 |
|---|---|---|
| `initialize` 响应（capabilities、agentInfo） | `session_ready` | 能力矩阵一并入 run 记录 |
| `agent_message_chunk` | `assistant_delta` | 流式正文 |
| `agent_thought_chunk` | `thinking_delta` | 思考折叠块 |
| `tool_call` / `tool_call_update`（status: pending→in_progress→completed/failed） | `tool_started` / `tool_updated` / `tool_finished` | kind/rawInput/rawOutput 入工具卡片 |
| `plan` | `run_stage` | 计划条目作为阶段行呈现 |
| `usage_update` | `usage_reported` | **语义换算**：ACP 是「上下文窗口占用 + 累计成本」视角，产品语义是「逐请求累计求和」；投影层如实换算并在 run 记录标注口径差异，不伪造逐请求 token 数 |
| `available_commands_update` | `kernel_status`（警告） | 见 §4.3 门禁 |
| `session/prompt` 返回 `stopReason` | 终态判定输入 | `end_turn`→正常；`max_tokens`≈ pi 的 `length`（finished 但标注交付不完整）；`refusal`→failed/model_error 类；`cancelled`→取消路径 |
| `session/request_permission` | 不投影为交互，自动应答（§4.2） | 应答结果记 `kernel_status` |

run 生命周期、终帧即终态、提交先于发布等全部沿用架构 §3/§4，不因 harness 改变。

### 4.2 权限自动应答（无人值守前提）

沙箱/无人场景没有人应答权限询问。M1 默认 **fail-closed 全拒**（合成验证用）；真实冒烟
（M2）前按 harness 逐个定义白名单策略并记录。拒绝本身必须进入事件流（`kernel_status`），
不允许静默。

### 4.3 准入与门禁

ACP prompt 是结构化 ContentBlock，不存在 pi 那样的「`/` 前缀先派发扩展命令」面；但
`available_commands_update` 表明 agent 可能自带 slash 命令——准入层对 ACP 族拒绝
以 `/` 开头的 prompt（同形复用现有门禁入口，允许清单为空集，从 harness 能力推导而非
硬编码模板目录）。

### 4.4 续接（resume）

* pi 族：复制会话文件 + `--session`（现状，不动）。
* ACP 族：`session/resume`（能力位声明）；resume 依赖 agent 自身状态目录
  （`~/.claude`、`~/.codex`、dsh sessions）——inproc 面状态目录每 run 独立渲染；
  docker/e2b 面沿用「销毁前回收状态目录、追问时灌回新沙箱」的现有会话回收模式。
* 能力缺失时产品层显式拒绝追问（不冒充、不降级为静默新建）。

### 4.5 凭据与配置渲染

沿用 `agent_env.py` 模式：每 run 渲染独立配置目录，凭据写文件或注入命令级 env，
不进宿主进程环境，全部出站过 `redaction.py`。各 harness 凭据注入点逐个实测记录
（M2）：claude-code-acp（`ANTHROPIC_API_KEY` env）、codex-acp（`OPENAI_API_KEY` /
`auth.json`）、dsh（credentials 包形态待实测）。

## 5. 开源复用策略

| 项目 | 复用方式 | 约束 |
|---|---|---|
| ACP 适配器（claude-code-acp、@zed-industries/codex-acp、dsh acp） | 作为**钉版外部依赖**接入（npm 包/二进制），记录版本、来源、license | 版本钉死 + 冒烟 + 每 harness 一份集成笔记（沿用 pi-boundary 纪律）；适配器缺陷不改对方源码，反馈上游或换版本 |
| vibe-kanban（BloopAI，已停摆转社区维护） | **只读借鉴**：`crates/executors` 的 per-agent normalize 实现是事件归一化字典 | 不引入运行时依赖 |
| 官方 SDK（claude-agent-sdk Python、@openai/codex-sdk TS） | SDK 直连族的候选（暂不建）；其事件定义作为投影语义参照 | Python SDK 才可进程内；TS SDK 仅参考 |
| VibeKit / OpenHands / terminal-bench / gate4agent | 形态与传输矩阵的参照证据 | 不引入 |
| coder/agentapi | **排除**：PTY 屏幕解析机制违背本仓库事件契约底线 | — |

ACP 客户端本体用 Python 标准库实现（subprocess + json），不新增第三方依赖；
确需新增时论证并记录消费者。

## 6. 里程碑

| 里程碑 | 内容 | 验收 | 状态 |
|---|---|---|---|
| M1（GLINK-ACP-01） | ACP 族 inproc 骨架：ACP stdio JSON-RPC 客户端（复用 LF 分帧）、projector、harness 注册与配置级选择（默认仍为 pi，不动 API/前端）、run 全生命周期接入（受理/流式/取消/终态/产物回收）、fail-closed 权限应答、合成 fake ACP server 测试 | `bash scripts/check.sh` 全绿；合成测试覆盖 §4.1 映射、取消、stopReason 判定、权限拒绝、门禁；现有 pi 三面测试零回归 | 已完成（合成验证；实现口径与待实测项见 [ACP 集成笔记](acp-integration-notes.md)） |
| M2 | 真实冒烟：`dsh acp` 与 `codex-acp` 各跑一次真实研究 run（inproc）；产出 `docs/acp-integration-notes.md` 实测记录（能力矩阵、凭据注入点、usage 口径、版本事实） | 真实 run 报告产物 + 集成笔记 | 未开始，**需真实凭据与模型调用授权** |
| M3 | docker / e2b 执行面扩展；e2b 双向 stdio（`stdin=True` + `send_stdin`，SDK 2.x 已具备）帧完整性实测先行；模板钉版 | 三面 smoke | 未开始 |
| M4 | claude-code-acp 接入；能力矩阵补齐；如需，前端 harness 标识与按 run 选择 harness 的 API | 按当时验收 | 未开始 |

每个里程碑独立验收、独立提交边界；里程碑之间允许因实测结论回写本文。

### M1 落地偏差（相对 §3–§4；详细口径见 [ACP 集成笔记](acp-integration-notes.md)）

* **配置开关**：`PI_GLINK_HARNESS=pi|acp`（默认 `pi`）+ `PI_GLINK_ACP_COMMAND`；只在配置层，
  无 API/前端参数，`public_view()` 不暴露 harness。ACP 与 docker/e2b 组合在启动即拒绝
  （M3 做传输面）。
* **续接（§4.4）**：M1 不实现 ACP 的 `session/resume`；ACP run 不落会话文件，追问在准入层以
  `session_unavailable` 拒绝，适配器也不会以新会话冒充追问。
* **用量（§4.1）**：`usage_update` 保留 ACP 的「上下文窗口占用 + 累计成本」视角，投影为
  `used/size + cost` 并标注 `basis: "acp_usage_update"`；不做逐请求 token 求和，终态记录采用
  最后一个快照（替换而非累加）。
* **状态目录（§4.4/§4.5）**：inproc 面每 run 渲染 `runs/<id>/acp-agent/home` 作为子进程
  `HOME`，不注入任何宿主凭据；各 agent 是否还认专用变量（`CODEX_HOME` 等）与凭据注入点
  逐 harness 实测属 M2。
* **未投影的更新**：`user_message_chunk`、`current_mode_update`、`session_info_update` 等
  §4.1 未定义的变体不猜语义，按类型一次性记 `kernel_status` 提示；`max_turn_requests` 以原词
  进入 `stopReason`（前端目前只对 `length` 提示「交付不完整」）。

## 7. 风险与纪律

* **ACP 能力参差**：`loadSession`/`resume`/`usage_update` 均为可选能力——以实测矩阵为准，
  能力缺失走显式降级，不伪造。
* **dsh 处于 developer preview**（官方明示 breaking changes）：钉版 + 集成笔记，同 pi 纪律。
* **事件语义鸿沟**（usage/stopReason/工具状态机）：产品事件语义由宿主定义，projector 换算；
  换算不了的如实标注，不塞进近似语义。
* **权限无人应答**：fail-closed 默认；任何放行白名单需逐 harness 论证并记录。
* **升级路径**：每个外部适配器/harness 的版本固定点与升级冒烟步骤，写进对应集成笔记，
  参照 [pi 边界](pi-boundary.md) §3 的三处固定模式。
