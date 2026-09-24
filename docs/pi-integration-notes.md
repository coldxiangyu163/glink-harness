# pi 集成实测笔记（0.86.1）

本文件记录**实测得到**的 pi 行为事实。每条都有可复现的命令或证据来源；架构决策引用这里的结论，而不是推测。

测试基线：宿主 pi `0.86.1`（升级演练到 `0.87.0`）、宿主开发 Node `v24.16.0`、Docker / E2B 镜像与沙箱 Node `v24.21.0`（`node:24.21.0-bookworm-slim` 精确 tag）、宿主 Python 3.12、上游 OpenRouter（`https://openrouter.ai/api/v1`，模型 `openai/gpt-5.6-sol`）。

## 1. `models.json` 的环境变量插值范围

| 字段 | 支持 `$VAR` 插值 | 证据 |
|---|---|---|
| `apiKey` | ✅ 支持 | `docs/models.md`「Value Resolution」明确列出 apiKey 与 headers |
| `headers` | ✅ 支持 | 同上 |
| `baseUrl` | ❌ 不支持 | 设 `"baseUrl": "$OPENAI_BASE_URL"` 后 `pi --list-models` 能列出模型（不校验 URL），实际请求报 `Invalid URL` |
| `models[].id` | ❌ 不支持 | `pi --list-models` 原样打印 `$OPENAI_MODEL` 字样 |

**结论**：宿主编译期无法把 endpoint 与模型 id 交给 pi 自己插值，必须由宿主在运行期渲染 `models.json`（见 `host/pi_glink/kernel/agent_env.py`）。密钥**不写进 `models.json`**：宿主把它写入 pi 官方凭据存储 `auth.json`（权限 0600，位于运行期目录），进程环境里不再有该变量，内核的 `env` 类读取拿不到（三面实测 0）；详见 architecture.md §6 与 docs/evidence/credential-boundary.txt。

复现：

```bash
# 模型 id 插值失败的证据（输出里出现字面量 $OPENAI_MODEL）
PI_CODING_AGENT_DIR=<dir with models.json using $OPENAI_MODEL> pi --list-models
```

## 2. thinking 参数与 reasoning 解析

- pi 对 `reasoning: true` 的模型**会发送**思考参数：抓包显示请求体含 `"reasoning":{"effort":"medium"}`（前提是模型定义带 `compat.thinkingFormat: "openrouter"`；默认自定义 provider 下不会发送）。
- 上游 OpenRouter 流式响应**确实返回**思考内容，字段是 `choices[].delta.reasoning`（附带 `reasoning_details[]`）。
- **pi 能把它解析为 thinking 内容块**：真实 run 的 `message_update` 序列里看到 80 段 `thinking_delta`（共 444 字符），见 `docs/evidence/local-m1-smoke.txt`。
- 但**简单提示词不一定产出 thinking**：早前两次极短 prompt（“只回复两个字：收到”、“9.11 和 9.9 哪个大”）的事件序列里没有 `thinking_*`，`message_end.message.content` 也只有 text 块。当时据此误判为“pi 不解析 reasoning”；复杂多轮任务（读文件 + 写报告）则稳定产出。

**结论**：思考展示链路在本机 OpenRouter 路径下**可用**，但思考内容是否出现取决于任务复杂度与上游是否返回 reasoning，不能当作每次 run 的必然产物；前端必须能接受“这一轮没有思考”的正常形态（见 `docs/architecture.md` 事件契约）。

抓包证据（宿主的记录代理，用于确认请求参数与上游响应形态）：

```
request keys: ['max_completion_tokens','messages','model','reasoning','store','stream','stream_options','tools']
request body: {"model":"openai/gpt-5.6-sol","reasoning":{"effort":"medium"},"stream":true,"max_completion_tokens":65536}
message roles: ['developer','user']
```

上游 SSE 首帧：

```
data: {"choices":[{"index":0,"delta":{"content":"","role":"assistant","reasoning":"**Decoding decimals**\n\nI","reasoning_details":[{"type":"reasoning.summary",...
```

## 3. 配置目录重定向

