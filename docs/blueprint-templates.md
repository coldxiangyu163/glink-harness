# 会话蓝图（blueprint）模板

[开发入口](../AGENTS.md) · [架构](architecture.md) · [工程约束](engineering.md) · [多 Harness 计划](multi-harness-plan.md)

本页是 GLINK-ACP-10（第一增量）的格式契约与设计边界：宿主侧、**harness 无关**的会话模板。
蓝图声明一次研究会话的流程骨架与**产出契约**（阶段、每阶段目标、交付物与检查点），
把回合内执行留给 harness 自治——约束结果，不约束过程。

状态（2026-09-25）：第一增量已实现并通过合成验证（`tests/test_blueprints.py`），
覆盖蓝图定义、准入、渲染与 run 记录；**多 run 编排、阶段推进、前端呈现、租户自定义蓝图
均未实现**，见下文「挂账的后续增量」。真实冒烟证据（若执行）写在 `docs/evidence/blueprint-*.txt`。

## 1. 定义与位置

* 蓝图是仓库级内容，放在 `blueprints/`（**不放 `agent/`**：那是 pi 的专属配置目录，
  蓝图必须对 pi 与 ACP 族同时成立）。
* 一个蓝图一个文件：`blueprints/<name>.md`，`<name>` 同时是 API 取值与 run 记录里的标识。
  文件名必须是 slug（`^[a-z0-9][a-z0-9-]{0,63}$`），目录里其它文件不构成蓝图。
* 目录位置可用 `PI_GLINK_BLUEPRINTS_DIR` 覆盖（默认 `<repo>/blueprints`）。
  目录缺失不是启动错误：它只意味着可用蓝图集合为空，任何蓝图 run 都会被拒绝。
* 宿主自身不持有蓝图内容、也不硬编码名字：**每次准入都从目录重新推导**可用集合，
  文件增删立即生效（符号链接、非常规文件、非 `.md` 不计入）。
* 内置示例：`blueprints/deep-research.md`。它的方法论与 `agent/prompts/deepresearch.md` 同源
  （同一批取证、覆盖声明、自我核查与「来源与核查」义务），只是换成结构化的阶段形式；
  本增量不新增未经验证的流程主张。

## 2. 格式契约

文件由 **TOML frontmatter**（`+++` 围栏之间）与**正文模板**（第二个围栏之后）组成。
使用 `+++` 而不是 YAML 的 `---`：宿主用标准库 `tomllib` 解析，接受的子集就是 TOML 能表达的
子集，不引入 YAML 依赖，也不靠编辑器启发式判断。解析是**全有或全无**：任何一处不合契约即拒绝，
不做尽力而为的部分渲染。

```markdown
+++
format = "pi-glink-blueprint/v1"
name = "my-topic"            # 必须等于文件 stem
description = "一句话说明这个会话要交付什么。"

[[stages]]
id = "collect"               # slug，同一蓝图内唯一
title = "取证"
goal = "这一阶段要达成什么。"
deliverable = "这一阶段的产出契约（交付物 + 质量/证据要求）。"
checkpoint = "进入下一阶段前必须成立的自检声明。"
+++

正文模板：方法论与执行约定；随蓝图一起进入 system prompt，但不替代用户请求。
```

| 字段 | 必填 | 规则 |
|---|---|---|
| `format` | 是 | 固定 `"pi-glink-blueprint/v1"`（版本标记；未知取值拒绝） |
| `name` | 是 | 非空，且等于文件 stem（标识与文件不允许漂移） |
| `description` | 是 | 非空 |
| `stages` | 是 | 非空表数组，顺序即阶段顺序 |
| `stages[].id` | 是 | slug，唯一 |
| `stages[].title` | 是 | 非空 |
| `stages[].goal` | 是 | 非空 |
| `stages[].deliverable` | 是 | 非空；本阶段的产出契约 |
| `stages[].checkpoint` | 是 | 非空；**声明**检查点，宿主不据此暂停或推进 |

校验与边界：

* **未知键拒绝**（顶层与 stage 都是）：写成 `deliverble` 这类拼写错误必须让准入失败，
  而不是静默丢掉那份契约。
* 文件大小上限 32 KiB、必须 UTF-8：渲染结果要经 argv（pi 面）或沙箱命令串（E2B 面）下发，
  无上限的文件不予放行。
* 正文模板必须非空。
* 蓝图的**标识**（stem/`name`）与**阶段元数据**会写入 run 记录；渲染文本本身不入记录。

## 3. 准入

`POST /api/runs` 接受可选字段 `blueprint`：

* 取值只能是 `blueprints/` 目录实际存在的模板 stem（从目录推导，不硬编码），**精确匹配**
  （不做大小写折叠、不裁剪空白、不接受路径分隔或 `..`）。
* 未知名称 → `400 {"error": "unknown_blueprint", "available": [...]}`；
  存在但格式不合契约 → `400 {"error": "invalid_blueprint", ...}`；
  字段不是字符串 → `400 {"error": "invalid_request", ...}`。
* 拒绝形态与 `/` 命令门禁一致：只报**推导出的**可用名称列表，不回显提交值，也不回显文件内容。
* 拒绝发生在凭据解析、父 run 读取、run 目录创建与执行面创建**之前**。
* 规则**与 harness 无关**：`PI_GLINK_HARNESS=pi|acp` 走同一条准入路径、同一套失败形态。
* 多租户归属规则不变：蓝图只是记录的附加字段，owner 仍只来自服务端验证过的 `TenantContext`。

未指定 `blueprint` 的 run **零变化**：system prompt 与旧行为逐字节相同，记录字段为空。

### 3.1 列举（`GET /api/blueprints`，只读）

