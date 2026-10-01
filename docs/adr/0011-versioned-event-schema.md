# 0011 产品事件契约为单一来源、带版本的 schema

- 状态：已接受（2026-09-28，架构重设计 P2）
- 范围：`host/glink_harness/event_schema.py`、`events.py`、`kernel/projection.py`、`kernel/acp_projection.py`、
  `scripts/gen-event-schema.py`、`web/modules/{event-schema.gen,run-reducer,run-stream}.mjs`

## 背景

事件类型与 payload 字段分散在宿主投影、runner 与前端流处理里，各自按约定读写；字段漂移只在前端
渲染异常时暴露，前端流处理同时承担订阅、状态折叠与 DOM，无法用 fixture 独立验证。

## 决定

1. `event_schema.py` 声明全部事件类型、payload 字段与按类型的版本 `v`；`events.py` 的常量导入时
   与之核对。信封形状固定为 `{type, v, runId, seq, ts, payload}`。
2. `EventBus.commit` 在分配 `seq` 前校验；两族投影经共享 `checked` 校验自身产出。
   `GLINK_EVENT_SCHEMA=strict`（测试套件默认）时违规抛错、不落盘不耗 `seq`；生产模式记错误日志
   后仍提交。
3. 前端 schema 由脚本生成（`event-schema.gen.mjs`，`check.sh` 以 `--check` 防过期）；前端拆为纯
   `reduce(state, event)`、订阅层与视图，reducer 只应用 `v` 与当前版本严格相等的帧。
4. 改变某类型 payload 含义且旧读者会误读时提升该类型 `v`，同步 reducer。

## 不兼容

schema 之前写下的事件没有 `v`，当前前端不渲染（计入 `rejectedFrames`），不做迁移。
