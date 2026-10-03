# 架构决策记录（ADR）

本目录记录改变宿主契约的决定及其来由。代码注释与 `docs/architecture.md` 只描述现行契约；
「以前怎样、为什么改」写在这里。每份 ADR 一个决定，编号递增，已接受的决定不改写，被替代时
新写一份并在旧文件顶部注明「已被 NNNN 替代」。

| 编号 | 决定 |
|---|---|
| [0001](0001-required-run-admission-facts.md) | run 记录的准入事实必填，旧数据不保证可读（记录形状已被 0010、读取与裁决口径已被 0009 替代） |
| [0002](0002-follow-up-judged-by-parent-record.md) | 追问门禁与状态根按父记录自身事实判定 |
| [0003](0003-acp-credential-copies-removed-at-close-out.md) | 注入凭据副本在收尾时删除（pi 部分已被 0008 替代） |
| [0004](0004-pi-e2b-rpc-convergence.md) | pi 的 E2B 面收敛到 RPC |
| [0005](0005-acp-e2b-follow-up-via-recovered-state.md) | ACP E2B 面追问经状态根回收后受理 |
| [0006](0006-child-progress-observer-backs-off.md) | 子代理进度观察失败只降频不停止 |
| [0007](0007-report-served-only-from-host-artifact.md) | 报告只认宿主产物副本 |
| [0008](0008-egress-proxy.md) | 真实模型/检索 key 只留在宿主出口代理，执行环境只拿短期 token |
| [0009](0009-sqlite-run-store.md) | run 记录、事件与资源台账迁入 SQLite；终帧与终态同一事务，旧数据不可读 |
| [0010](0010-harness-sandbox-runspec.md) | Harness × Sandbox 正交组合，RunSpec 为准入冻结点；顶层三键旧记录不可读 |
| [0011](0011-versioned-event-schema.md) | 产品事件为单一来源、带版本的 schema；无 `v` 的旧事件不渲染 |
| [0012](0012-pack-manifest-v2-single-source.md) | harness 包清单 v2 为唯一真源，镜像/模板参数由清单生成；v1 与旧 ACP 变量不再读取 |
| [0013](0013-run-lifecycle-pipeline.md) | run 收尾是显式的八阶段管线，秘密边界与子代理插件独立 |
| [0014](0014-delivery-gate-lint-types-coverage.md) | 交付门禁加入 ruff 静态检查、mypy 类型检查与覆盖率总量 ratchet（dev-only 依赖） |
| [0015](0015-acp-credential-channels-under-model-credential-policy.md) | ACP 凭据通道视同模型凭据，服从 tenant/shared 政策；shared 按族分流冻结清单 |
| [0016](0016-acp-credential-env-single-source.md) | ACP 凭据与 dsh 主模型、effort 统一到 `.env`；包仅保留结构，new/resume 应用当前部署值并核验实际状态，组合验证归部署验收 |
| [0017](0017-declared-deliverables.md) | ACP `present` 声明 → 安全回收多文件 → 宿主副本下载与交付卡片；主报告仍遵循 0007 |
| [0018](0018-conversation-state-archive-channel.md) | 宿主持有对话级权威状态，E2B 面回收/回灌走归档通道（增量 tar 流），继承阈值改为异常检测 |
| [0019](0019-acp-state-bounds-warning-tier-and-adaptive-resume.md) | 状态上限三分支（警告档不拒、fail-closed 才拒），resume 超时按状态根体量自适应，继承/回灌/回收与 resume 耗时进事件流 |
| [0020](0020-conversation-sandbox-lease-hot-path.md) | 对话沙箱租约：收尾泊车（pause）而非销毁，追问热路径重连原沙箱（代数双端一致才命中，失败降级冷路径），TTL 对账销毁与删除最小闭环；租约不占 run 槽位 |
| [0021](0021-event-fixture-contract.md) | 跨消费方一致性 = 权威事件样例库（golden fixtures）+ 消费方 fold 契约测试（不共享 fold 代码）；ULegal trajectory 定位为业务里程碑+展示 fold，delta/直播帧不落盘不投影，事件原文存档归 R3 |
