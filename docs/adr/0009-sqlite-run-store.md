# 0009 run 记录、事件与资源台账迁入 SQLite

- 状态：已接受（2026-09-28，架构重设计 P1；部分替代 [0001](0001-required-run-admission-facts.md) 的读取与裁决口径）
- 范围：`host/pi_glink/store.py`、`runs/admission.py`、`runs/resource_ledger.py`、`runs/runner.py`、
  `runs/stages.py`、`web_sse.py`、`kernel/harness_pi.py`（会话目录）、`config.py`

## 背景

此前每个 run 一个目录：`meta.json`（原子替换）+ `events.jsonl`（追加），另有进程内的提交水位
（`_WATERMARKS`）与 run 索引（`_INDEXES`）。终帧先 `fsync` 进日志，再把终态作为「落定副本」写回
`meta.json`；读者要在两份文件之间投影同一事实，崩溃留下的半行要补换行（torn-tail 修复），终帧后要
封口，索引要与全量扫描保持一致，准入在 manager 锁内按索引计数、与写入不在同一个原子单元。
这些机制都在弥补「两份文件、没有事务」这一个缺口，并且各自需要回归保护。会话文件另放在
`<数据根>/sessions/<id>/`，与 run 目录分离。

## 决定

1. run 记录与事件存入 `<数据根>/runs/runs.db`（标准库 `sqlite3`，WAL，`synchronous=NORMAL`）：
   `runs` 表一行一个 run（完整 `RunRecord` JSON + `created_at`/`tenant_id`/`status`/`holds` 查询列），
   `events` 表主键 `(run_id, seq)`。库文件以 0600 普通文件打开，拒绝符号链接；`user_version` 不符即拒绝打开。
2. **终帧即终态**：`commit_terminal()` 在同一事务里插入终帧并改写 run 行为终态，以 `synchronous=FULL`
   提交；已终态的行在同一事务内拒绝任何后续帧。`update()` 拒绝终态字段。
3. **准入与台账同一事务**：`create(admission_check=…)` 在 `BEGIN IMMEDIATE` 内读持有资源的行、做配额
   检查并插入新行；拒绝即回滚，不留行、不留目录。
4. 删除模块级 `_WATERMARKS`/`_INDEXES`、torn-tail 修复、日志封口、落定副本（`settle`）、`scan_all`、
   不可读目录计入持有与 `resource-adjudication.json` 侧车。行不可解析时读取一律抛 `StoreError`
   （fail-closed），由操作员修复数据库。
5. 报告、子代理产物与会话文件仍是文件；会话目录并入 `runs/<id>/sessions/`，
   `PI_CODING_AGENT_SESSION_DIR` 不再是宿主配置项（宿主只把它作为每 run 目录传给 pi）。
6. SSE 仍按 `seq` 续传（`Last-Event-ID`），重放从 `events` 表按 `seq` 分页读取。单机单宿主，不做多主机。

## 后果

- **旧数据不再可读**：`runs/<id>/meta.json`、`events.jsonl` 与 `<数据根>/sessions/<id>/` 不被读取，旧 run
  不出现在列表、详情、重放与资源台账中，也不能追问。不提供迁移。
- 0001 中「缺准入事实的记录作为不可读目录计入全局持有、经 `adjudicate_unreadable` 裁决」不再成立：
  缺 `spec` 或必填字段的行按不可解析处理（fail-closed，准入全部拒绝）。0001 的「准入事实必填」仍然有效。
- 回归：`tests/test_store.py`（终态原子性以触发器注入失败、并发 create/commit、准入事务、schema 版本、
  符号链接、旧目录不读、不可解析行）、`tests/test_event_replay.py`（提交返回前不可见）、
  `tests/test_resource_ledger.py::test_an_unparseable_row_fails_admission_closed`。
