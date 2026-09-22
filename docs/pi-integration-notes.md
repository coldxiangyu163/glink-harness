# pi 集成实测笔记（0.86.1）

本文件记录**实测得到**的 pi 行为事实。每条都有可复现的命令或证据来源；架构决策引用这里的结论，而不是推测。

测试基线：pi `0.86.1`、Node `v24.16.0`、宿主 Python 3.12、上游 OpenRouter（`https://openrouter.ai/api/v1`，模型 `openai/gpt-5.6-sol`）。

## 1. `models.json` 的环境变量插值范围

| 字段 | 支持 `$VAR` 插值 | 证据 |
|---|---|---|
| `apiKey` | ✅ 支持 | `docs/models.md`「Value Resolution」明确列出 apiKey 与 headers |
| `headers` | ✅ 支持 | 同上 |
| `baseUrl` | ❌ 不支持 | 设 `"baseUrl": "$OPENAI_BASE_URL"` 后 `pi --list-models` 能列出模型（不校验 URL），实际请求报 `Invalid URL` |
| `models[].id` | ❌ 不支持 | `pi --list-models` 原样打印 `$OPENAI_MODEL` 字样 |

**结论**：宿主编译期无法把 endpoint 与模型 id 交给 pi 自己插值，必须由宿主在运行期渲染 `models.json`（见 `host/pi_glink/kernel/agent_env.py`）。密钥仍可用 `$OPENAI_API_KEY` 占位，由 pi 从子进程环境解析，因此**不需要落盘**。

复现：

```bash
# 模型 id 插值失败的证据（输出里出现字面量 $OPENAI_MODEL）
PI_CODING_AGENT_DIR=<dir with models.json using $OPENAI_MODEL> pi --list-models
```

## 2. thinking 参数与 reasoning 解析

- pi 对 `reasoning: true` 的模型**会发送**思考参数：抓包显示请求体含 `"reasoning":{"effort":"medium"}`（前提是模型定义带 `compat.thinkingFormat: "openrouter"`；默认自定义 provider 下不会发送）。
- 上游 OpenRouter 流式响应**确实返回**思考内容，字段是 `choices[].delta.reasoning`（附带 `reasoning_details[]`）。
- **但 pi 0.86.1 不把该字段解析成 thinking 内容块**：同一 run 的 `message_update` 序列只有 `text_*`，`message_end.message.content` 里没有 `thinking` 块，因此产品事件流里没有 `thinking_delta`。
- pi 没有内置 `openrouter` provider（`pi --list-models` 中 0 条 openrouter 记录），无法通过内置 provider 绕过。

**结论**：本机 OpenRouter 路径下暂时拿不到思考内容。这是 pi 内核侧的解析缺口，不属于本仓库可修复范围（不 fork 内核）。接入层的 `thinking_delta → thinking_delta` 投影路径已实现并由协议级回归覆盖（`tests/fixtures/fake_pi.py` 会发 `thinking_delta`），一旦 pi 支持该字段（或换用会返回 `reasoning_content` 的上游），同一通道立刻可用，无需改动宿主与前端。

抓包证据（宿主的记录代理）：

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
- **pi 会往配置目录写运行时状态**：实测出现过 `auth.json` 与 `models-store.json`（当日为空对象，无凭据）。因此仓库里的 `agent/` **只能作为模板**，不能直接当成 `PI_CODING_AGENT_DIR` 使用；运行期目录必须由宿主渲染到数据根下（`<data_root>/pi-agent`），`.gitignore` 保留第二道防线。

## 4. RPC 协议（`pi --mode rpc`）

- JSONL over stdio：命令写 stdin，`response` 帧带 `id` 回执，其余为事件；未知命令返回 `success: false` 而不中断会话（实测 `get_commands` 之外的 `get_available_commands` 会报 `Unknown command`）。
- 帧分隔符只有 LF；含 U+2028 的字符串不会被拆帧（回归 `test_frames_split_on_lf_only_and_keep_u2028` 覆盖）。
- 一次真实研究型 prompt 的事件面：`agent_start → turn_start → message_start → message_update(text_*) … → tool_execution_start/update/end → turn_end → … → agent_settled`。
- `agent_settled` 是判断"这一次 run 完全落定"的可靠信号（`agent_end` 之后仍可能有重试/压缩/排队续跑）。

## 5. 待补测

- Docker / E2B 沙箱内 `PI_CODING_AGENT_DIR` 与凭据注入（task-6 / task-7）。
- pi-web-access 在配置目录重定向后是否从 `PI_CODING_AGENT_DIR/web-search.json` 读取凭据（task-5）。
