# 0019 上限语义的警告档、自适应 resume 超时与观测补全

- 状态：已接受（阶段 1 收尾；背景与裁决见任务简报，方案 cc 拟、用户批准）
- 范围：`kernel/acp_env.py`（警告线与继承 outcome）、`kernel/acp_execution.py`
  （自适应 resume 超时、阈值警告帧、观测字段）、`kernel/acp_projection.py`
  （警告帧投影）、`tests/test_kernel_acp_thresholds.py`（新）
- 关联：[0018](0018-conversation-state-archive-channel.md)（异常检测语义的第一步，本文补其收尾）、
  [0005](0005-acp-e2b-follow-up-via-recovered-state.md)（回收失败不受理的语义，fail-closed 档继承）

## 背景

[0018](0018-conversation-state-archive-channel.md) 把 INHERIT/UPLOAD 上限从「会话寿命配额」
（旧值 4096 文件 / 单文件 8MB / 总量 64MB——dsh 会话 15-30 轮即撞限，断崖=追问被拒）抬到
异常检测量级并改为命中即拒。收尾裁决要求该语义完整落地为**三分支**：超过正常量级先警告
（可见、可呈现、不拒），只有真正异常量级才 fail-closed；同时 resume RPC 超时写死 60s
（`REQUEST_TIMEOUT_SECONDS`）不再成立——dsh 的 `session/resume` 要读取并解压整个状态根
（zstd 帧链），状态根经归档通道放大后 60s 会在解析完成前先超时，读起来像一次拒绝。

## 决定

1. **三分支**。继承（`inherit_acp_state`）、回收（`recover_state`，含归档与 files 两条路）、
   回灌（`backfill_state`）共用同一判定 `state_threshold_warning`：

   | 档 | 判定 | 行为 |
   |---|---|---|
   | 正常 | ≤ 警告线 | 无声通过 |
   | 警告 | 警告线 < 量 ≤ fail-closed 线 | 拷贝/发布照常完成，发 `kernel_status`（warning，`acp_state_threshold_warning`），前端可呈现；追问照常受理 |
   | fail-closed | > `INHERIT_MAX_*` / `ARCHIVE_MAX_BYTES` | 整次拒绝（继承抛 `AcpStateInheritError`；回收不发布、追问按新鲜度门禁拒收；通道中止连接） |

   数值与依据：dsh 实测 ~420KB/轮线性（835KB/2 轮），50 轮 ≈ 21MB、数百文件。
   警告线取正常上界的数倍——总量 64MB、单文件 32MB、文件数 2048：越过它说明已偏离
   正常研究行为（失控缓存、异常会话文件），但离 fail-closed 线（总量 256MB 对齐通道
   传输上限、单文件 128MB、文件数 16384，见 0018）还有一个量级，拷贝/传输仍可完成。
   两条线的关系（warn < max）由测试钉住；任一数值变更须重新给出依据。

2. **警告的三处来源**：宿主侧继承（inproc/docker 面，`AcpRuntime.inherit_facts`）、
   归档通道回灌（e2b 面，`backfill_facts`）、回收（`AcpStateRecovery` 的 files/bytes）。
   adapter 在 agent 启动时（继承/回灌）与 `recover_artifacts`（回收）各发一次帧，投影为
   `kernel_status`；`detail` 携带来源标签（`[inherit]`/`[backfill]`/`[recovery]`）与量值，
   不新增事件类型、不扩 `kernel_status` 的 payload schema。

3. **resume 超时按状态根体量自适应**（`resume_timeout_seconds`）：
   `60s 保底（=REQUEST_TIMEOUT_SECONDS，协议自身开销） + 体量 / 4MiB/s（悲观解析吞吐，
   比 zstd 裸解压低一个量级，覆盖索引/回放重建）`，硬上限 600s（对齐
   `ARCHIVE_COMMAND_TIMEOUT_SECONDS`：一个方向的传输预算就是 resume 重解析预算的上界）。
   体量输入 = resume 将重解析的树：inproc/docker 为继承后的 run HOME，e2b 通道面为
   回灌体量 + HOME（`_tree_bytes` 只读元数据，链接不跟随，文件数受继承界约束）。
   `session/load`（回放式续接）同用此预算。处于 fail-closed 界内的最大根（256MB）只要求
   ~124s，硬上限纯为防御。

4. **观测补全**（`session_ready` 的 open payload，与 0018 的 `stateRecovery`/`stateBackfill`
   同一处）：
   - `stateInherit`：继承的 files/bytes/durationMs/warning（inproc/docker 面的"传输"）；
   - `resumeDurationMs`：续接 RPC 的墙钟耗时 = agent 端重解析/回放重建，与传输耗时
     （`stateInherit`/`stateBackfill` 的 durationMs）分开测量，事件流可区分。

## 被替代的原义务

- 0018 第 4 条「达到阈值即 fail-closed 拒绝」的单一档：被三分支取代——fail-closed 线与
  数值不变，其下新增警告档；「超限事实进事件流」从 fail-closed 时的失败记录扩展为
  警告档的主动 `kernel_status`。
- 更早的「会话寿命配额」语义（旧值 4096/8MB/64MB 的存在理由——每 run 一次性沙箱下把
  回收/回灌成本钉在 files API 预算内）已由 0018 废除，本文不再保留其数值。
- resume RPC 使用固定 `REQUEST_TIMEOUT_SECONDS`（60s）的隐含假设：自适应预算取代之，
  保底值即原值，其余请求（`initialize`/`session/new`/`set_config_option`）仍用固定预算。

## 数据模型

- `kernel/acp_env.py`：`INHERIT_WARN_{FILES,FILE_BYTES,TOTAL_BYTES}`；
  `AcpStateInheritOutcome{secrets, files, bytes, largest_file, warning}`（`inherit_acp_state`
  的返回，取代裸 secrets tuple）；`state_threshold_warning(files, total_bytes,
  largest_file=None)`；`AcpRuntime.inherit_facts`。
- `kernel/acp_execution.py`：`RESUME_TIMEOUT_{BASE_SECONDS,BYTES_PER_SECOND,MAX_SECONDS}`、
  `resume_timeout_seconds(state_bytes)`、`_tree_bytes(root, max_files)`；
  帧 `acp_state_threshold_warning{source, detail}`；`session_ready` 载荷新增
  `stateInherit`、`resumeDurationMs`。
- 事件 schema 不变（警告走既有 `kernel_status` 的 level/message/detail）。

## 理由与残留

- 警告线的三档不按来源分档：继承/回收/回灌的物理介质不同（本地拷贝、tar 流、files 逐
  文件），但"多大量算异常"是会话行为问题，不是介质问题；分档只增加解释成本。
- `_tree_bytes` 是元数据统计（不读内容），计入的是 st_size 而非压缩后体量——dsh 会话
  文件本身就是 zstd 数据，磁盘尺寸即重解析输入，无需再估。
- 4MiB/s 是悲观测量假设而非实测值：真实 dsh 解析吞吐未测（需要 live 验收），悲观取值
  保证预算只多不少；`resumeDurationMs` 进事件流正是为了后续用观测修正该常数。
- 归档通道的 `ARCHIVE_MAX_BYTES`（256MB）保持 fail-closed：它是传输介质的物理上限，
  不是行为判断；警告线低于它才有"先警告、后拒绝"的完整三档。
