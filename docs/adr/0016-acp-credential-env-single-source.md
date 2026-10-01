# 0016 ACP 凭据与 dsh 模型的 .env 单源配置

- 状态：已接受（2026-09-29）；2026-09-30 经 GLINK-DSH-03 修订决定 5
- 范围：`host/glink_harness/config.py`（`GLINK_ACP_CREDENTIAL_ENV` 解析、政策接入
  `_acp_credentials_follow_policy`）、`host/glink_harness/kernel/acp_env.py`
  （`_render_credential_env`）、`host/glink_harness/harnesses.py`（结构来源声明）、
  `host/glink_harness/kernel/acp_session_config.py`（ACP 应用与实际值核验）、
  `harnesses/dsh/harness.toml`、`harnesses/dsh/home/.dsh/settings.yaml`（provider 结构）、
  凭据与模型回归、`.env.example`、`README.md`

## 背景

`GLINK_ACP_CREDENTIAL_FILES`（ADR 0015 之前即存在的文件注入通道）要求操作员维护一份
与本地 dsh 生态并行的凭据文件（如 `~/.dsh/.credentials.glink.yaml`）。实践暴露两类问题：

- **双份漂移**：同一把 key 在 `.env`（pi 族用）与注入文件（ACP 族用）各存一份，轮换要改
  两处（2026-09-29 实测：OpenRouter key 在 `.env OPENAI_API_KEY` 与
  `.credentials.yaml refs.OPENROUTER_API_KEY` 重复）；
- **粒度过粗**：整文件注入把沙箱不需要的 key（Lark/OpenCode 等）一并带进明文可见面。

原决定 5 将部署模型写入仓库 overlay，依据是尚未验证的假设：
`settings.yaml` 的 `agent-default-model` 会控制 `dsh --profile acp` 创建的会话。
2026-09-30 实测证伪：三个 run 的实际 `currentValue` 都是
`["deepseek-official","deepseek-v4-flash"]`，并非 overlay 声明的 GPT。
dsh 的 acp-app bundle 为每个 ACP-created agent 提供 fallback selection，
`agent-default-model` 在这条路径没有选择机会。
已存在的 ACP `session/set_config_option` 才是控制会话模型的通道。

## 决定

1. **凭据单源在 `.env`**：新增 `GLINK_ACP_CREDENTIAL_ENV="KEY_A,KEY_B"`（逗号分隔的
   环境键名）。run 准备时宿主从配置值渲染一份 dsh 格式的
   `.dsh/.credentials.yaml`（`version: 1` + 平面 `refs` 映射）进该 run 的 HOME：
   0600、`O_NOFOLLOW`、父目录 owner-only——与文件注入同一条投递纪律；渲染副本登记为
   credential destination，收尾删除（决定 C，§18.4），值并入该 run 的脱敏边界。
   **删除义务覆盖准备失败路径**：`prepare_acp_runtime` 在渲染之后的任何失败（状态继承
   失败、族级 session_seed 拒绝）都会先就地删除已投递的副本再抛错——失败的 run 不进
   收尾管线，渲染方自己是唯一的清理点（`discard_rendered_credential_copies`，与
   ADR 0003 的收尾删除同一套「只删已知相对路径、不跟随链接、失败不遮蔽原异常」纪律）。
   同理，该通道只在 dsh 包下生效（非 dsh 的 ACP 包与 pi 包配置即拒绝启动，不写没人读
   的凭据文件）。
2. **与 `GLINK_ACP_CREDENTIAL_FILES` 互斥**：两者都往 run HOME 投递凭据，一次部署
   只选一个来源，同时配置拒绝启动。FILES 保留为逃生通道（非 dsh 形态的凭据文件）。
3. **fail-fast**：声明的键名必须是环境变量形态；声明了但 `.env`/环境里没有值 → 拒绝
   启动，绝不渲染空凭据。值存 `HostConfig.acp_credential_env_values`（`repr=False`），
   不进日志与 repr。
   **进程环境优先**：`load_config` 的进程环境白名单只放行宿主自有键，因此**显式声明**的
   凭据键另按「进程环境优先于 `.env`」继承（只在进程环境提供时可启动；轮换时导出的新值
   覆盖 `.env` 旧值），未声明的无关变量仍被过滤。原实现把声明键一并过滤，导致只靠进程
   环境注入时拒绝启动、`.env` 留旧值时静默沿用旧值。
4. **政策对齐（ADR 0015 的第三通道）**：`_acp_credentials_follow_policy` 把 ENV 通道与
   另两通道同列——multitenant + `tenant` 整通道拒绝；multitenant + `shared` 视为具名
   共享凭据源（缺源不启动）；单用户模式照常可用。
5. **部署可变值统一到 `.env`**：主模型复用 `OPENAI_MODEL`，reasoning effort 新增
   `OPENAI_REASONING_EFFORT`，两者没有 dsh 默认值，声明接线但缺值即拒绝启动。
   进程环境优先于 `.env`，启动后冻结，经 `HostConfig` 随 `ExecutionSpec` 传递。
   包只保留结构：`harness.toml` 的 `session_config` 声明 config id、`source` 与编码规则；
   `settings.yaml` 删除无效的 `agent-default-model`，保留 `llm-pi-ai.providers.apiKeyEnv`。
   pi 的模型 id、thinking 策略与凭据通道保持原语义，新增 effort 键不改变它们。

   dsh 的广告值来自 `modelValue()` 的 `JSON.stringify([provider, model])`。
   宿主先按完整模型 id 唯一匹配广告路由，歧义时可用 `provider/model` 全路由消歧，
   不按首个斜杠拆模型 id，也不自行拼造 opaque ACP 值。
   new/resume 后先切模型，再从更新后的广告集选择 effort；未广告选项、无唯一路由、
   非法值、setConfig 未返回实际状态或 `currentValue` 不等于声明值，都在 prompt 前失败。
   每次变更也复核此前应用的值，防止 effort 选择撤销模型选择。已匹配则不重复发送请求。

   **resume 应用当前配置**：续接按当前部署的冻结声明切换模型与 effort，保证部署一致性。
   父 run 的实际值、事件与报告不回改；本轮记录 agent 核验后的实际值。
   **验证义务调整**：包版本与模型组合的验证从包版本验收转到部署验收。
   包升级仍需协议与结构回归，部署修改模型或 effort 后须核验真实会话实际值与模型质量。
   fake ACP 只证明接线和 fail-closed，不代替真实部署验收。

## 边界

- 渲染格式是 dsh 的 credentials 形状；其他 ACP 包若需要不同文件形态走 FILES 通道
  （模型路由依 agent 广告的 ACP 选项解析，不从 dsh settings 猜实际模型）。
- 本机 `~/.dsh/`（Lark、web profile、DeepSeek 直连）与沙箱 HOME 是两个家，互不影响；
  唯一同步物是 `.env` 里的值。
- 子代理模型继承核验、前端期望与实际展示、历史报告重算不在 GLINK-DSH-03 范围。
- 本修订不授权重启服务、构建镜像或 E2B 模板，也不声称既有服务已应用新模型。
