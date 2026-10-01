# 0013 run 收尾是显式的阶段管线

- 状态：已接受（2026-09-28，架构重设计 S4；行为不变的结构决定）
- 范围：`host/glink_harness/runs/{pipeline,stages,state,admission,secrets,plugins,channel,runner}.py`

## 背景

`RunManager` 一个类承担准入、启动、观察、回收、清扫、判定与终帧提交，收尾顺序义务（进度观察者先停、
清扫先于报告、判定在锁内冻结事实、终帧是线性化点）散在方法调用次序里，只能靠探针测试间接保护。

## 决定

1. 一个 run 依次经过 Admit → Prepare → Launch → Observe → Recover → Seal → Decide → Commit，
   次序由 `RunPipeline` 定义；每阶段只接收上一阶段冻结的事实（`runs/state.py`）。
2. `RunSecretBoundary` 收拢一个 run 的脱敏器、出口 token 授权、清扫根与注入凭据副本；收尾扫描只在
   Seal 阶段调用。
3. 子代理文档、进度与增强是生命周期插件（`on_committed` / `close` / `seal` / `settle`），不是 runner 方法；
   事件提交路径在 `RunChannel`。
4. `RunManager` 只保留公共 API 与准入锁。

## 不兼容

无外部契约变化；内部入口（旧 runner 私有方法）删除，测试改经阶段、边界与插件入口。
