# 0003 注入凭据副本在收尾时删除

> pi 部分已被 [0008](0008-egress-proxy.md) 替代：pi 的运行期文件只含已吊销的出口代理 token，不再声明
> `credential_copies`，收尾不再删除。ACP 部分仍然有效。

- 状态：已接受（2026-09-27，GLINK-OBS-01 用户决定 C；详见 `docs/acp-integration-notes.md` §18.4、§18.5）
- 范围：`kernel/sweep.py::SessionSweep`、`runs/runner.py::_sanitize_session`、`_remove_credential_copies`

## 背景

此前宿主注入到每 run 状态根的凭据副本（ACP 的 `GLINK_ACP_CREDENTIAL_FILES` 目标、pi 的
`auth.json` / `web-search.json`）随 run 目录生命周期保留，收尾只扫描其值并入脱敏边界。

## 决定

run 收尾在只读扫描之后按运行时契约声明的 `credential_copies` 删除这些副本（ACP 的 `home` 与
`recovered` 两处，pi 的 `pi-agent/`）。只删已知相对路径本身，不做模式匹配、不跟随链接、必须在根内；
删除失败记入终帧 `artifactFailures` 并告警，不改变终态。

## 理由

追问从凭据源重新渲染（继承时已 skip 这些目标），副本收尾后没有用途；删除缩小 0600 明文在宿主磁盘
上的驻留面。运行中（内核仍在读）的副本仍存在，这段残留风险不变。
