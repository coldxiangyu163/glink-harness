# 0010 Harness × Sandbox 正交组合，RunSpec 为准入冻结点

- 状态：已接受（2026-09-28，架构重设计 S2；部分替代 [0001](0001-required-run-admission-facts.md) 的记录形状）
- 范围：`host/glink_harness/kernel/{sandbox,harness,harness_pi,harness_acp,execution,acp_execution}.py`、
  `store.py::RunSpec`、`runs/admission.py`、`tests/test_repository_boundaries.py`

## 背景

执行面由 `execution.build_adapter` 按 `(harness, executor)` 字符串对分派，族上另挂 plan 回调，
Docker plan 有两份；run 记录把「用什么执行」写成顶层 `harness` / `harness_pack` / `executor` 三个键，
runner 直接导入具体族与执行面模块。新增一个族或执行面要在多处同时加分支。

## 决定

1. 两个维度各一处实现：**Sandbox**（`kernel/sandbox.py`：`InprocSandbox` / `DockerSandbox` /
   `E2BSandbox`，`open_sandbox(spec.executor, ...)` 选出）只管资源、传输、路径放置、回收与
   `CleanupReport`；**Harness**（`HarnessFamily`）只管 launch 描述、协议驱动、终态词汇、续接、
   子代理与 usage。组合只有 `driver(sandbox.spawn(launch(spec)))` 一条路径，不按执行面名字符串分支。
2. `store.RunSpec{family, pack, executor, blueprint, credential_origin}` 在准入时由冻结配置解析一次，
   与 run id / owner 同一事务写入记录的 `"spec"`；此后族、执行面、追问门禁与状态根继承只读 spec。
3. `runs/` 编排层只依赖抽象（`HarnessFamily` / `HarnessRuntime` / `FrameProjector` / `Sandbox` /
   `ExecutionAdapter`），由仓库边界测试的导入白名单卡住。

## 不兼容

顶层 `harness` / `harness_pack` / `executor` 形状的旧记录不再解析，无迁移（随后 0009 迁入 SQLite，
旧文件记录整体不再读取）。`execution.build_adapter`、族 plan 回调与重复 Docker plan 已删除。
