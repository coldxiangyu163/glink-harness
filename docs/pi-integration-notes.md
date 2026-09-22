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
- `PI_CODING_AGENT_SESSION_DIR` 或 `--session-dir` 覆盖会话目录；宿主把它指向数据根，run 与 session 文件一一对应。
- `--provider <id> --model <含斜杠的完整 id>` 可正常工作（实测 `--provider frontier --model openai/gpt-5.6-sol`，`get_state` 返回该 id）。
- **pi 会往配置目录写运行时状态**：实测 pi 会在其中生成 `models-store.json`、`npm/`（插件安装目录）等；`auth.json` 则是 pi 官方凭据存储，本仓库由**宿主在运行期渲染**（模型 key，0600），不是 pi 写出的空对象。因此仓库里的 `agent/` **只能作为模板**，不能直接当成 `PI_CODING_AGENT_DIR` 使用；运行期目录必须由宿主渲染到数据根下（`<data_root>/pi-agent`），`.gitignore` 保留第二道防线。

## 4. RPC 协议（`pi --mode rpc`）

- JSONL over stdio：命令写 stdin，`response` 帧带 `id` 回执，其余为事件；未知命令返回 `success: false` 而不中断会话（实测 `get_commands` 之外的 `get_available_commands` 会报 `Unknown command`）。
- 帧分隔符只有 LF；含 U+2028 的字符串不会被拆帧（回归 `test_frames_split_on_lf_only_and_keep_u2028` 覆盖）。
- 一次真实研究型 prompt 的事件面：`agent_start → turn_start → message_start → message_update(text_*) … → tool_execution_start/update/end → turn_end → … → agent_settled`。
- `agent_settled` 是判断"这一次 run 完全落定"的可靠信号（`agent_end` 之后仍可能有重试/压缩/排队续跑）。

## 5. pi-web-access 接入（已实测）

- **配置目录解析**：pi-web-access 的 `utils.ts` 中 `getWebSearchConfigDir()` **优先读 `process.env.PI_CODING_AGENT_DIR`**，且不检查该目录下是否已有 `web-search.json`。这与 pi 本身的重定向一致，但也意味着：一旦宿主重定向了配置目录，操作员本机 ``~/.pi/agent/web-search.json` 就不再被读到，检索会静默退化成无凭据模式。
- **凭据装配**：宿主在运行期目录渲染 `web-search.json`：优先用 `PI_GLINK_WEB_SEARCH_KEY`（渲染为 `{"exaApiKey": …}`），否则复制操作员本机的 `~/.pi/agent/web-search.json`（或 `~/.pi/web-search.json`）。无凭据不是错误：pi-web-access 仍可用无密钥的 Exa MCP 检索。
- **插件安装**：在 `agent/settings.json` 的 `packages` 里声明 `npm:pi-web-access`，pi 启动时把它装到 `<PI_CODING_AGENT_DIR>/npm/`（实测会在运行期目录下生成 `npm/node_modules/`）。
- **工具面**：实测一次检索 run 调用 `web_search`（1 次）+ `fetch_content`（3 次），事件流里以 `tool_started/tool_updated/tool_finished` 正常呈现，报告写入 10 个可核验 URL。见 `docs/evidence/local-web-search.txt`。

## 6. Docker / E2B 沙箱内的配置目录与凭据注入（已实测）

三面都在**沙箱内运行 pi**，宿主只提供数据通道；凭据一律走运行期 `auth.json`，不进进程环境。

| 事实 | Docker | E2B |
|---|---|---|
| `PI_CODING_AGENT_DIR` | `/config`（宿主运行期目录的 bind mount，读写：pi 要往 `npm/` 装插件、写运行时状态） | `/home/user/config`（上传） |
| 会话目录 | `/sessions`（bind mount；pi 上报容器路径，宿主词法映射回 `<data>/sessions/…`） | `/home/user/sessions`（随沙箱销毁） |
| 工作区 | `/workspace` | `/home/user/workspace`（`e2b_env.SANDBOX_WORKSPACE`） |
| 模型凭据 | `auth.json` 在挂载内（0600，仓库外） | 上传的 `auth.json` |
| 进程环境含 key | 否（`env \| grep -c OPENAI_API_KEY` = 0） | 否（同为 0） |

实测要点：

- **Docker 会话文件是容器路径**：pi 报 `/sessions/<file>.jsonl`，该路径在宿主不存在；宿主必须先在
  `kernel/docker_env.py::host_session_path` 做纯词法映射（拒绝相对路径、前缀伪装、`..`），再做收尾
  脱敏。只映射会话挂载，`/config/auth.json` 永不映射。未映射到的新路径会显式告警而不是静默保留。
- **E2B 没有双向 stdio**：SDK 不提供持久 stdin，所以这一面用 `pi --mode json "<prompt>"` 单向事件流；
  事件是 run 结束后回放的，不是实时 SSE（沙箱内 `on_stdout` 回调实测不触发）。
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
