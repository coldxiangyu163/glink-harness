# GLINK-ACP-15：阶段诊断与待裁决方案

> 本文保留批准前的阶段事实；Lead 后续已批准副本兼容例外，实现与最终门禁见 [交付证据](acp15-delivery.md)。

## 结论

`71779b32e35a` 的恢复状态可在本机真实 dsh、零 prompt/零模型请求下复现同一个 `session/resume -32603`。根因是旧 Jina bundle 写入的 **`web/jina-fetch-request` 未标记 `ignorable`**，不是空 `session.lock`。父 session 首个拒绝点为 seq 43。

当前源码已撤销该审计事件（`docs/acp-integration-notes.md` §19.6），但旧会话仍包含它。该节明确规定旧会话修复需另行决定。因此本阶段仅在临时副本做因果实验，**尚未实现宿主修复，尚未满足最终验收**。

## 范围、环境与安全

- 分支 `glink-acp-15`；基线/诊断时 HEAD `d1766adacfdd1159b51d5fd5af2f3ef48405dad4`，开工工作区干净。
- 当前 worker：brief 指定入口 `/Users/coldxiangyu/.local/bin/pigpt`；该 launcher 参数与当前环境相符：provider `openai-codex` / model `gpt-6.1-sol` / thinking `xhigh`。worker 由 Lead 启动，本 worker 未再启动或委派 agent。
- Python：项目依赖解释器 `/Users/coldxiangyu/AI/git/pi-glink/.venv/bin/python`；Node `v24.16.0`。
- 真实 agent：已缓存的 `@deepseek-ai/dsh@0.1.5-rc.3`（`~/.npm/_npx/99cabb0ceac85b86/node_modules/@deepseek-ai/dsh/lib/bin.js`），以 `node <绝对路径> --profile acp` 直接执行，不经 npx 网络安装。
- agentInfo：`deepseek-harness-acp` / `0.0.1`；协商声明 `sessionCapabilities.resume: {}`。
- 将**整个**只读真实数据根 `/tmp/glink-e2btest-dsh` 复制到 `/tmp/glink-acp15.l22MjJ/data`，后续实验只读这个副本、写各自独立 HOME。原始数据根、7872 服务及操作员配置均未修改。
- 每个实验从 `data/runs/71779b32e35a/acp-agent/recovered` 独立复制 HOME；剔除复制品里的凭据目标与 Linux 专属 profile，使缓存 dsh 生成自己的本地 ACP profile。保留完整 sessions 与 projcache；所有差分用同一启动配置。
- 子进程环境显式只交 `PATH`、临时 `HOME` 与临时 XDG 根；不交模型/检索凭据。请求只有 `initialize` 与 `session/resume`，**没有 `session/prompt`**，无需任何真实模型调用。
- 工作目录为临时目录，resume 参数 `cwd=/home/user/workspace`、`sessionId=0e848302-a564-43e4-9f6d-7903d2827273`、`mcpServers=[]` 与恢复会话一致；无需创建或修改本机 `/home/user`。
- 全部 agent 通过关闭 stdin 自行退出 0，`hostStopped=False`，无 stderr，无本任务遗留进程/服务/云资源。临时目录含真实运行数据副本，保留 0700 根用于核验，**不入仓、不传播**。

## 红绿证据

探针：`/tmp/glink-acp15.l22MjJ/repro.py`（临时调查工具，非交付测试）。实际运行形式：

```bash
PYTHONPATH="$PWD/host" /Users/coldxiangyu/AI/git/pi-glink/.venv/bin/python \
  /tmp/glink-acp15.l22MjJ/repro.py <case> > /tmp/glink-acp15.l22MjJ/<case>.log 2>&1
```

退出状态逐条记录，没有以输出截取或管道覆盖非零退出码：

| case / 日志 | 唯一状态差分 | 原始退出码 | 结果 |
|---|---|---:|---|
| `baseline2` | 无，4 个空 lock 保留 | **1（预期红）** | `-32603`；`web/jina-fetch-request` seq 43 unknown/not ignorable；0.874s |
| `no-lock` | 仅删除 4 个复制品 `session.lock` | **1（预期红）** | 完全相同的事件类型与 seq 43 拒绝；0.928s |
| `ignorable3` | 仅给该类型添加顶层 `ignorable: true`，4 个 lock 保留 | **0（诊断绿）** | resume 返回 `configOptions`；2.718s |

