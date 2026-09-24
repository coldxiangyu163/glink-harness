# GLINK-SUB-01 spike：pi-subagents 的 async/前台配置面

本目录是 `GLINK-SUB-01` 的有界验证产物：判断 `pi-subagents@0.40.0` 是否存在**配置/设置开关**
（`extensions/subagent/config.json` 或 pi `settings.json`，非 prompt 约束）可以强制只走前台
同步执行，并关闭 scheduledRuns / intercom / watchdog / lsp 等非必需面。

## 结论（BLOCKER）

**不存在这样的开关。** 配置面只能把 launch 推向 async（`asyncByDefault`、`forceTopLevelAsync`），
不能禁止模型请求的 `async: true`；也没有环境变量 kill switch。因此：
模型只要在 `subagent` 工具调用里传 `async: true`，就一定会走 detached 子进程路径
（`src/runs/background/async-execution.ts:454` `detached: true`，`env: {...process.env, ...}`），
配置无法拦截。按 brief 的门禁，本 spike 在 Q1 停止，未执行取消传播、事件投影/脱敏、
产物目录与容器安装等 live 验证（见 `docs/evidence/pi-subagents-spike.txt`）。

非必需面里**可以**关闭的部分（静态确认，供后续方案参考）：

| 面 | 默认 | 开关 |
|---|---|---|
| scheduledRuns | 关闭 | 需显式 `{ "scheduledRuns": { "enabled": true } }` 才启用 |
| watchdog | 关闭 | `DEFAULT_WATCHDOG_CONFIG.enabled: false`（LSP 诊断受 watchdog 约束） |
| intercomBridge | 未配置时解析为 `always` | `{ "intercomBridge": { "mode": "off" } }` |
| artifactDir | `project`（`cwd/.pi-subagents`） | 可配 `session` / `temp` |

## 复现

仓库外临时前缀安装（不污染仓库、不动全局 pi / `~/.pi`）：

```bash
npm install --prefix /tmp/glink-sub-spike/prefix pi-subagents@0.40.0 --no-audit --no-fund
```

静态审计（只读、离线；不读取个人 pi 配置）：

```bash
bash scripts/spikes/pi_subagents/audit_async_switch.sh \
  /tmp/glink-sub-spike/prefix/node_modules/pi-subagents
```

退出码：

* `0`：未发现“禁止 async / 强制前台”的配置键或环境变量（0.40.0 的实际结果）；
* `1`：发现疑似开关（审计脚本内置该负例：临时 fixture 里的 `disableAsync?: boolean` 会红）；
* `2`：用法错误或目录结构不符（上游结构变化时的人工介入信号）。

负例 fixture（证明检查器不是永远绿灯）：

```bash
fx=/tmp/glink-sub-spike/negative-fixture
rm -rf "$fx" && mkdir -p "$fx/src/shared"
printf '{ "name": "pi-subagents", "version": "9.9.9-negative-control" }\n' > "$fx/package.json"
printf 'export interface ExtensionConfig {\n\tdisableAsync?: boolean;\n}\n' > "$fx/src/shared/types.ts"
bash scripts/spikes/pi_subagents/audit_async_switch.sh "$fx"   # 应退出 1 并打印 FOUND: disableAsync
```

审计脚本会打印 `ExtensionConfig` 全部键、模型可见的 `async` 参数、前台/异步分派逻辑、
非必需面默认值，并把任何新增的 async/foreground/detach/sync 命名键标为可疑。

## 为什么没有 mock 服务与 live 驱动

brief 的门禁规定：没有可靠开关即标 BLOCKER、停止后续深挖。禁用 async 是后续
（凭据继承、取消传播、事件投影、产物目录）验证的前提，因此没有构建 mock 模型服务。
若后续改用 pi-glink 自有扩展（`tool_call` 钩子）强制改写 `async: false`，再补
loopback 合成 OpenAI 兼容 mock、临时 `PI_CODING_AGENT_DIR` 与 RPC 驱动。
