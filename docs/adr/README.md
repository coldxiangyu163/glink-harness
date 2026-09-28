# 架构决策记录（ADR）

本目录记录改变宿主契约的决定及其来由。代码注释与 `docs/architecture.md` 只描述现行契约；
「以前怎样、为什么改」写在这里。每份 ADR 一个决定，编号递增，已接受的决定不改写，被替代时
新写一份并在旧文件顶部注明「已被 NNNN 替代」。

| 编号 | 决定 |
|---|---|
| [0001](0001-required-run-admission-facts.md) | run 记录的准入事实必填，旧数据不保证可读（读取与裁决口径已被 0009 替代） |
| [0002](0002-follow-up-judged-by-parent-record.md) | 追问门禁与状态根按父记录自身事实判定 |
| [0003](0003-acp-credential-copies-removed-at-close-out.md) | 注入凭据副本在收尾时删除（pi 部分已被 0008 替代） |
| [0004](0004-pi-e2b-rpc-convergence.md) | pi 的 E2B 面收敛到 RPC |
| [0005](0005-acp-e2b-follow-up-via-recovered-state.md) | ACP E2B 面追问经状态根回收后受理 |
| [0006](0006-child-progress-observer-backs-off.md) | 子代理进度观察失败只降频不停止 |
| [0007](0007-report-served-only-from-host-artifact.md) | 报告只认宿主产物副本 |
| [0008](0008-egress-proxy.md) | 真实模型/检索 key 只留在宿主出口代理，执行环境只拿短期 token |
| [0009](0009-sqlite-run-store.md) | run 记录、事件与资源台账迁入 SQLite；终帧与终态同一事务，旧数据不可读 |