- `PI_CODING_AGENT_DIR` 覆盖 pi 配置目录（默认 `~/.pi/agent`）；指向项目 `agent/` 后，用户个人配置（packages、models、extensions）不再加载，这正是本仓库需要的隔离语义。
- `PI_CODING_AGENT_SESSION_DIR` 或 `--session-dir` 覆盖会话目录；宿主把它指向**每个 run 自己的**会话子目录（`PI_CODING_AGENT_SESSION_DIR/<runId>`，默认 `<data>/sessions/<runId>`），run 与 session 文件一一对应，且不会与其他 run 混写。
- `--provider <id> --model <含斜杠的完整 id>` 可正常工作（实测 `--provider frontier --model openai/gpt-5.6-sol`，`get_state` 返回该 id）。
- **pi 会往配置目录写运行时状态**：实测 pi 会在其中生成 `models-store.json`、`npm/`（插件安装目录）等；`auth.json` 则是 pi 官方凭据存储，本仓库由**宿主在运行期渲染**（模型 key，0600），不是 pi 写出的空对象。因此仓库里的 `agent/` **只能作为模板**，不能直接当成 `PI_CODING_AGENT_DIR` 使用；运行期目录必须由宿主渲染到每个 run 自己的 `runs/<runId>/pi-agent`，`.gitignore` 保留第二道防线。

## 4. RPC 协议（`pi --mode rpc`）

- JSONL over stdio：命令写 stdin，`response` 帧带 `id` 回执，其余为事件；未知命令返回 `success: false` 而不中断会话（实测 `get_commands` 之外的 `get_available_commands` 会报 `Unknown command`）。
- 帧分隔符只有 LF；含 U+2028 的字符串不会被拆帧（回归 `test_frames_split_on_lf_only_and_keep_u2028` 覆盖）。
- 一次真实研究型 prompt 的事件面：`agent_start → turn_start → message_start → message_update(text_*) … → tool_execution_start/update/end → turn_end → … → agent_settled`。
- `agent_settled` 是判断“pi 不会再自动继续”的可靠信号（`agent_end` 之后仍可能有重试/压缩/排队续跑），但它**不是成功信号**：最终结果看最后一条 assistant `message_end` 的 `stopReason`（`error`/`aborted`/`length`/`stop`），终态判定见 architecture.md §4。

## 5. pi-web-access 接入（已实测）