列出宿主当前可用蓝图（`name`/`description`/阶段元数据），供前端/CLI 消费；stage 字段
与 run 记录的 `blueprintStages` 同形（`id`/`title`/`goal`/`deliverable`/`checkpoint`）。

* **与准入同源**：同一套目录推导与解析（`blueprint_names` + `load_blueprint`），不硬编码名字，
  文件增删即时生效；准入会拒绝的候选（格式/大小/编码不合契约）不进列表。
* **鉴权与既有 `/api/*` 一致**：多租户下要求可信身份（未认证 `401`）；蓝图是**宿主级资源**，
  认证后各租户看到同一份列表（与 `/api/config` 同形）。逐租户可见性属于未实现的租户自定义蓝图增量。
* **空目录/缺失目录**：两者都回 `200 {"blueprints": []}`。可用集合为空是合法部署状态
  （准入同样不把目录缺失当启动错误），目的为了让空部署不表现为故障；两种状态刻意不可区分，
  且不回显宿主路径。
* **只读**：上传/编辑/删除没有任何实现，`POST /api/blueprints` 落到统一的
  `404 unknown_endpoint`。
* 列表只给标识与阶段契约，**不下发渲染后的 system prompt 正文**。

## 4. 渲染与送达

渲染是**纯函数**：同一蓝图文档永远得到逐字节相同的文本；文本中不含 host 路径、run id、
时间戳或凭据。渲染结果只进入 **system prompt 通道**，用户 prompt 走自己的通道、逐字节不变：

| 面 | 通道 |
|---|---|
| pi（inproc / docker / E2B 命令） | `--append-system-prompt <工作约定 + 蓝图渲染>`（同一次 append，不改 pi 自身 prompt） |
| ACP 族（dsh / claude / codex / opencode） | `session/prompt` 的**首个**围栏文本块：`<system-prompt>` 内先工作约定、后蓝图渲染（ACP v1 无 system role，见计划 §4.5） |

渲染文本以 `<blueprint name="…">` 围栏标出，明确「宿主注入的产出契约，不是用户请求」，
并逐阶段列出目标 / 产出契约 / 检查点。

## 5. 记录

* run 记录（`meta.json` 与 `RunRecord`）新增：`blueprint`（模板标识或 `None`）与
  `blueprint_stages`（阶段元数据：`id`/`title`/`goal`/`deliverable`/`checkpoint`，保持顺序）。
  新记录的 `meta.json` 因此多出这两个字段；读取端过滤未知键，旧版本读新记录时忽略它们。
* HTTP 视图 `run_view` 对应 `blueprint` 与 `blueprintStages`，列表与详情同源；旧记录没有这两个
  字段时按空值读出（向后兼容，不改写历史）。
* **只记录，不推进**：宿主不维护阶段状态、不做检查点闸门。
* **不新增产品事件类型**：事件流（`events.py` 词表）不变，蓝图信息不进 payload；
  合成回归对照「同一 run 带/不带蓝图」的事件类型集合与 payload 键。

## 6. 设计边界与非目标

* 蓝图是**会话模板**，不是调度器、不是权限框架、不是多租户资源单位。
* 本增量不碰 `agent/`（pi 专属配置）、`docker/`、`web/`（前端）、`scripts/`、pi 或 ACP 的既有行为。
* 蓝图文本**不复制**进 per-run agent 配置目录：它是宿主渲染并注入的文本，不是 agent 的配置文件。
* 启动时不校验蓝图内容：坏文件只在被请求时 fail-closed 拒绝，不影响无关 run。
* 列举只有只读的 `GET /api/blueprints`（§3.1）；蓝图的上传/编辑/删除仍没有实现。

## 7. 挂账的后续增量

以下能力**未实现**，各自需要独立的有界任务与（部分）先取得决定：

1. **多 run 编排**：一个蓝图拆成多个 run（阶段级 run、交接、并行分支）时的受理、配额、
   取消传播与产物归属。必须先决定 run 级契约与并发语义。
2. **阶段推进**：host 侧阶段状态、检查点闸门、阶段完成事件。会改动产品事件词汇与
   SSE/存储语义，属于[需要先取得决定的变更](engineering.md#需要先取得决定的变更)。
3. **前端呈现**：蓝图选择器、阶段进度与产出的可视化。改动 UI 标准，先按
   [UI 对齐](ui-alignment.md)更新标准再实现。
4. **租户自定义蓝图**：租户级目录、存储与配额、共享/私有、审计与脱敏边界；
   与多租户存储隔离一并设计，不能靠共享宿主目录。
5. 次要候选项：蓝图与 `/deepresearch` 模板的关系
   （当前两者内容同源但分别维护）；租户自定义蓝图（见上）。

> 2026-09-25（GLINK-ACP-12）：`GET /api/blueprints` 只读列举已实现并有合成回归；
> 更大 prompt 体积在 E2B 通道上的上限评估见
> [ACP 集成笔记 §13](acp-integration-notes.md#13-蓝图-system-prompt-在-e2b-通道上的体积评估调查)。

## 8. 验证

* 格式与准入：`python -m pytest tests/test_blueprints.py`（目录推导、路径/符号链接拒绝、
  格式矩阵、大小与编码边界、渲染确定性、双 harness 送达、记录字段、事件零新增、租户归属）。
* 受影响既有回归：`python -m pytest tests/test_runner.py tests/test_runs_api.py
  tests/test_kernel_acp_runner.py tests/test_store.py tests/test_agent_prompts.py`。
* 默认交付检查：`bash scripts/check.sh`。
* 真实冒烟（可选、≤1 次、inproc、需授权）：临时数据根 + 本仓库配置启动宿主，
  用内置 `deep-research` 蓝图跑一次最小 run，证据存 `docs/evidence/blueprint-*.txt`；
  未执行时在交付报告中如实标注未覆盖。
