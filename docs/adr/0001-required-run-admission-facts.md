# 0001 run 记录的准入事实必填，旧数据不保证可读

- 状态：已接受（2026-09-28，架构重设计 S1）；记录形状已被 [0010](0010-harness-sandbox-runspec.md) 替代（`spec` 对象），读取与裁决口径已被 [0009](0009-sqlite-run-store.md) 替代
  （记录迁入 SQLite，缺字段的行 fail-closed，不再计入不可读持有）
- 范围：`host/pi_glink/store.py`（`RunRecord`、`RunStore.create`、`_load_meta`）

## 背景

`harness`、`harness_pack`、`executor` 是准入时由冻结配置确定、与 run id / owner 同一次原子写入
`meta.json` 的事实。它们最初作为可选字段加入：早期记录没有这些键，追问门禁、ACP 能力扫描与状态根
推导因此各自保留了「字段缺失时」的回退分支（帧内 `harness` 标记证明族、按宿主当前执行面推导状态根）。
这些分支让同一决定有两条证明路径，并且回退路径会随宿主配置漂移。

## 决定

- 三个字段在 `RunRecord` 与 `RunStore.create` 上都是必填的非空字符串。
- 读取时缺任一字段（或值不是非空字符串）的 `meta.json` 按**不可读**处理：不出现在 `get`/`list`
  中，并与损坏的 meta 一样作为不可读目录计入全局资源持有，直到人工裁决（`adjudicate_unreadable`）。
  不猜默认值、不写迁移。
- 用户授权「不考虑兼容旧内容」：本次重设计起旧 run 数据不保证可读。

## 后果

- 追问门禁、`recorded_capabilities`、`state_home` 只有一条判定路径（见 0002）。
- 升级后首次启动的宿主若遇到旧记录，会因全局持有而拒绝新 run，直到运维对这些目录逐个裁决或移走；
  这是 fail-closed 的有意代价。
- 回归：`tests/test_store.py::test_meta_missing_an_admission_fact_is_unreadable`。
