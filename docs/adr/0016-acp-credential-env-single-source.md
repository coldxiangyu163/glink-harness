# 0016 ACP 凭据的 .env 单源渲染通道

- 状态：已接受（2026-09-29，本地部署 DX 收口）
- 范围：`host/pi_glink/config.py`（`PI_GLINK_ACP_CREDENTIAL_ENV` 解析、政策接入
  `_acp_credentials_follow_policy`）、`host/pi_glink/kernel/acp_env.py`
  （`_render_credential_env`）、`harnesses/dsh/home/.dsh/settings.yaml`（沙箱默认模型）、
  `tests/test_kernel_acp_env.py`、`tests/test_acp_credential_policy.py`、`.env.example`、
  `README.md`

## 背景

`PI_GLINK_ACP_CREDENTIAL_FILES`（ADR 0015 之前即存在的文件注入通道）要求操作员维护一份
与本地 dsh 生态并行的凭据文件（如 `~/.dsh/.credentials.glink.yaml`）。实践暴露两类问题：

- **双份漂移**：同一把 key 在 `.env`（pi 族用）与注入文件（ACP 族用）各存一份，轮换要改
  两处（2026-09-29 实测：OpenRouter key 在 `.env OPENAI_API_KEY` 与
  `.credentials.yaml refs.OPENROUTER_API_KEY` 重复）；
- **粒度过粗**：整文件注入把沙箱不需要的 key（Lark/OpenCode 等）一并带进明文可见面。

操作员侧还有一个直觉诉求：dsh 的部署偏好（默认模型、provider）应当像
`harnesses/pi/settings.json` 一样**版本化在仓库里**，而不是散在本机文件。

## 决定

1. **凭据单源在 `.env`**：新增 `PI_GLINK_ACP_CREDENTIAL_ENV="KEY_A,KEY_B"`（逗号分隔的
   环境键名）。run 准备时宿主从配置值渲染一份 dsh 格式的
   `.dsh/.credentials.yaml`（`version: 1` + 平面 `refs` 映射）进该 run 的 HOME：
   0600、`O_NOFOLLOW`、父目录 owner-only——与文件注入同一条投递纪律；渲染副本登记为
   credential destination，收尾删除（决定 C，§18.4），值并入该 run 的脱敏边界。
2. **与 `PI_GLINK_ACP_CREDENTIAL_FILES` 互斥**：两者都往 run HOME 投递凭据，一次部署
   只选一个来源，同时配置拒绝启动。FILES 保留为逃生通道（非 dsh 形态的凭据文件）。
3. **fail-fast**：声明的键名必须是环境变量形态；声明了但 `.env`/环境里没有值 → 拒绝
   启动，绝不渲染空凭据。值存 `HostConfig.acp_credential_env_values`（`repr=False`），
   不进日志与 repr。
4. **政策对齐（ADR 0015 的第三通道）**：`_acp_credentials_follow_policy` 把 ENV 通道与
   另两通道同列——multitenant + `tenant` 整通道拒绝；multitenant + `shared` 视为具名
   共享凭据源（缺源不启动）；单用户模式照常可用。
5. **部署偏好版本化**：dsh 的默认模型经 `harnesses/dsh/home/.dsh/settings.yaml`（overlay
   每 run 播种进沙箱 `~/.dsh/`）声明——`agent-default-model` + `llm-pi-ai.providers`
   的 `apiKeyEnv` 只是引用名，**值永远不进仓库**。与 `harnesses/pi/settings.json` 的
   地位对称：配置（无 key）在仓库、凭据（值）在 `.env`。

## 边界

- 渲染格式是 dsh 的 credentials 形状；其他 ACP 包若需要不同文件形态走 FILES 通道
  （宿主仍不理解 dsh settings 的语义——模型/provider 的 schema 归 dsh 自己）。
- 本机 `~/.dsh/`（Lark、web profile、DeepSeek 直连）与沙箱 HOME 是两个家，互不影响；
  唯一同步物是 `.env` 里的值。