- **配置目录解析**：pi-web-access 的 `utils.ts` 中 `getWebSearchConfigDir()` **优先读 `process.env.PI_CODING_AGENT_DIR`**，且不检查该目录下是否已有 `web-search.json`。这与 pi 本身的重定向一致，但也意味着：一旦宿主重定向了配置目录，操作员本机 ``~/.pi/agent/web-search.json` 就不再被读到，检索会静默退化成无凭据模式。
- **凭据装配**：宿主在本 run 的运行期目录渲染 `web-search.json`：优先用 `PI_GLINK_WEB_SEARCH_KEY`（渲染为 `{"exaApiKey": …}`），否则复制操作员本机的 `~/.pi/agent/web-search.json`（或 `~/.pi/web-search.json`）。无凭据不是错误：pi-web-access 仍可用无密钥的 Exa MCP 检索；每次渲染都会重建或删除该文件，所以上一 run 的凭据不会残留。
- **插件安装**：在 `agent/settings.json` 的 `packages` 里声明 `npm:pi-web-access@0.31.0`（精确 pin）。inproc 面 pi 启动时把它装到 `<PI_CODING_AGENT_DIR>/npm/`；docker/e2b 面宿主把该声明改写成镜像内本地包 `/opt/pi-packages/node_modules/pi-web-access`，不在 run 里联网安装（实测与机制见 §9）。
- **工具面**：实测一次检索 run 调用 `web_search`（1 次）+ `fetch_content`（3 次），事件流里以 `tool_started/tool_updated/tool_finished` 正常呈现，报告写入 10 个可核验 URL。见 `docs/evidence/local-web-search.txt`。

## 6. Docker / E2B 沙箱内的配置目录与凭据注入（已实测）

三面都在**沙箱内运行 pi**，宿主只提供数据通道；凭据一律走运行期 `auth.json`，不进进程环境。

| 事实 | Docker | E2B |
|---|---|---|
| `PI_CODING_AGENT_DIR` | `/config`（本 run 运行期目录的 bind mount，读写：pi 要往 `npm/` 装插件、写运行时状态） | `/home/user/config`（整棵上传，带 `(路径, sha256)` 清单） |
| 会话目录 | `/sessions`（bind mount 到本 run 的会话子目录；pi 上报容器路径，宿主词法映射回该目录） | `/home/user/sessions`（随沙箱销毁） |
| 工作区 | `/workspace` | `/home/user/workspace`（`e2b_env.SANDBOX_WORKSPACE`） |
| 模型凭据 | `auth.json` 在挂载内（0600，仓库外） | 上传的 `auth.json` |
| 进程环境含 key | 否（`env \| grep -c OPENAI_API_KEY` = 0） | 否（同为 0） |

实测要点：

- **Docker 会话文件是容器路径**：pi 报 `/sessions/<file>.jsonl`，该路径在宿主不存在；宿主必须先在
  `kernel/docker_env.py::host_session_path` 做纯词法映射（拒绝相对路径、前缀伪装、`..`），再做收尾
  脱敏。只映射会话挂载，`/config/auth.json` 永不映射。未映射到的新路径会显式告警而不是静默保留。
- **E2B 没有双向 stdio**：SDK 不提供持久 stdin，所以这一面用 `pi --mode json "<prompt>"` 单向事件流。
  事件可以实时：`commands.run(background=True)` 返回的 `CommandHandle` 才是流式接口（迭代它逐块拿到
  stdout；把 `on_stdout` 传给 `run()` 本身不生效，那是早期的用法误判）；宿主把 pi 的 stdout 同时
  `tee` 进 `/home/user/events.jsonl`，按 LF 逐行投递，断流时用文件补发未送出的行。宿主侧离线已验证，
  真实沙箱实时性待 live 复验。
- **Node 与 pi 固定在两侧镜像**：`node:24.21.0-bookworm-slim` 精确 tag + pi 0.86.1，镜像自包含
  （无挂载时 `docker run --rm pi-glink-worker:latest pi --version` = 0.86.1）。
- 证据：`docs/evidence/docker-run.txt`、`docs/evidence/e2b-run.txt`、`docs/evidence/credential-boundary.txt`。

## 7. 工具输出的流式语义（已实测）

`tool_execution_update.partialResult` 是**截至当前的累计快照，不是增量**。pi `docs/rpc.md` 原文：

> The `partialResult` in `tool_execution_update` contains the accumulated output so far (not just the delta), allowing clients to simply replace their display on each update.

真实 run `ebf75f590601` 的 bash 工具给出 3 帧 `tool_updated`，长度 `0 → 53 → 495`，后两帧存在前缀包含：

```
seq=86  len=0
seq=87  len=53   '/Users/coldxiangyu/.pi-glink/workspaces/ebf75f590601\n'
seq=88  len=495  同前 53 字符 + '\nPI vars:\nPI_CODING_AGENT_DIR=…'
```

因此宿主投影层原样传递累计快照，前端**每次替换**显示；若按增量拼接会得到 548 字符的重复前缀。
回归：`tests/web/run-stream.test.mjs` 的 `cumulative tool snapshots replace the display instead of duplicating prefixes`。

**终帧与最后一次更新帧是同一段文本**：同一 run 的 `seq=89 tool_finished.result` 与
`seq=88 tool_updated.output` 逐字相同（均 495 字符）。终帧去重同样属于前端责任：
`run-stream.mjs::finishTool` 在 `result === item.output` 时清空 output，卡片只渲染
result 一次；两者不同的工具仍保留两份内容。校验：
`tests/web/run-stream.test.mjs` 的 `the audited run ebf75f590601 replays with replacement semantics (real fixture)`
断言 `output + result` 只渲染 495 字符且前缀仅出现一次；浏览器回归以
`toolsRenderingTextTwice === 0` 卡住同类回退（证据 `docs/evidence/browser-run.json`）。

## 8. pi-subagents 0.40.0 验证（spike GLINK-SUB-01）

结论：**0.40.0 没有配置开关可以禁止 async/detached 子 agent**，也没有相关环境变量；按 spike 门禁
就此停止，未做凭据继承、取消传播、事件投影/脱敏与产物落点的 live 验证（agent/settings.json 未改）。
该结论仍然成立，也是 GLINK-SUB-02 改用 pi-glink 自有 `tool_call` 守卫的原因；前台强制方案、
完整入口清单与配置取值见 §10。
本节其余事实（配置面键集、行号、参考副本与发布包的差异）继续有效；
本节的源码行号与事实来自 `npm install --prefix /tmp/glink-sub-spike/prefix pi-subagents@0.40.0`
的发布包（integrity 与原始输出见 `docs/evidence/pi-subagents-spike.txt`）。

配置面（`<PI_CODING_AGENT_DIR>/extensions/subagent/config.json`，路径见 `src/extension/config.ts:8-9`，
`PI_CODING_AGENT_DIR` 由 `src/shared/utils.ts:95-100` 解析）只有 26 个键，其中 async 方向的只有
`asyncByDefault`（默认 false，只改默认值）与 `forceTopLevelAsync`（把顶层 launch 强制改成 async）；
不存在 force-foreground/disable-async 类键。模型面对 `subagent` 工具始终可见
`async: boolean`（`src/extension/schemas.ts:316`），分派判定为
`runsForeground = clarify === true || (async ?? asyncByDefault) !== true`
（`src/runs/foreground/subagent-executor.ts:4467-4468`）；模型传 `async: true` 即走 detached
子进程（`src/runs/background/async-execution.ts:454` `detached: true`，`env: {...process.env, ...}`）。
pi-subagents 自带 skill 还明确建议每次启动都用 `async: true`
（`skills/pi-subagents/references/execution-controls.md:122`）。

可关闭的非必需面：`scheduledRuns` 需显式 `enabled: true` 才启用（`src/runs/background/scheduled-runs.ts:70`）；
watchdog 默认 `enabled: false`（`src/watchdog/settings.ts:71-72`），LSP 诊断受 watchdog 约束；
`intercomBridge` 未配置时 mode 解析为 `always`、可用 `mode: "off"` 关闭
（`src/intercom/intercom-bridge.ts:77-80`）；`artifactDir` 可在 `project`/`session`/`temp` 间配置
（`src/shared/types.ts:1632`）。

复现：`scripts/spikes/pi_subagents/audit_async_switch.sh <pi-subagents-root>`（只读、离线；发现
新增 async/前台开关时退出 1，0.40.0 实测退出 0）。注意 `~/.feynman/npm-global` 下的参考副本与 npm 发布
包不是同一源码：23 个 `src/` 文件不同（参考副本多出 `modelOverrideFromParent`、`contextOverflow`、
`finalizeToolResult` 等符号），两侧 version 都写 0.40.0；引用行号前先按实际安装包核对。

## 9. 容器面预装 pi 包与本地包加载（GLINK-PKG-01，已实测）

**动机与实测**：E2B 每轮从沙箱就绪到 `agent_start` 固定约 15–16s（真实 run `4be8cb8ac9f4`、
`160d10412704`，模型首字仅约 2s），根因是 `settings.json` 的 `packages: ["npm:pi-web-access"]`
未 pin，pi 在每个 run 的 `PI_CODING_AGENT_DIR/npm` 里现场 `npm install`（历史 run 的
`pi-agent/npm/package.json` 写 `pi-web-access ^0.31.0`）。容器内同配置对照（pi 0.86.1，
无模型调用）：

| `packages` 声明 | 沙箱内 `pi --mode rpc` 启动 | 运行期目录 |
|---|---|---|
| `npm:pi-web-access@0.31.0` | 16.5s（其中 npm “added 210 packages in 11s”） | `npm/` 85MB |
| `/opt/pi-packages/node_modules/pi-web-access` | 3.4s | 无 `npm/` |

**本地包是官方扩展点**：`docs/packages.md` 的 local source（原文 “Loaded from the resolved path
without copying”）；`docs/settings.md`：“Resource paths in user settings resolve from the agent
directory… Absolute paths and `~` are supported.” 实测 `get_commands` 的 `sourceInfo.source`
就是该绝对路径，`origin: package`；宿主 0.87.1 与隔离安装的 0.86.1 行为一致。

**pi 对不存在的本地包完全静默**：把 `packages` 指向 `/nonexistent/pi-web-access` 时 rc=0、
stderr 为空、`get_commands` 里没有扩展资源。所以只把声明换成路径不够：两个镜像都带
`/opt/pi-packages/check-packages.sh`（Docker 面作为容器 `--entrypoint`，E2B 面先验证守卫再交给
守卫 exec pi），缺包 exit 78；否则研究 run 会静默丢掉 `web_search`。

**安装方式必须跟 pi 自己的 npm 托管安装一致**：`dist/core/package-manager.js::getNpmInstallArgs`
返回 `npm install … --prefix <root> --legacy-peer-deps`（注释原文：Disable peer dependency
resolution for managed installs … so package managers do not install or solve host-provided
`@earendil-works/pi-*` peers）。本仓第一次候选构建用默认 npm 语义，就装入了
`@earendil-works/{pi-ai,pi-coding-agent,pi-tui,pi-telemetry}` 0.87.1，而镜像内 pi 是 0.86.1；
pi-web-access 的 `supportsDynamicTools()` 会 `import.meta.resolve` 这份副本并据其版本把 web tools
从 eagerly available 切到动态激活路径。加 `--legacy-peer-deps` 后 `/opt/pi-packages` 下不再有
`@earendil-works` 副本，扩展回到与运行期 npm 安装相同的 eager 回退（两边 stderr 提示一致）。

**版本真源**：`agent/settings.json` 的 pin、两个 Dockerfile 的 `ARG PI_WEB_ACCESS_VERSION` /
`ARG PI_SUBAGENTS_VERSION` 由 `tests/test_repository_boundaries.py` 卡住一致（与 §3 的 PI_VERSION
同口径）。候选构建/自检/tag 移动/回滚与完整输出见 `docs/evidence/prebaked-packages.txt`。
`pi-subagents@0.40.0` 在 GLINK-SUB-02 起写进 `packages` 并被真正加载（前台强制方案见 §10）；
镜像守卫脚本仍只检查 `pi-web-access`，该已知缺口固定在
`tests/test_kernel_docker_env.py::test_the_image_guard_covers_every_prebaked_package`。

## 10. pi-subagents 0.40.0 的前台强制接入（GLINK-SUB-02，离线验证）

§8 的结论继续成立：0.40.0 没有“禁止 async/detached”的包开关。GLINK-SUB-02 因此不依赖包开关，
而是在 pi-glink 侧用官方 `tool_call` 钩子（`agent/extensions/subagent-foreground-guard.ts`）
把派发收回到主 run 内的**前台同步**调用，并按“最保守取值”固定 pi-subagents 的 extension config。
原则：**能安全改写就改写（`async:false`、`chainDir` 进工作区），改写了会改变语义／无法保证生命周期
的就阻止并给出可理解的原因**，未知输入一律 fail-closed。

源码基线：`npm install --prefix /tmp/glink-sub2/prefix pi-subagents@0.40.0` 的发布包（仓库外；
GLINK-SUB-01 已记录它与 `~/.feynman/npm-global` 参考副本有 23 个 `src/` 文件不同）。以下行号都指向
该发布包。离线验证证据：`docs/evidence/pi-subagents-foreground.txt`。

**范围注**：本任务只做工具面的强制前台与子代理定义（`agent/agents/researcher.md`）；提示词侧的委派
纪律（何时派、怎么描述任务、主 agent 保留哪些环节）按 Lead 指令暂缓，`agent/research-prompt.md`
与 `/deepresearch` 模板保持原约定。

### 10.1 能把执行推离主 run 的入口清单与处置

| # | 入口（源码位置） | 形态 | 处置 | 理由 |
|---|---|---|---|---|
| 1 | `subagent` 的 `async`（`src/extension/schemas.ts:316`；分派判定 `src/runs/foreground/subagent-executor.ts:4467-4468`） | detached 子进程 | **改写**：显式 `async:false` | 前台是同一工具的受支持形态；同一处判定 `runsForeground = clarify \|\| (async ?? asyncByDefault) !== true` |
| 2 | agent frontmatter `async: true`（`src/agents/agents.ts:1470-1474`，应用点 `subagent-executor.ts:1799`：仅当 `params.async === undefined`） | 隐式 async 默认值 | **改写**：同 #1 的显式 `false` 直接覆盖 | 只看参数是否缺省，显式 `false` 是唯一能盖住它的地方 |
| 3 | 配置 `asyncByDefault` / `forceTopLevelAsync`（`src/extension/config.ts:8-9`、`src/extension/index.ts:197`、`src/runs/background/top-level-async.ts:1-13`） | 把 launch 推向 async | **config 置 false + 扩展仍显式改写** | `forceTopLevelAsync` 只会把顶层 `async` 改成 `true`，没有反向开关（§8） |
| 4 | `action:"schedule"`（+`schedule`/`scheduleName`，`schemas.ts:291-292`；`subagent-executor.ts:3978-3987`；开关 `src/runs/background/scheduled-runs.ts:70`） | 定时、总是 async、fresh context | **阻止** + `scheduledRuns.enabled:false` | 无法改写成同步（定时语义本身依赖后台进程） |
| 5 | `action:"resume"`（`subagent-executor.ts:3907`；`src/runs/background/async-resume.ts`） | 复活 async/暂停的子代理 | **阻止** | 目标就是后台 run；改写会改变调用含义 |
| 6 | `action:"steer"`（`subagent-executor.ts:3910-3974`，默认带 pause-and-revive 恢复） | 给活着的 async run 发指令，失败时复活它 | **阻止** | 同上；本运行不存在可 steer 的 async run |
| 7 | `action:"append-step"`（`subagent-executor.ts:3975-3977`） | 往运行中的 async chain 追加步骤 | **阻止** | 只对后台链有意义 |
| 8 | `action:"stop"` / `"interrupt"`（`subagent-executor.ts:3988-4060`） | 控制 async/前台 run | **阻止** | 控制面只作用于后台/在途 run；前台等待由 `timeoutMs` 保证 |
| 9 | watchdog 动作 `watchdog.status/check/configure/recommend-model`（`src/watchdog/tool-actions.ts:154`；`subagent-executor.ts:3690-3708`） | 诊断/改写 watchdog 设置（可写 user/project settings） | **阻止** + settings 关 watchdog | 整面关闭；`configure` 还会改配置文件，不放进模型可写面 |
| 10 | `action:"grant-spawn-budget"`（`subagent-executor.ts:3709-3772`） | 提高 session 派发上限（交互确认） | **阻止** | 无 UI 确认；上限由配置固定 |
| 11 | agent/chain 管理动作 `create/update/delete/eject/disable/enable/reset`（`src/agents/agent-management.ts:1152-1165`） | 运行中改写 agent 定义 | **阻止**（只读 `list/get/models` 放行） | 避免运行中改写 `PI_CODING_AGENT_DIR/agents`；`update/delete` 能改掉本次 researcher |
| 12 | `subagent_wait` 工具（`src/runs/background/wait-tool.ts:8`，注册 `src/extension/index.ts:460`） | 等待后台作业 | **阻止** + `waitTool.enabled:false` | 本运行没有后台作业；留着只会让模型白等一轮 |
| 13 | detached 子进程本体（`src/runs/background/async-execution.ts:451-460`：`detached:true`、`env: {...process.env}`） | 脱离 pi 进程的子进程 | 由 #1-#3 使其不可达 | 前台运行不经过这条路径 |
| 14 | intercom/父子监督通道（`src/intercom/intercom-bridge.ts:77-80` 默认 `mode:"always"`；`src/intercom/native-supervisor-channel.ts:295/307/636-637`） | 子代理↔父进程的额外通道 | **config `intercomBridge.mode:"off"`** | 前台同步调用不需要跨进程通道 |
| 15 | 嵌套派发（子代理再派子代理）：深度检查 `src/shared/types.ts:1794-1799`，拒绝点 `subagent-executor.ts:4086-4094` | 递归委派 | **`maxSubagentDepth:1`** + researcher 的 `tools` 白名单不含委派工具（`src/runs/shared/pi-args.ts:289` 把 frontmatter `tools` 变成 `--tools`） | 双层：工具面先拿不到，深度上限兜底 |
| 16 | `share: true`（`schemas.ts` 单 agent overrides；`src/runs/foreground/execution.ts` 的 `shareEnabled`） | 把子代理会话上传到 GitHub Gist | **阻止** | 会话内容离开本 run 目录，超出宿主的脱敏与目录边界 |
| 17 | `worktree: true`（`schemas.ts:240/307`；`src/runs/shared/worktree.ts:173-175` 默认基目录 `os.tmpdir()`） | 在 run 目录外创建 git worktree | **阻止** | 生命周期不由本 run 回收路径保证，且落在工作区外 |
| 18 | `cwd` / `sessionDir` / `output` / `chainDir` 路径参数（`schemas.ts:135/157/188/218/315`；chainDir 默认 `src/shared/settings.ts:174-176` = `CHAIN_RUNS_DIR`，即 OS 临时目录） | 把读写/产物放到工作区外 | **阻止**（工作区外）或 **改写**（`chainDir` 固定为 `<workspace>/.pi-subagents/chain-runs`） | 宿主只对本 run 工作区做收尾脱敏与目录边界保护（architecture.md §6.1） |
| 19 | **不在工具面上的入口**：pi-subagents 注册的扩展命令（`src/slash/slash-commands.ts:1181-1340`：`/run [--bg]`、`/chain [--bg]`、`/parallel`、`/subagents-detach`、`/subagents-stop`、`/subagents-fleet`、…） | 用户输入（RPC `prompt`）直接执行的扩展命令 | **未覆盖（已知）** | pi 先派发扩展命令、后触发 `input` 事件（`docs/rpc-commands.md`：extension command “executes immediately”），`tool_call` 钩子拿不到；见 §10.4 |

判定与常量都在 `agent/extensions/subagent-foreground-guard.ts`；单测
`tests/extensions/subagent-foreground-guard.test.mjs` 对每一行都有正反例（含 handler 异常时阻止）。

### 10.2 extension config 逐项取值（`agent/extensions/subagent/config.json`）

pi-subagents 从 `<PI_CODING_AGENT_DIR>/extensions/subagent/config.json` 读扩展配置
（`src/extension/config.ts:8-9`，`PI_CODING_AGENT_DIR` 解析见 `src/shared/utils.ts:95-100`）。
该目录没有 `index.ts`/`index.js`，pi 的扩展发现不会把它当成扩展加载（只在模板里当配置交付；
加载冒烟确认没有 Tool conflicts / extension_error）。

| 键 | 取值 | 作用与理由 |
|---|---|---|
| `asyncByDefault` | `false` | 默认不推向 async；模型显式 `async:true` 仍由 #1 的守卫改写 |
| `forceTopLevelAsync` | `false` | 关掉“顶层强制 async” |
| `waitTool` | `{"enabled": false}` | `subagent_wait` 不再阻塞等待（工具本身仍注册，由 #12 阻止调用） |
| `scheduledRuns` | `{"enabled": false}` | 关掉定时 run；`action:"schedule"` 另被 #4 阻止 |
| `intercomBridge` | `{"mode": "off"}` | 关掉默认 `always` 的 intercom 桥（#14） |
| `maxSubagentDepth` | `1` | 顶层 depth=0 可派，子代理 depth=1 被拒 → 子代理不得再派（#15） |
| `globalConcurrencyLimit` | `4` | 与前台 `tasks` 并行的预期规模一致（2–4 个 researcher） |
| `maxSubagentSpawnsPerSession` | `8` | 单 run 派发次数上限，防止失控扇出 |
| `parallel` | `{"concurrency": 4}` | 并行任务并发度上限 |
| `artifactDir` | `"project"` | 产物落 `<workspace>/.pi-subagents/artifacts`（`src/shared/artifacts.ts:6/20-40`），在工作区内 → 被收尾脱敏覆盖 |
| `proactiveSkillSubagents` | `false` | 关掉“按 skill 主动推荐子代理”的提示（默认是开的：`src/agents/proactive-skills.ts:48-52`） |
| `control` | `{"enabled": false}` | 关掉后台注意力提示/通知面 |

watchdog 不在这个文件里：它读 `<PI_CODING_AGENT_DIR>/settings.json` 的 `subagents.watchdog`
（`src/watchdog/settings.ts:384-389/422-424`）。`agent/settings.json` 因此显式写
`subagents.watchdog.{enabled,main.enabled,children.enabled} = false`（默认本来关闭，
见 `src/watchdog/settings.ts:71-72`；显式写入防止任何一处被打开）。同一份 settings.json 的
`packages` 现在同时声明两个精确 pin（`npm:pi-web-access@0.31.0`、`npm:pi-subagents@0.40.0`），
与两个 Dockerfile 的 ARG 由 `tests/test_repository_boundaries.py` 卡住一致。

### 10.3 agent 定义的发现路径与优先级

pi-subagents 从 `<PI_CODING_AGENT_DIR>/agents` 读 user 作用域定义（`src/agents/agents.ts:1646-1651`，
`getAgentDir()` 优先 `PI_CODING_AGENT_DIR`）。渲染模板因此把 `agents` 加进 `TEMPLATE_ENTRIES`
（`host/pi_glink/kernel/agent_env.py`），三个面都会拿到 `pi-agent/agents/researcher.md`。

发现范围与优先级（`discoverAgents` + `mergeAgentsForScope`，`src/agents/agent-selection.ts:12-24`）：

1. builtin（包自带 `agents/`，含一个同名 `researcher`）→ 2. package → 3. **user**（本项目渲染的
   `PI_CODING_AGENT_DIR/agents`，以及 `~/.agents` 与 `PI_SUBAGENT_EXTRA_AGENT_DIRS`）→ 4. project
   （工作区最近 `.pi/agents`、`.agents`）。同名后者覆盖前者：**本仓的 `researcher` 覆盖包内建的
   同名定义**。`agentScope` 默认 `both`，所以工作区里若出现 `.pi/agents/researcher.md` 也会覆盖
   本仓定义——本仓的提示词不写这种文件，作为已知发现事实记录在这里。

researcher 的 `tools` 白名单（`read,bash,write,edit,web_search,fetch_content,get_search_content`）
会被 `--tools` 交给子进程（`src/runs/shared/pi-args.ts:289`），所以子代理既没有委派工具，也没有
工作台之外的工具；`systemPromptMode` 对自定义 agent 默认 `replace`，子代理的系统提示就是
`researcher.md` 正文（不继承主 run 的 `--append-system-prompt`）。

### 10.4 已知未覆盖项与残留风险（如实记录）

1. **扩展命令面**（#19）：`/run --bg`、`/chain --bg`、`/subagents-detach` 等由用户输入直接执行，
   `tool_call` 钩子不可达；pi 的 `input` 事件在扩展命令派发之后才触发，扩展也没有注销他扩展命令的
   API。仅有模型工具面的强制前台保证，用户输入面的同名能力仍可发起后台 run（同 sandbox 内、无新增
   权限，但生命周期不随主 run 取消/超时收口）。
2. **子代理会话文件的收尾脱敏**：前台子进程默认把会话写到
   `<run 会话目录>/<父会话名>/run-N/session.jsonl`（`subagent-executor.ts:4233-4253`、`4267-4269`；
   基目录来自 `src/extension/index.ts:86-93`）。它在本 run 目录内，但宿主收尾只脱敏
   **本 run 自己那个** `.jsonl`（`kernel/runner.py::_sanitize_session`），子代理会话文件不在覆盖范围。
   工作区内的 `.pi-subagents/`（artifacts/chain-runs）会随工作区清扫被脱敏。
3. **未做真实 run 效果验证**：本任务只有离线单测与无模型加载冒烟；真实模型/检索下的前台
   `tasks` 并行、取消传播、事件投影顺序与容器面（Docker/E2B）行为未验证。
4. **容器 pi 版本差异**：本机加载冒烟用 pi 0.87.1，镜像/模板内是 0.86.1；`tool_call` 钩子与
   `getAllTools` 等 API 两版都覆盖本次用到的部分，但未在 0.86.1 上跑同一冒烟（未修改镜像）。
5. **镜像守卫未覆盖 pi-subagents**：两个镜像无条件安装它（ARG pin），但
   `/opt/pi-packages/check-packages.sh` 的 `for pkg in pi-web-access;` 不含它；旧镜像/被删包时
   pi 会静默跳过该本地包（缺的是子代理能力，主 run 仍可自己取证）。本任务禁改 Dockerfile，
   缺口固定在 `tests/test_kernel_docker_env.py`。