代表性原始输出（只截取诊断相关字段；完整脱敏协议摘要在上述临时日志）：

```text
resume FAIL session/resume failed: -32603: Internal error
(session "0e848302-a564-43e4-9f6d-7903d2827273" contains event type
 "web/jina-fetch-request" (seq 43) unknown to this harness and not marked ignorable;
 refusing to interpret the log ...)
```

```text
only ignorable annotation; rows 135 locks remain 4
resume PASS response keys ['configOptions'] elapsed 2.718
agent exit 0 hostStopped False
stderr tail []
```

绿实验给全部 4 个 v3 日志中的该类型加标记（总计 135 条），保留 type/seq/time/data 及其他字段，不删除或重新编号事件；只重新压缩副本。它只证明诊断可行，**不是已实现的生产恢复链路**，未发 prompt，也未宣称模型配置链或 turn 开始已通过。

调查工具的中间错误单列，均未伪装成原机制红测：

- 首次 `baseline` 捕获到同一真实 `AcpRequestError`，但摘要代码错用 `SecretRedactor.text`，原退出码 1；改为 `.redact` 后用 `baseline2` 完整复验。
- `ignorable`：Python zstandard 不接受 Path 作为 stream_reader 输入，退出 1；改为字节输入。
- `ignorable2`：把头和事件重新压成同一帧，dsh 正确拒绝 `first frame is not exactly one header line`，退出 1；改为头单独一帧、后续逐行独立校验和帧，得到 `ignorable3` 退出 0。

## 候选裁决与 ACP-13 差异

1. **C1 排除**：仅删 lock 无改善；加 ignorable 后保留 lock 可恢复。目标安装的 `dsh-session-persistence-jsonl/lib/index.js` 的 `SessionWriteLease` 与只读参考仓库 `packages/session/session-persistence-jsonl/src/lease.ts` 均说明锁为 POSIX `flock(2)`，文件常驻，句柄关闭/进程死亡才释放内核锁，空文件不是 PID 锁。**不应新增删锁逻辑**；删除活锁可能破坏 inode 排他。
2. **新候选 C6 成立**：必需的未知 durable 审计事件让冻结词表拒绝回放；仅改变此 envelope 标记即可绿。
3. **C2/C3 非该失败所需原因**：同样的 cwd、sessions 和 projcache 不变，标记事件即可恢复；不能据此声称所有 cwd/缓存异常均不存在。
4. **C4 非原拒绝原因**：resume 尚未返回时就失败，发生在 `_apply_session_config` 之前；后续修复仍需叠加 DSH-03 模型应用回归。
5. **C5 的具体差异**：ACP-13 真实 inproc/e2b 成功记录也明确包含 `session.lock`，父轮为 `finished/end_turn`，故锁存在和结束方式均不能解释差别。失败恢复状态含后来 Jina bundle 写出的未知审计事件；§19.6 已撤销该生产行为，但旧状态未变。

## 需 Lead 裁决的最小方案

现有契约默认不改写 ACP 状态字节，§19.6 又明确既有会话处理另行决定。建议限定宿主的**续接目标副本**，不修改父 run、对话权威归档或任何真实历史：

- 仅针对 dsh v3 `.dsh/sessions/<cwd>/<session>/session.v3.jsonl.zstd` 的已知退役 `web/jina-fetch-request` 类型，添加顶层 `ignorable: true`；不删事件、不改 seq/sourceEventSeqs、不改 payload。
- 覆盖本地继承、冷沙箱 files/归档回灌与 reboot 热路径；统一在新 agent spawn 之前处理目标状态。不得在有运行中 agent/活 writer 的 HOME 上改写。
- 使用已有无链接路径纪律、大小/数量界限及原子发布；未知类型、非 v3、损坏日志继续 fail closed。不将任何未知事件泛化为 ignorable，不 patch/fork dsh。
- 故障回归先红后绿，保护含 4 个 lock 的合法输入与未知事件拒绝，并叠加真实 fake agent → resume → model/reasoning 配置 → prompt/turn 链；codex/opencode 不受影响。
- 架构最小同步需声明这是已知退役**宿主自有审计**的副本兼容例外；修改 `acp-integration-notes.md` 按 brief 先报 Lead。

尚待决定：是否授权此副本兼容例外、实施是否覆盖热路径，以及是否接受只添加 ignorable 而不删除审计事件。未授权前停止实现该语义变化；`bash scripts/check.sh` 尚未运行，E2B live 挂账。
