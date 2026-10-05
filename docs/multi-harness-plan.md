# 多 Harness 接入与通用执行服务计划

[架构](architecture.md) · [工程约束](engineering.md) · [pi 边界](pi-boundary.md)

长期方向是 **产品无关、API-first 的原生 harness 通用执行服务**，职责与解耦约束以
[架构：产品方向与当前实现](architecture.md#产品方向与当前实现)为准。本文保留原生包机制、执行
持久化与接入证据，§6 分别记录已完成的接入/服务化里程碑与尚未实施的服务化工作，不绑定特定应用，
也不把应用数据库作为运行依赖。

既有接入状态（2026-09-25）：**M1（GLINK-ACP-01）已完成合成验证**；
**M2（GLINK-ACP-02）已完成真实冒烟**：`dsh --profile acp`（dsh 0.1.5-rc.3）与
`codex-acp`（@zed-industries/codex-acp 0.16.0，M3a 起后继包 @agentclientprotocol/codex-acp）各完成真实 initialize/session/new 握手，并经
宿主 HTTP API 各跑一次最小真实研究 run（流式事件、终态、`report.md` 回收全部验证）；
system prompt 送达通道与每 run 凭据注入已实现。逐条实测事实、能力矩阵与权限决策见
[ACP 集成笔记](acp-integration-notes.md) §4，证据在 `docs/evidence/acp-m2-*.txt`。
**M3a（GLINK-ACP-03）已完成**：ACP 族的 Docker 执行面可用——新增自包含的 ACP worker
镜像（`docker/Dockerfile.acp-worker`），容器 stdio 直接桥接 M1 客户端，每 run HOME 与只读
凭据挂载、cpus/memory/pids 限制与降权与 pi 面对齐，容器名/停止/对账复用既有的
`docker_env` 约定，并经宿主 HTTP API 跑过一次容器内最小真实 run；实测与镜像钉版见
[ACP 集成笔记](acp-integration-notes.md) §5，证据在 `docs/evidence/acp-m3-docker-*.txt`。
**M3b（GLINK-ACP-04）已完成**：ACP 族的 E2B 执行面可用——先在真实沙箱完成双向 stdio
帧完整性实测（保序/字节一致/LF 分帧/64 KiB–1 MiB 大帧/`close_stdin`，正面），再用独立
dev 模板 `glink-acp-worker`（E2B CLI 构建、ARG 钉版同 M3a）跑通两个 agent 的沙箱内握手与
dsh 的一次最小真实 run（SSE 流式、终态、`report.md` 回收、`sandbox_id` 对账无残留）；
实测与模板事实见 [ACP 集成笔记](acp-integration-notes.md) §6，证据在
`docs/evidence/acp-m3-e2b-*.txt`。
**M4（GLINK-ACP-05）已完成**：claude-code-acp（`@zed-industries/claude-code-acp@0.16.2`）
接入三个执行面的事实面——inproc 握手与一次最小真实 run（流式/终态/report.md/清理只操作
已知 PID）、每 run 凭据形态（运行期 `.claude/settings.json` 的 env 块，CREDENTIAL_FILES
0600）、权限最小白名单（`allow_once,read,search,fetch,edit`；execute 保持拒绝）、镜像与
dev 模板重建（docker 与 e2b 各加 claude 双包钉版层 + 无模型握手自检）；docker/e2b 面的
真实模型 run 因无 API key 属预期阻塞，详见 [ACP 集成笔记](acp-integration-notes.md) §7
与 `docs/evidence/acp-m4-claude-*.txt`。
**M5（GLINK-ACP-06）已完成**（2026-09-25）：opencode（`opencode-ai@1.18.32`，入口
`opencode acp`）接入——inproc 握手与一次最小真实 run（53 事件/19.8s，report.md 336 bytes）、
每 run `auth.json` 只读复制（OAuth `access`/`refresh` 值并入 redactor，4 条）、
状态目录落点实测、权限形态（真实 run 0 请求，不启用白名单）、镜像与 dev 模板加
opencode 钉版层并完成容器/沙箱握手；docker 面另用刷新后的凭据副本跑通一次最小真实 run
（只读挂载使刷新写回不可用，有效期内无需它）。详见 [ACP 集成笔记](acp-integration-notes.md) §8
与 `docs/evidence/acp-m5-opencode-*.txt`。**M6（GLINK-ACP-08）已完成**（2026-09-25）：
claude 适配器迁到后继包 `@agentclientprotocol/claude-agent-acp@0.81.2`（入口
`claude-agent-acp`）——inproc 握手与能力矩阵重新实测、settings env 凭据形态在新版成立
（不再需要 `CLAUDE_CODE_EXECUTABLE` 绕行，该通道仍可用）、两次 inproc 最小真实 run
（原生通道与旧通道各一次）通过；镜像与 dev 模板换后继钉版层并完成容器/沙箱握手；
codex 侧旧包引用收尾清零。详见 [ACP 集成笔记](acp-integration-notes.md) §9 与
`docs/evidence/acp-m6-migration-*.txt`。
**M7（GLINK-ACP-09）已完成**（2026-09-25）：ACP 真实续接——追问按能力矩阵走
`session/resume`（优先）或 `session/load`（回放帧计数后丢弃，不重复投影），追问 run 从父
run 继承 agent 状态根（有界、拒绝链接、凭据目标排除后重渲染、继承值并入 redactor），准入
从“一律拒绝”改为按「同 harness + 已结束 + 最新轮次 + 有 sessionId + 声明续接能力」受理
（e2b 面因状态随沙箱销毁维持拒绝），续接沿用会话工作区；claude-agent-acp 与 opencode 各
完成一次 inproc「首轮+追问」真实续接。详见 [ACP 集成笔记](acp-integration-notes.md) §10 与
`docs/evidence/acp-m7-resume-*.txt`。
**M8（GLINK-ACP-13）已完成**（2026-09-25）：e2b 面续接——沙箱销毁前有界回收 ACP 状态根、
追问灌回新沙箱后续接（新沙箱/同 sessionId/代号回环由 dsh 真实冒烟判定），e2b 追问准入在原
四条件上增加「父状态根回收成功」，其余维持拒绝；dsh 与 codex-acp 各完成一次 inproc
「首轮+追问」真实续接。详见 [ACP 集成笔记](acp-integration-notes.md) §14 与
`docs/evidence/acp-m8-resume-*.txt`。
2026-10-01 前宿主默认 harness 为 `pi`；自 2026-10-01 起默认改为 `dsh`（dsh 为主力接入方案），
pi/codex/opencode 作为扩展包经显式 `GLINK_HARNESS` 选择保留；执行面已覆盖 inproc / docker / e2b；
「`kernel/` 泛化为 harness 适配层」已落地为**族接口**：`kernel/harness.py` 的
`HarnessFamily`（职责 (a)-(f)：准入白名单与凭据、追问门禁与续接种子、runtime 渲染与
projector、adapter 组装、stop 词汇归一化、子代理增强与运行中事实）加 `harness_pi.py` /
`harness_acp.py` 两个实现，`RunManager.__init__` 经 `select_family` 唯一选择；runner 保留
锁/相位、finalize 顺序控制与终帧提交（见架构 §4「族接口」）。§3 的「SDK 直连族」未来实现点
即 `HarnessFamily` 的第三个实现。
**GLINK-ARCH-6（2026-09-27，§1–§3 已按新模型重写）定下方法论与 harness 的最后一层解耦**：
run 的系统提示与 agent 环境由三层内容组成——工作约定 / 方法论蓝图 / harness 包；前两层
harness 无关、各自唯一，harness 的差异全部收进 `harnesses/<pack>/`。选择改为包名
（`GLINK_HARNESS=pi|dsh|codex|opencode`），族由包清单的 `family` 推导；云端保留
pi、dsh、codex（codex-acp）、opencode（均开源），**Claude（claude-agent-acp + claude CLI）
从产品中移除**——M4/M6 章节保留为历史实测记录（见
[ACP 集成笔记](acp-integration-notes.md)）。
本文是该方向的事实源：实现与本文出现偏差时回写本文，而不是让代码与文档各自漂移。

## 1. 决定与定位

* **目标职责**：glink 独立提供通用执行 API、harness 适配、E2B 传输、会话、事件、取消与回收，
  并拥有执行终态和资源台账的持久化。调用方维护产品身份、业务关联、订阅与成果流程；核心
  接口不引入具体产品模型，也不要求调用方使用特定数据库。
* **保留原生 harness**：Agent loop、工具、上下文管理、原生会话及特有扩展留给 pi/dsh 等，
  公共层只归一化协议可证明的执行事实；内核执行结束不等于业务成果完成或资源已释放。
* **云端部署由运营方选定 harness**：不增加按 run 任意切换的产品入口。切换仍围绕包清单与
  包内容；glink 负责通用执行事件投影，客户端与应用不依赖某个内核的私有协议。
* **云端保留的 harness：pi、dsh（deepseek-harness，原生 ACP server）、codex（codex-acp）、
  opencode**，四者均开源。**Claude（claude-agent-acp + claude CLI）已从产品中移除**
  （2026-09-27 用户决定）：开源性差（闭源 CLI），云端只保留开源 harness；
  移除范围含镜像层、代码分支、测试与现行文档描述，M4/M6 章节只作历史实测证据保留。
* **方法论以蓝图（blueprint）为唯一真源**：工作约定（`methodology/work-agreement.md`）与
  蓝图（`methodology/blueprints/*.md`）是 harness 无关的合同；pi 的 `/deepresearch` prompt
  模板不再作为独立的方法论副本存在（`/<蓝图名> <题目>` 由宿主统一解释为蓝图请求，见 §3.3）。
* **扩展机制按 harness 保留**：pi 通过 packages/extensions/agents/skills 集成 pi-subagents、
  pi-web-access、前台守卫与 researcher 子代理；dsh/codex/opencode 有各自的扩展方式
  （skills/配置/MCP 等）。统一的只是方法论与产品契约，**不是**扩展机制。
* **保留的产品底线**：工作约定里「**引用某个来源前先打开它**，确认它确实支撑你要写的那句话」
  必须保留（2026-09-24 A/B 实测：删掉这句核实质量显著下降）。
* 目标候选（未来）：一切 ACP 兼容开源 agent（Gemini CLI、Kimi 等）。
* **执行不变量**：事件词汇（`events.py`）、终态提交与资源台账由 glink 负责，不为特定应用
  转移权威 owner。通用契约变更须同步生产方、消费方、测试和文档；调用方的业务完成不覆盖
  执行终态，凭据、授权、脱敏与可靠清理义务继续保留。
* **明确不做**：
  * 不做本地客户端模式、不做按 run 选择 harness、本任务不做真实模型/E2B 调用。
  * 不为尚无消费者的 harness 预建空适配器或抽象框架（工程约束：不预建无消费者的抽象）；
    抽象随第一个真实 harness（ACP 族）的接入而产生。
  * 不 fork / patch / vendor 任何 harness；一切接入走对方官方文档化的协议或扩展点。
  * 不以终端文本解析为接入机制（coder/agentapi 的 PTY 屏幕解析路线与本仓库
    「前端不解析终端文本」的底线冲突，排除）。
  * M1 不引入真实模型调用；合成验证先行，真实冒烟单独授权。

## 2. 现状接缝（为什么可行）

宿主中 harness 专有的部分全部集中在 `kernel/` 与包目录，边界清晰：

| 已 harness 无关（不动） | harness 专有（进 `harnesses/<pack>/` 或其族） | 包/族的形态 |
|---|---|---|
| 产品事件词汇、EventBus、RunStore（SQLite）、SSE 重放 | `kernel/projection.py`（pi 事件 → 产品事件） | 每族一个 projector |
| 租户、准入、资源台账、数据根所有权 | `kernel/agent_env.py`（渲染 models/auth/settings、argv、每 run 配置目录） | pi 包自带 `settings.json`/`extensions/`/`agents/`；ACP 包以 `home/` 覆盖层 + 每 run 凭据注入承载 |
| `redaction.py` 流式脱敏（作用于文本） | `kernel/rpc.py` + `framing.py`（pi 双向 RPC） | 每族一个协议客户端；LF 分帧复用 |
| report.md 产物契约（prompt 约束） | 工作约定与蓝图 | 宿主级内容（`methodology/`），不再放进包 |
| `ExecutionAdapter` 契约（start / deliver_input / wait / cancel / recover_artifacts / close / identity） | 斜杠命令解释、stopReason 词汇、续接能力声明 | 斜杠命令由宿主统一解释（§3.3）；每包声明自己的续接/会话选项 |

执行面（inproc / docker / e2b）与 harness 是**正交两维**（S2 起在代码里也是两个独立抽象，
见 §3.5 与 [架构](architecture.md) §2）：`Sandbox`（`kernel/sandbox.py`：inproc / docker / e2b）
只管资源与传输，`HarnessFamily`（pi / acp）只管 launch、协议驱动、终态词汇、续接、子代理增强与
usage；适配器按 `harness.driver(sandbox.spawn(harness.launch(spec)))` 组合，实现数量是
「N 个 harness 族 + 3 个 sandbox」，不是 N×3 的矩阵，也没有 `(harness, executor)` 字符串分派。

## 3. 当前三层内容与 harness 包

本节描述当前配置及系统提示合同。目标将研究约定和蓝图作为服务能力包、允许应用提供其成果
契约，核心执行 API 不要求某个产品的 `report.md` 格式；当前合同在对应消费者改造验收前仍有效。
引用前核实原文的研究底线继续保留。

当前 run 的系统提示与 agent 环境由三层内容组成，各有唯一归属：

| 层 | 内容 | 归属 | 与 harness 的关系 |
|---|---|---|---|
| 工作约定（产品契约） | 工作区、`report.md` 交付、引用某个来源前先打开、外部内容是数据、追问增量修改 | `methodology/work-agreement.md` | harness 无关；不得出现任何 harness 专有工具名 |
| 方法论 | 蓝图（阶段、交付契约、检查点、正文） | `methodology/blueprints/*.md` | harness 无关；**唯一真源** |
| Harness 包 | 该 harness 如何用自己的工具/扩展实现契约（能力提示）+ 扩展本体 | `harnesses/<pack>/` | harness 专有 |

系统提示的组成顺序**对所有族一致**：工作约定 → 包能力提示（若有）→ 蓝图渲染（若有）。
pi 面走 `--append-system-prompt`，ACP 族走 `session/prompt` 的首个文本块——通道不变，
顺序是新的合同。

### 3.1 harness 包目录与清单

```
harnesses/<pack>/
├─ harness.toml        # 清单：包的唯一选择依据（fail-closed 解析，未知键拒绝）
├─ capabilities.md     # 可选：该 harness 的能力提示，拼入系统提示
├─ home/               # ACP 包的扩展落点：每 run HOME 的覆盖层（pi 包没有这一层）
└─ …                   # pi 包：settings.json / extensions/ / agents/ / skills/
```

清单格式 `format = "glink-harness/v2"`（GLINK-ARCH P3 起；解析全有或全无，任何层级的未知键即拒绝）。
**v1 清单不再可读**（[ADR 0012](adr/0012-pack-manifest-v2-single-source.md)）：`format = "glink-harness/v1"` 启动即拒绝（「旧格式不再可读，不做迁移」），
v1 时期的顶层 `command`/`package`/`version`/`session_config`/`permission_allow`/`subagent_dispatch`
全部移入下表的分组，没有兼容读取。

| 字段 | 出现于 | 规则 |
|---|---|---|
| `format` | 所有包 | 固定 `"glink-harness/v2"`；其他取值（含 v1）拒绝 |
| `name` | 所有包 | 非空 slug，必须等于目录名（标识与目录不允许漂移） |
| `family` | 所有包 | `pi` 或 `acp`；`select_family` 据此构造族实例 |
| `[entry].command` | 所有包（必填） | argv 数组（非空字符串）。ACP：agent 的 argv，直接 exec、不经 shell。pi：只有一项，即镜像内的内核可执行名（宿主拼 `--mode rpc` 等参数；`GLINK_PI_BIN` 缺省取它） |
| `[capabilities].resume` | 所有包（必填 bool） | `false`：准入层对该包的每个追问一律 `session_unavailable`，不读父会话；`true`：继续交给族的运行期事实（ACP 还要看协商出的 `sessionCapabilities.resume`/`loadSession`）。四个随仓库包都声明 `true` |
| `[capabilities].subagent_dispatch` | ACP 包（可选） | `sync`（缺省：派发调用的终态即子代理结果）或 `async`（派发被接受即 completed，子代理继续运行，由 run 终帧收口；dsh 声明）；投影据此判定，不识别 agent 回复措辞 |
| `[capabilities].child_progress` | ACP 包（可选） | 运行中子代理进度源；目前只有 `dsh-session-zstd`（状态根下 `.dsh/sessions` 的私有会话文件，dsh 声明）。未声明 = 该包如实没有进度源，族代码不按包名或目录猜 |
| `[capabilities.session_config]` | ACP 包（可选） | `id → value` 或结构来源表 `{ source = "OPENAI_MODEL" / "OPENAI_REASONING_EFFORT", encoding = "value" / "dsh-model" }`；来源从宿主冻结配置派生，缺值拒绝启动；`session/new` 与续接后应用并核验实际 `currentValue`，未广告选项/值或不一致即 run 失败；`dsh-model` 仅用于主模型，按 dsh 广告的 JSON 路由唯一匹配 |
| `[capabilities].permission_allow` | ACP 包（可选） | ACP option/tool kind 的子集；缺省空 = fail-closed 全拒 |
| `[capabilities].patch_overlays` | ACP 包（可选） | dsh `--patch` launcher 覆盖层（可重复）：run HOME 内的相对路径数组，必须由 home/ overlay 交付对应普通文件（解析期校验，缺失即拒）；launch 按放置后的 HOME 追加 `--patch <HOME>/<相对路径>`，使插件 config 在挂载时同步生效（dsh 声明，DSH-03）。未声明 = 不追加任何旗标 |
| `[image].target` | 所有包（必填） | 承载该包的 worker 镜像：pi 族只能是 `pi-worker`，ACP 族只能是 `acp-worker` |
| `[image].cpu_count` / `memory_mb` | 所有包（可选，缺省 2 / 4096） | E2B 模板资源下限；同一镜像取成员包的最大值 |
| `[[image.packages]]` | 所有包（至少一条） | `arg`（Dockerfile ARG 名，`*_VERSION`）、`package`（npm 包名）、`version`（精确版本）、`role`。pi 族 role ∈ `kernel`（恰好一个）/`pi-package`（预装进 `/opt/pi-packages`，并写入 `settings.json`）；ACP 族 role = `agent` |

* ACP 专属能力（`subagent_dispatch`/`child_progress`/`session_config`/`permission_allow`/
  `patch_overlays`）出现在
  pi 包即拒绝（这些键对 pi 族没有消费者）。
* **族代码只读能力声明**：`config.pack`（`HarnessPack` 对象本身）是运行期唯一入口，`HostConfig`
  不再有从清单抄来的 `acp_command`/`acp_session_config`/`acp_permission_allow`/
  `acp_subagent_dispatch`/`pack_capabilities`/`pack_home` 字段；容器面预装包名单来自
  `pack.prebaked_pi_packages()`，不再有代码内的第二份列表。
* **镜像构建产物由清单生成并入库**：`python3 scripts/generate-pack-artifacts.py` 改写四个
  Dockerfile 的 `ARG <NAME>_VERSION=` 行、`docker/e2b/templates.json`（每个 worker 镜像的
  E2B 别名、Dockerfile、CPU/内存、构建参数与构建命令）以及 pi 包 `settings.json` 的
  `packages`；`--check` 只比对。规则：已声明的 ARG 在所属镜像的每个 Dockerfile 里恰好
  出现一次；Dockerfile 中未被任何清单声明的 `ARG *_VERSION` 拒绝（不许手写第二处真源）；
  同一镜像内同名 ARG 声明冲突拒绝。`tests/test_repository_boundaries.py` 检查生成产物与
  清单一致（不再逐字比对三处）。升版流程：只改 `harness.toml` → 运行生成脚本 → 提交产物；
  生成与校验都不构建镜像、不访问网络。Node 基础镜像 tag 与镜像源 ARG 不是包属性，仍手写。
* `capabilities.md` 只写**已知成立**的事实（例如 pi 包的子代理工具面）；不确定的 agent 可以
  不提供该文件，宁缺不臆造工具名。解析时读入 `HarnessPack.prompt`，宿主生命周期内字节稳定。
* `home/` 覆盖层是仓库内容：渲染每 run HOME 时**先铺覆盖层、再按现有规则注入凭据**
  （凭据目标优先、不被覆盖层覆盖）。dsh 的覆盖层自 GLINK-DSH-02 起承载每 run 的 ACP
  profile 清单（把 `web.fetchProvider` 指向 out-of-tree 的 Jina fetch bundle；bundle 代码
  在镜像/模板构建期装进 dsh 安装，覆盖层只带清单），见
  [ACP 集成笔记](acp-integration-notes.md) §19。dsh 的 ACP profile 用户层还负责产品身份：
  `system-prompt.includeHarnessIdentity = false`，`personaPrefix` 为 Glink 的研究、分析与文档
  助手定位，保留 `personaSuffix` 的工作目录提示及上游工具指导/runtime context；方法论仍只
  由 `methodology/` 提供。显式禁用 `session-telemetry-otel`（不上送会话历史到上游 collector）
  与 `skill-badge`（不注入官方 badge）。这些配置随新 run HOME 渲染，不修改 dsh 源码、模型/
  检索路由或请求归属头，也不改写既有会话状态/热沙箱；追问继承旧状态时可能仍沿用旧 profile。
  回归见 `tests/test_dsh_jina_bundle.py`（文本配置与宿主渲染，不代表真实 dsh/E2B 已验收）。
  codex/opencode 覆盖层仍为空，机制由回归证明。
* 包目录与清单在启动时读取并冻结（`GLINK_HARNESSES_DIR` 可覆盖，默认 `<repo>/harnesses`）；
  目录缺失、选中包不存在或清单不合契约都拒绝启动——这是运营方的部署选择，不是按 run 的请求参数。

### 3.2 选择与族推导

* `GLINK_HARNESS=<pack>`（`dsh` | `pi` | `codex` | `opencode`，默认 `dsh`，2026-10-01 起；
  此前默认 `pi`）：一次部署一个，
  启动冻结，没有 per-run 参数与前端开关。
* 族由清单的 `family` 推导，`select_family` 按清单构造族实例（`harness_pi.py` /
  `harness_acp.py`）；每个 run 的族取自其记录的 `RunSpec.family`（§3.4），runner 不读
  harness 字符串做分支，也不导入具体族（导入白名单见 `tests/test_repository_boundaries.py`）。
* **旧变量不再读取**（P3 起删除专用拒绝分支，不做兼容映射）：`GLINK_HARNESS=acp` 只是一个
  不存在的包名，按「不可用」拒绝并列出可用包；`GLINK_ACP_COMMAND` /
  `GLINK_ACP_SESSION_CONFIG` / `GLINK_ACP_PERMISSION_ALLOW` 不再是配置面，设置了也不会
  覆盖包清单（对应写法是 `[entry].command`、`[capabilities.session_config]`、
  `[capabilities].permission_allow`）。

* `GLINK_ACP_CREDENTIAL_FILES` 与 `GLINK_ACP_ENV` 是**部署秘密**，仍留在环境配置：
  只有选中包的族是 ACP 时才生效，非 ACP 族下设置即拒绝启动（沿用现有非空即报错的语义）。

### 3.3 斜杠命令（宿主统一解释，harness 无关）

* `/<蓝图名> <题目>` ≡ API `blueprint=<蓝图名>` + `prompt=<题目>`：宿主在准入层解析，
  不再依赖 pi 的 prompt 模板目录，也不再让 pi 自己展开模板。
* 其余任何以 `/` 开头的输入一律 `400 unsupported_command`，包括 `/<蓝图名> /run --bg`
  这类题目仍以 `/` 开头的情形（重写后的 prompt 绝不能带着 `/` 进入内核）；所有族同一判定。
* 判定位置不变：准入层，读父会话 / 写 run 目录 / 拉起执行面之前；追问同样适用。

### 3.4 run 记录与追问

* 准入时解析一次 `RunSpec{family, pack, executor, blueprint, credential_origin}`，作为
  run 记录的 `"spec"` 对象与 run id/owner 在同一数据库事务内写入（[架构](architecture.md) §2）；
  此后族、执行面与追问门禁只读记录里的 spec。
* 追问：`spec.family` 或 `spec.pack` 与本 run 不同即 `session_unavailable`（fail-closed）。
* **旧记录不再可读**（S2 重新设计，无迁移）：顶层 `harness` / `harness_pack` / `executor`
  形态的旧 meta 按不可读记录处理——不列出、不能追问，资源台账按损坏记录计入全局持有。

### 3.5 适配族

```
HarnessFamily（kernel/harness.py + harness_pi.py / harness_acp.py）
│  launch(spec, sandbox) → Launch；driver(spec, sandbox=, launch=) → ExecutionAdapter；
│  build_adapter = driver(sandbox.spawn(launch(spec)))，族内没有按执行面的 plan 方法
├─ pi 族   — rpc（双向 stdio NDJSON，inproc/docker/e2b 三面共用同一份 PiRpcClient）；
│            旧 e2b `--mode json` 单向流机制已随 GLINK-ARCH-3 删除；
│            作为其它族的参照系与回归基线
├─ ACP 族  — 一次 ACP client（JSON-RPC over stdio）覆盖：dsh 原生 acp server、
│            codex-acp（@agentclientprotocol/codex-acp）、opencode（`opencode acp`）；
│            适配器进程与 agent 同容器/沙箱，stdio 桥接
└─ SDK 直连族（可选，暂不建）— 未来落地即 HarnessFamily 的第三个实现

Sandbox（kernel/sandbox.py，open_sandbox(spec.executor) 选出）
├─ InprocSandbox — 本机子进程，passthrough 环境，路径即宿主路径
├─ DockerSandbox — docker run -i；容器名为 planned_identity，close = docker rm -f
└─ E2BSandbox    — 云沙箱后台命令（stdin=True）；沙箱 id 经 on_identity 回报；
                   远程文件交付由族 driver 的传输器完成（PiSandboxTransfer / AcpSandboxTransfer）
```

扩展机制的差异**保留在包里**：pi 走 `settings.json` 的 packages、`extensions/`、`agents/`
与 `skills/`（承载 pi-subagents、pi-web-access、前台守卫与 researcher 子代理）；ACP 包以
自己的配置形态与 `home/` 覆盖层承载（dsh/codex/opencode 当前为空，按实测再填）。
统一的只是工作约定、方法论和产品契约。

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
| 回合末（本轮 `agent_message_chunk` 的累计） | `assistant_message` | ACP 没有 `message_end` 一类的整帧：投影层把本轮已释放（流式脱敏后）的 delta 装配为权威正文帧，在 `turn_finished` 前落地；截断纪律与 pi 族相同（`truncated: true` 以权威副本为准并标记交付不完整，不保留更长的流式拼接），空回合不发帧。帧覆盖整回合（正文段可能已被工具/plan 项夹断），带 `turnScoped: true`：前端据此合并替换本轮全部文本块而非 pi 族逐 message 的替换末块（契约 §2.5） |
| `agent_thought_chunk` | `thinking_delta` | 思考折叠块 |
| `tool_call` / `tool_call_update`（status: pending→in_progress→completed/failed） | `tool_started` / `tool_updated` / `tool_finished` | kind/rawInput/rawOutput 入工具卡片 |
| `plan` | `run_stage` | 计划条目作为阶段行呈现 |
| `usage_update` | `usage_reported` | **语义换算**：ACP 是「上下文窗口占用 + 累计成本」视角，产品语义是「逐请求累计求和」；投影层如实换算并在 run 记录标注口径差异，不伪造逐请求 token 数 |
| `available_commands_update` | `kernel_status`（警告） | 见 §4.3 门禁 |
| `session/prompt` 返回 `stopReason` | 终态判定输入 | `end_turn`→正常；`max_tokens`≈ pi 的 `length`（finished 但标注交付不完整）；`refusal`→failed/model_error 类；`cancelled`→取消路径 |
| `session/request_permission` | 不投影为交互，自动应答（§4.2） | 应答结果记 `kernel_status` |

run 生命周期、终帧即终态、提交先于发布等全部沿用架构 §3/§4，不因 harness 改变。

### 4.2 权限自动应答（无人值守前提）

沙箱/无人场景没有人应答权限询问。默认 **fail-closed 全拒**；拒绝本身必须进入事件流
（`kernel_status`），不允许静默。M2 实测：dsh 0.1.5-rc.3 与 codex-acp 0.16.0 在
workspace-write 会话模式下跑完两个最小真实研究 run，均未发出权限请求，因此**保持默认、
不启用白名单**；配置级门控（当时是 `GLINK_ACP_PERMISSION_ALLOW`，GLINK-ARCH-6 起
是包清单的 `permission_allow`）（option kind，可选 tool kind）
已实现并默认空集。M5 接入 opencode 时反向确认了"先实测再决定"：opencode 的真实 run 在
`webfetch`/`apply_patch` 上 0 条请求，**不为它启用白名单**（默认 fail-closed 不变），
源码里请求自带 `kind` 的形态与触发面记在集成笔记 §8.4。M4 曾为已移除的闭源适配器启用最小集合
`allow_once,read,search,fetch,edit`（决定与证据见集成笔记 §7.4）；该 agent 已随
GLINK-ARCH-6 从产品移除，**现有包的 `permission_allow` 全部为空**。“请求不带 `kind` 时按
同一 `toolCallId` 的 `tool_call` 通知解析”的兼容分支保留（集成笔记 §9.4 的重测：该 agent 后来
的版本已自带 `kind`，`toolKindSource` 记录来源），对任何省略 `kind` 的 ACP agent 都成立。

### 4.3 准入与门禁

斜杠命令自 GLINK-ARCH-6 起由**宿主统一解释**（harness 无关，见 §3.3）：`/<蓝图名> <题目>`
等价于 API 的 `blueprint=<蓝图名>` + `prompt=<题目>`，其余以 `/` 开头的输入一律
`400 unsupported_command`。ACP agent 自带 slash 命令（`available_commands_update`）仍不被
宿主路由，门禁因此对所有族一致。M2 实测 codex-acp 确实在 `session/new` 后广播 6 条命令，
准入判定不受其影响。蓝图从 `methodology/blueprints/` 目录推导、未知或畸形即 400；
格式与设计边界见 [会话蓝图模板](blueprint-templates.md)。

### 4.4 续接（resume）

* pi 族：复制会话文件 + `--session`（现状，不动）。
* ACP 族（M7 已实现，实测见集成笔记 §10）：`session/resume`（能力位
  `sessionCapabilities.resume`，不回放）优先；只有 `resume` 缺失而顶层 `loadSession` 为
  true 时用 `session/load`，并把它回放的历史帧计数后丢弃（父 run 已记录的历史不重复投影）。
  resume 依赖 agent 自身状态目录（`~/.codex`、`~/.local/share/opencode`、
  dsh sessions）——每 run HOME 独立渲染，追问时**从父 run 的状态根有界继承**（拒绝链接、
  凭据目标排除后重新渲染、继承值并入子 run 的 redactor），启动新 agent 进程后续接。
* 会话工作区：agent 会话记录自己的 `cwd`，同一会话的每轮共用该工作区（追问 run 复用父
  run 的工作区）；子 run 的产物仍发布到自己的 `runs/<id>/artifacts/`，父 run 的记录与已发布
  产物不变。opencode 的真实追问实测了这个必要性（模型沿用记忆中的绝对路径）。
* 准入：同 harness + 已结束 + 最新轮次 + 有 sessionId + 声明续接能力才受理；能力缺失、
  agent 拒绝或状态继承失败都 fail-closed，绝不以新会话冒充追问。
* e2b 执行面（M8 已实现，实测见集成笔记 §14）：沙箱销毁前把 agent 状态根**有界回收**到宿主
  `runs/<id>/acp-agent/recovered`（沿用继承上限，整份成功才发布，越界/读失败整份拒绝并在
  `artifactFailures` 记录）；追问把继承状态灌回新沙箱的每 run HOME（先 0700 后上传、0600
  加固）后走同一份 `session/resume` 路径，并把会话工作区的 `report.md` 一并灌回；准入在
  M7 四条件上增加「父状态根回收成功」，回收失败/未回收维持拒绝。

### 4.5 凭据与配置渲染

沿用 `agent_env.py` 模式：每 run 渲染独立配置目录，凭据写文件或注入命令级 env，
不进宿主进程环境，全部出站过 `redaction.py`。ACP 族的 M2 实现：
`GLINK_ACP_CREDENTIAL_FILES` 把 `source=dest` 源文件**只读复制**进每 run HOME
（父目录 0700、文件 0600），`GLINK_ACP_ENV` 注入显式 env（不允许覆盖 `HOME`），
注入值抽入该 run 的 redactor。已实测：codex-acp 读 `$HOME/.codex/auth.json`、dsh 读
`$HOME/.dsh/.credentials.yaml`（dsh harness home 默认 `$DSH_HOME` 或 `~/.dsh`）；两者都
尊重每 run `HOME`，真实用户目录未被修改。逐 agent 落点与证据见
[ACP 集成笔记](acp-integration-notes.md) §4.4。Docker 面沿用同一“只读复制进 run HOME”，
再在容器内对每个凭据文件加嵌套只读挂载（HOME 本身可写，agent 要写会话/缓存）；
`GLINK_ACP_ENV` 在 docker 面按名字传给容器，值不进 argv（见集成笔记 §5.2）。E2B 面
没有宿主挂载，改为把同一份 run HOME 上传进沙箱状态根并在沙箱内重新施加 0700/0600，
`HOME` 指向该根、`GLINK_ACP_ENV` 作为命令环境通道（值不进命令行；见集成笔记 §6.3）。
system prompt 的通道也已在 M2 定下：
作为 `session/prompt` 的首个围栏文本块送达（ACP v1 无 system role；见集成笔记 §4.3）。
该通道现在也承载宿主侧、harness 无关的**会话蓝图**：同一份渲染文本对 pi 面走
`--append-system-prompt`、对 ACP 面进同一首块，用户 prompt 始终走自己的通道、不被替代；
格式契约、记录字段与后续增量挂账见 [会话蓝图模板](blueprint-templates.md)。
M5 补上 opencode：凭据就是文件
`~/.local/share/opencode/auth.json`（OAuth `access`/`refresh`，按精确字段名并入 redactor），
每 run 落在 `HOME/.local/share/opencode/auth.json`（0600），docker 面再叠一层嵌套只读挂载、
e2b 面在沙箱内 0600 加固——同一套机制，没有为它新增通道；见集成笔记 §8.3。已移除的
闭源适配器的凭据形态（由 `settings.json` 的 `env` 块派生运行期 settings 再经
CREDENTIAL_FILES 复制）见集成笔记 §7.3/§9.3（历史实测）。

## 5. 开源复用策略

| 项目 | 复用方式 | 约束 |
|---|---|---|
| ACP 适配器（dsh 原生 acp server、 `@agentclientprotocol/codex-acp`、opencode） | 作为**钉版外部依赖**接入（npm 包/二进制），记录版本、来源、license | 版本钉死 + 冒烟 + 每 harness 一份集成笔记（沿用 pi-boundary 纪律）；适配器缺陷不改对方源码，反馈上游或换版本 |
| vibe-kanban（BloopAI，已停摆转社区维护） | **只读借鉴**：`crates/executors` 的 per-agent normalize 实现是事件归一化字典 | 不引入运行时依赖 |
| 官方 SDK（@openai/codex-sdk TS 等） | SDK 直连族的候选（暂不建）；其事件定义作为投影语义参照 | TS SDK 仅参考（Python SDK 才可进程内；随 harness 名单同步维护） |
| VibeKit / OpenHands / terminal-bench / gate4agent | 形态与传输矩阵的参照证据 | 不引入 |
| coder/agentapi | **排除**：PTY 屏幕解析机制违背本仓库事件契约底线 | — |

ACP 客户端本体用 Python 标准库实现（subprocess + json），不新增第三方依赖；
确需新增时论证并记录消费者。

## 6. 里程碑

### 已完成的接入里程碑

下表保留原验收范围，不证明通用 API、可选 UI 或开源准备已经完成。

| 里程碑 | 内容 | 验收 | 状态 |
|---|---|---|---|
| M1（GLINK-ACP-01） | ACP 族 inproc 骨架：ACP stdio JSON-RPC 客户端（复用 LF 分帧）、projector、harness 注册与配置级选择（默认仍为 pi，不动 API/前端）、run 全生命周期接入（受理/流式/取消/终态/产物回收）、fail-closed 权限应答、合成 fake ACP server 测试 | `bash scripts/check.sh` 全绿；合成测试覆盖 §4.1 映射、取消、stopReason 判定、权限拒绝、门禁；现有 pi 三面测试零回归 | 已完成（合成验证；实现口径与待实测项见 [ACP 集成笔记](acp-integration-notes.md)） |
| M2（GLINK-ACP-02） | 真实冒烟：`dsh --profile acp` 与 `codex-acp` 各跑一次真实研究 run（inproc）；system prompt 送达通道；per-agent 凭据注入（每 run HOME/env、0600、并入 redactor）；能力矩阵、usage 口径与权限决策回写 | 真实 run 报告产物 + 集成笔记；`bash scripts/check.sh` 全绿 | **已完成**（2026-09-25）：两侧握手与最小真实 run 均成功，fail-closed 默认无需白名单；实测细节见 [ACP 集成笔记](acp-integration-notes.md) §4，证据 `docs/evidence/acp-m2-*.txt` |
| M3a（GLINK-ACP-03） | Docker 执行面：`docker/Dockerfile.acp-worker`（自包含、ARG 钉版、镜像内自检）；`docker run -i` 双向 stdio 桥接复用 M1 客户端；每 run HOME 与只读凭据挂载；容器名/停止/对账复用 `docker_env` 约定；资源限制与降权与 pi 面对齐；解除 `harness=acp + executor=docker` 的启动拒绝 | `bash scripts/check.sh` 全绿；合成 plan/钉版边界回归；镜像自检 + 一次容器内最小真实 run（流式/终态/report.md/无容器残留） | **已完成**（2026-09-25）：docker 面可用，pi docker 面零回归；实测与钉版见 [ACP 集成笔记](acp-integration-notes.md) §5，证据 `docs/evidence/acp-m3-docker-*.txt` |
| M3b | e2b 执行面：双向 stdio（`stdin=True` + `send_stdin`，SDK 2.x 已具备）帧完整性实测先行；模板钉版；`harness=acp + executor=e2b` 的启动拒绝随之解除 | e2b smoke | **已完成**（2026-09-25）：帧完整性正面、dev 模板 `glink-acp-worker` 构建+自检、两 agent 沙箱内握手、dsh 最小真实 run（report.md 与沙箱按 id 无残留）；实测见 [ACP 集成笔记](acp-integration-notes.md) §6，证据 `docs/evidence/acp-m3-e2b-*.txt` |
| M4（GLINK-ACP-05） | claude-code-acp 接入（`@zed-industries/claude-code-acp@0.16.2`）；能力矩阵补齐；每 run 凭据与首启形态；权限最小白名单；镜像/dev 模板加 claude 双包钉版层 | inproc 握手 + 一次最小真实 run（流式/终态/report.md/清理）；容器与沙箱无模型握手自检；`bash scripts/check.sh` 全绿 | **已完成**（2026-09-25）：inproc 握手与真实 run 通过；docker 镜像重建后三个 agent 容器内握手（codex-acp 无凭据属预期）；e2b dev 模板重建后沙箱握手；docker/e2b 的真实模型 run 无 API key 属预期阻塞。详见 [ACP 集成笔记](acp-integration-notes.md) §7，证据 `docs/evidence/acp-m4-claude-*.txt` |
| M5（GLINK-ACP-06） | opencode 接入（`opencode-ai@1.18.32`，入口 `opencode acp`）：inproc 握手与能力矩阵；每 run `auth.json` 凭据与状态目录；权限形态与决策；镜像/dev 模板加 opencode 钉版层 | inproc 握手 + 一次最小真实 run（流式/终态/report.md/清理）；容器与沙箱无模型握手自检；`bash scripts/check.sh` 全绿 | **已完成**（2026-09-25）：inproc 与 docker 面各一次最小真实 run 通过（均 0 权限请求），凭据/状态目录、镜像重建与容器挂载契约、dev 模板沙箱握手均已实测。详见 [ACP 集成笔记](acp-integration-notes.md) §8，证据 `docs/evidence/acp-m5-opencode-*.txt` |
| M6（GLINK-ACP-08） | claude 适配器迁到后继包 `@agentclientprotocol/claude-agent-acp@0.81.2`（入口 `claude-agent-acp`，旧包 `@zed-industries/claude-code-acp@0.16.2` 被 npm 标记 renamed）；codex 旧包引用收尾清零；镜像/dev 模板换后继钉版层；集成笔记与 plan 回写 | inproc 握手与能力矩阵对照（逐条记差异）；settings env 凭据形态结论；≤2 次 inproc 最小真实 run；镜像与 dev 模板重建 + 容器/沙箱握手；`bash scripts/check.sh` 全绿；钉版边界测试同步（原义务/新义务注释） | **已完成**（2026-09-25）：两次 inproc 真实 run 通过（原生通道与 `CLAUDE_CODE_EXECUTABLE` 通道）、settings env 派生在新版成立、镜像/dev 模板重建后握手通过、`git grep` 旧包引用清零（历史文档条目逐条说明）。详见 [ACP 集成笔记](acp-integration-notes.md) §9，证据 `docs/evidence/acp-m6-migration-*.txt` |
| M7（GLINK-ACP-09） | ACP `session/resume` 真实续接：能力矩阵决定 `session/resume`/`session/load`；追问 run 继承父 run 的 agent 状态根；准入按「同 harness + 已结束 + 最新轮次 + 有 sessionId + 声明续接能力」放开，其余拒绝（e2b 维持拒绝）；续接沿用会话工作区 | claude-agent-acp 与 opencode 各一次 inproc「首轮+追问」真实续接（题面可判定连续性）；合成回归含能力缺失/load 回放抑制/继承失败/状态缺失/异 harness/异租户拒绝与继承值脱敏；`bash scripts/check.sh` 全绿 | **已完成**（2026-09-25）：两次 inproc 真实续接通过（追问持有首轮上下文、report.md 回收、状态继承与清理按已知 PID），e2b 面追问显式拒绝等待状态根回收。详见 [ACP 集成笔记](acp-integration-notes.md) §10，证据 `docs/evidence/acp-m7-resume-*.txt` |
| M8（GLINK-ACP-13） | e2b 面续接：沙箱销毁前有界回收 ACP 状态根到宿主、追问灌回新沙箱每 run HOME（先 0700 后上传）后走 ACP-09 resume 路径，回收/灌回失败 fail-closed；e2b 追问准入在 M7 四条件上增加「父状态根回收成功」；dsh/codex-acp 真实 resume、e2b 选能力具备且凭据可注入者跨沙箱续接 | dsh 与 codex-acp 各一次 inproc「首轮+追问」；e2b 一次跨沙箱「首轮+追问」（新沙箱/同 sessionId/代号回环，按已知 PID 与 sandbox id 清理）；合成回归含回收边界/灌回顺序/准入矩阵/fake SDK 全链路；`bash scripts/check.sh` 全绿；pi 面与既有 ACP 零回归 | **已完成**（2026-09-25）：两次 inproc 真实续接（dsh/codex-acp 均 `session/resume` 且同 sessionId）与一次 e2b 跨沙箱续接（新沙箱、两侧状态根回收、代号回环、沙箱按 id 对账无残留）通过。详见 [ACP 集成笔记](acp-integration-notes.md) §14，证据 `docs/evidence/acp-m8-resume-*.txt` |

每个里程碑独立验收、独立提交边界；里程碑之间允许因实测结论回写本文。

### 后续服务化里程碑（已确认方向）

| 顺序 | 范围与交付 | 完成条件 | 状态 |
|---|---|---|---|
| R1：通用执行契约 | 明确执行、会话、事件、产物、取消与用量接口，区分服务执行事实与应用业务状态；保留原生适配和现有持久化 | 不引入特定产品字段、账号库或业务数据库；现有宿主仍可用，原生扩展与会话能力不被删减 | **已完成**（R1-5，2026-10-02）：HTTP 面语义固化在 [exec-contract.md](exec-contract.md)（受理与禁用字段、拒绝映射、SSE 续传与 429、终帧集合、report/deliverables、取消两态），`tests/test_exec_contract.py` 逐条钉住；只固化现状，无产品字段 |
| R2：服务与参考客户端解耦 | 服务可不依赖 `web/` 运行，参考 UI 只消费通用 API；解耦产品账号、邀请和固定报告格式前核对消费者 | 独立脚本客户端可通过接口完成执行与重放，不导入产品代码；调用方授权、产物访问和数据根安全继续有效，不以去 UI 为由删除事件历史 | **已完成**（2026-10-05，同日经对抗审查加固）：执行核心组装拆入 `service.py`（`build_execution_service`：所有权→身份→存储→启动对账、失败回滚、单一拆除路径；导入方向由静态规则 + 干净子进程传递闭包断言卡住，不导入 HTTP/UI 面），`app.create_server` 只加 HTTP 面；`close` 按组装逆序六步拆除（在途 run 取消并等待终态提交先于锁释放、`runs.db` 连接关闭与 WAL 回收、per-step 异常保护）；`scripts/glink-api-client.py` 纯标准库参考客户端经接口完成创建/直播/终态/重放（错误体非 JSON 容忍、多字节帧跨 chunk 切分、AST 标准库门禁），`tests/test_reference_client.py` 断言源码不导入产品代码、`tests/test_execution_service.py` 证明无 HTTP 直驱执行。未做（须先核对消费者）：产品账号、邀请与固定报告格式的解耦 |
| R3：可靠接口验收 | 补齐创建幂等、稳定标识与重连契约，覆盖服务写库失败、重启、取消竞态与未知资源清理；服务自身保存执行终态与台账 | 创建响应丢失不重复执行；终态与事件原子可见，迟到事件不覆盖终态，未释放资源不被遗忘；取得授权后完成 E2B 真实验收 | **已完成（除 E2B 真实验收）**（2026-10-05）：创建幂等以 `Idempotency-Key` 头落地——作用域 `(tenant, user)`（local 单 owner），digest 盖原始请求字段（prompt/parentRunId/blueprint，`/` 重写之前；命令形式与字段形式是不同请求）；同键同请求重放**先于一切准入语义**短路（201 + run 当前视图、同 `runId`、不重执行、不被当下蓝图/凭据/配额重判，运行中重试赢过 run_busy，重启后重放成立；重放视图反映重放时刻状态，非首次响应快照），prepare 窗口内的并发孪生竞速败者同样收口为重放；同键异请求 409 `idempotency_key_conflict` 且体不携带占用者事实；键行与 run 行同事务（写库失败不留键、键指向不可读 run 时 fail-closed），owner 作用域唯一索引兜底并发（宁拒不双执行）；同版本旧库打开即补 `idempotency_keys` 表。稳定标识（`runId` 不可变）、重连契约（SSE `Last-Event-ID`/`?after` 续传）、终态与事件原子可见（`commit_terminal` 单事务 + `synchronous=FULL`）、迟到事件不覆盖终态（终帧后拒写）、未释放资源不被遗忘（资源台账 + 对账/裁决）由既有机制保持且各有既有测试，本轮无改动。**E2B 真实验收未做**（须授权）。契约固化在 [exec-contract.md](exec-contract.md) §1.4，钉住测试见其 §5 对应表 |
| R4：可复用与开源准备 | 提供不含真实秘密和产品依赖的配置示例、部署及 API 文档、最小客户端示例；核对本仓库与依赖许可、上游归属、公开材料范围 | 独立环境可按文档接入；受管数据根不作为示例 fixture，镜像无固化 key；无应用源码/数据库依赖，发布范围经授权后才能开源 | 待实施 |

每步须可独立交付，不把接入某个应用作为完成条件，也不把退出 SQLite 当作服务化目标。存储变更
只因真实通用部署需求单独决定；多宿主、共享调度与 BYOK 不作为本轮前置工作。接口扩展、账号
解耦及发布形态需先明确兼容与消费者影响；方向确认不连带授权代码改造、真实模型调用、E2B 构建、
运行数据迁移、许可变更或公开发布。

### M1 落地偏差（相对 §3–§4；详细口径见 [ACP 集成笔记](acp-integration-notes.md)）

* **配置开关**：`GLINK_HARNESS=pi|acp`（默认 `pi`）+ `GLINK_ACP_COMMAND`；只在配置层，
  无 API/前端参数，`public_view()` 不暴露 harness。M1 落地时 ACP 只接 `inproc`；M3a 起
  （GLINK-ARCH-6 起由包清单取代这两个变量，见 §3.2。）
  `docker` 组合可用（镜像走独立的 `LOCAL_WEB_ACP_WORKER_IMAGE`，见 §5），`e2b` 组合继续在
  启动即拒绝（M3b）；M3b 起三个执行面（inproc / docker / e2b）都可用。
* **续接（§4.4）**：M1 不实现 ACP 的 `session/resume`；ACP run 不落会话文件，追问在准入层以
  `session_unavailable` 拒绝，适配器也不会以新会话冒充追问。**M7 已替代该决定**：能力矩阵
  声明续接通道的 agent 按 §4.4 受理，e2b 面继续拒绝（状态根随沙箱销毁）。
* **用量（§4.1）**：`usage_update` 保留 ACP 的「上下文窗口占用 + 累计成本」视角，投影为
  `used/size + cost` 并标注 `basis: "acp_usage_update"`；不做逐请求 token 求和，终态记录采用
  最后一个快照（替换而非累加）。
* **状态目录（§4.4/§4.5）**：inproc 面每 run 渲染 `runs/<id>/acp-agent/home` 作为子进程
  `HOME`，不注入任何宿主凭据；各 agent 是否还认专用变量（`CODEX_HOME` 等）与凭据注入点
  逐 harness 实测属 M2。
* **未投影的更新**：`user_message_chunk`、`current_mode_update`、`session_info_update` 等
  §4.1 未定义的变体不猜语义，按类型一次性记 `kernel_status` 提示；`max_turn_requests` 以原词
  进入 `stopReason`（前端目前只对 `length` 提示「交付不完整」）。
* **回合边界（§4.1 补充）**：ACP 没有 turn 更新，适配器在 `session/prompt` 前后补发合成的
  回合边界，投影为 `turn_started`/`turn_finished`，让流式文本/思考块按产品既有语义关闭与
  渲染；产品事件词表与前端契约不变。

## 7. 风险与纪律

* **ACP 能力参差**：`loadSession`/`resume`/`usage_update` 均为可选能力——以实测矩阵为准，
  能力缺失走显式降级，不伪造。
* **dsh 处于 developer preview**（官方明示 breaking changes）：钉版 + 集成笔记，同 pi 纪律。
* **事件语义鸿沟**（usage/stopReason/工具状态机）：产品事件语义由宿主定义，projector 换算；
  换算不了的如实标注，不塞进近似语义。
* **权限无人应答**：fail-closed 默认；任何放行白名单需逐 harness 论证并记录。
* **升级路径**：每个外部适配器/harness 的版本固定点与升级冒烟步骤，写进对应集成笔记，
  参照 [pi 边界](pi-boundary.md) §3 的三处固定模式。
* **E2B 传输与模板**：沙箱 stdio 是 `send_stdin` + `CommandHandle` 流（主机不可见容器级
  只读挂载），且 E2B 构建解析会改写 Dockerfile 文本（CMD 拼接、`\n` 转义）——模板必须
  CLI 构建 + 真实沙箱自检，帧完整性已有真实沙箱实测（集成笔记 §6.1/§6.2）。E2B 这条 SDK
  路径只暴露 lifetime，没有 cpus/memory 参数，宿主只声明它真的要求过的边界。
