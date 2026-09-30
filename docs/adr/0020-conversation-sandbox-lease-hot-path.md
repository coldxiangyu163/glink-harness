# 0020 对话沙箱租约与追问热路径

- 状态：已接受（阶段 2；背景与裁决见任务简报，方案 cc 拟、用户批准，主次关系：
  宿主权威状态为真相源，沙箱亲和只是热缓存）
- 范围：`kernel/conversation_lease.py`（新）、`kernel/acp_sandbox_park.py`（新）、
  `kernel/sandbox.py`（SandboxResume/E2B connect 面）、`kernel/e2b_env.py`
  （resume/kill-by-id 助手）、`kernel/acp_execution.py`（热路径分支与收尾泊车）、
  `kernel/harness_acp.py`（候选门禁）、`runs/stages.py`/`runs/runner.py`
  （候选判定接线与注入）、`runs/resource_ledger.py`（租约对账与删除闭环）、
  `store.py`（对话目录枚举）、`config.py`（TTL 旋钮）
- 关联：[0018](0018-conversation-state-archive-channel.md)（对话级权威状态与归档
  通道，本文的热路径以其降级链为冷路径）、
  [0005](0005-acp-e2b-follow-up-via-recovered-state.md)（回收受理语义，0018 已扩展）、
  [0015](0015-acp-credential-channels-under-model-credential-policy.md)/
  [0016](0016-acp-credential-env-single-source.md)（凭据纪律，本文的泊车形态）
- 替代义务：E2B 面 ACP run 收尾**一律销毁沙箱**的旧契约（0018 及之前）——
  归档通道部署下改为泊车；无通道部署行为不变。

## 背景

阶段 1 之后，E2B 面 ACP 追问的冷路径是「新沙箱 + 归档通道回灌 + `session/resume`」：
多轮长会话不再撞继承上限，但每轮仍付三段成本——新沙箱创建（云端冷启动）、
状态回灌（随会话线性增长）、agent 重解析（resume RPC）。裁决：追问优先
E2B `pause`/`resume` **原沙箱**（热路径），宿主权威状态仍是真相源，沙箱只是
热缓存；失败一律降级冷路径，不拒追问。

## 决定

1. **收尾泊车（pause 而非销毁）**。归档通道部署（`conversation_dir` 存在）的
   ACP/E2B run，在**状态回收成功且由本 run 自己发布**（manifest 的
   `sourceRunId`）之后泊车：先删沙箱内凭据副本（凭据不随沙箱持久化——
   销毁路径靠沙箱消亡，泊车路径必须显式删，0015/0016 的泊车形态），写状态
   代数标记，写租约，再 `pause(keep_memory=False)`。任一步失败 → 销毁沙箱
   （冷路径语义=旧行为），事实进终帧 `cleanup.lease`（`parked:false`+原因）。
   回收失败不泊车（沙箱销毁；追问门禁按 0018 的新鲜度校验照常拒绝）。
   无通道部署不泊车，行为与阶段 1 完全一致。
2. **`keep_memory=False` + `on_resume="reboot"`**。热路径的价值在文件系统亲和
   （状态根与工作区原样），不在进程存活：agent 的 stdio 无法跨 pause 重连，
   追问反正要新起 agent 进程再 `session/resume`。无内存快照则恢复必是磁盘
   冷启动，杜绝「宿主已停 agent，快照却复活旧进程」的双写风险。SDK 文档：
   控制面不支持 `reboot` 时要么拒绝（→降级冷路径，安全）、要么丢弃字段按
   restore 恢复——而 pause 时本无内存快照、且 agent 进程在 pause 前已被宿主
   停止，快照中无进程可复活。
3. **状态代数（generation）双端一致才走热路径**。每轮回收发布成功后对话代数
   +1（无租约从 1 起）；宿主把它写进两处——租约记录
   （`conversations/<根run>/lease.json`）与沙箱内标记文件
   （`/home/user/.pi-glink-state-generation`，在状态根**外**，回收/回灌不携带）。
   追问重连后读沙箱标记，与租约（对话根、代数）一致才认定热缓存；不一致
   （沙箱被外部替换/重置）→ 降级。候选门禁（`harness_acp.sandbox_resume_target`）
   另要求：租约由父 run 自己泊的（`runId` 匹配，与 0018 的 manifest 新鲜度同
   一事实的两面）、pack 与 E2B 模板匹配当前宿主、TTL 内且未禁用。
4. **降级一律冷路径，不拒追问**。connect 失败、代数不一致、沙箱消失：销毁
   租约沙箱（重连的 handle 先杀；无 handle 时按 id 销毁，SDK 的 not-found 契约
   视为已不在），删租约；销毁未确认则保留租约并**即刻过期**（对账下一轮重试）。
   随后走阶段 1 冷路径（新沙箱 + 回灌），发 `kernel_status` warning
   （`acp_sandbox_hot_path_degraded`）；命中热路径发 info（
   `acp_sandbox_hot_path`）且 `session_ready.sandboxResume` 携带结构化事实。
   热路径跳过回灌（沙箱即权威状态该代数的载体），凭据照常每 run 重新交付
   （`upload_home` 全量覆盖交付，追问无感知）。
5. **对话租约台账：不占 run 槽位**。租约是 `conversations/<根run>/lease.json`
   （0600、原子写），记 sandbox id、代数、pack/模板、暂停与到期时间、
   `retiredSandboxIds`（被替换但未能销毁的旧沙箱，挂账重试）。泊车成功 =
   run 的资源职责完结（终帧 `cleanup.released=true` + `cleanup.lease`），
   **不构成 resource hold**：不占并发槽位、不阻塞新 run 受理。闲置 TTL
   `PI_GLINK_CONVERSATION_LEASE_TTL_SECONDS`（默认 7200s：正常追问间隔分钟级，
   覆盖午休/会议节奏；0=禁用热路径，收尾直接销毁=旧行为），硬上限 24h
   （对齐 E2B Pro `set_timeout` 上限量级）启动校验拒绝越界。
6. **对账 fail-closed 销毁**。资源对账 watch（同一 tick）扫全部对话租约：过期
   → 按沙箱 id 销毁（kill-based confirm 契约，not-found=已不在），全部确认后
   删记录；未确认保留到下轮；未过期只重试 `retiredSandboxIds`。无
   `E2B_API_KEY` 不动任何租约。watch 启动条件放宽为「外部执行面或有 E2B key」：
   换面部署仍能清理数据根里的租约。
7. **删除链路（最小闭环）**。`RunManager.delete_conversation(root_run_id)`：
   销毁租约沙箱（含 retired）→ 全部确认后删除宿主对话状态对象
   （`conversations/<根run>/`：state 与 lease）。任一沙箱未确认销毁则目录与
   租约原样保留。多租户 owner 门禁与其它对话级读一致（未知/异租户一律
   `unknown`，不销毁不删除）。**run 记录、事件日志与工作区不属于这个闭环**：
   产品侧删除入口（HTTP 路由与回收政策）另行决定，组合在本函数之上。

## 安全与边界

- 凭据：泊车前删沙箱内凭据副本（只删已知相对路径，与收尾清扫同纪律）；
  凭据值不进租约/标记/事件/日志；热路径的凭据交付照常每 run 渲染覆盖。
- 租约记录无凭据、无主机路径泄漏面（对话根 run id + 沙箱 id + 时间戳）。
- 沙箱标记文件是宿主写的**宿主可读事实**（内存快照不承载它，必须落盘），
  位于状态根外，回收打包器与回灌都不会携带它。
- 观测：热/冷路径选择（`kernel_status` + `session_ready.sandboxResume`）、
  泊车事实（终帧 `cleanup.lease`）进事件流。

## 不兼容

- 归档通道部署下，E2B 面 ACP run 收尾不再销毁沙箱（改为泊车）；终帧
  `cleanup` 新增 `lease` 字段（前端按未知字段忽略，事件 schema 对终帧
  payload 的 `cleanup` 是开放对象）。
- `RunManager` 构造新增可选 `sandbox_resume_factory`（测试注入缝，默认 SDK
  类方法）。

## 理由与残留

- **泊车先决是「本 run 发布」而非仅「回收成功」**：租约代数必须描述权威状态；
  回收失败（或发布停在上轮）时沙箱里的状态不被本 run 背书，销毁即最诚实。
- **热路径候选仅在归档通道可用时启用**：降级冷路径必须是完整的阶段 1 回灌；
  无通道（无 public URL）部署若跳过了宿主侧继承又降级，将没有回灌来源。
- **租约写在 pause 之前**：中间崩溃留下「租约在、沙箱可能还在跑」——热路径
  connect 接受 running 沙箱，语义仍然正确；反序会留下无记录的暂停沙箱孤儿。
- 未覆盖项：E2B paused 沙箱的保留期与计费模型在 SDK 2.51.0 内无文档
  （`set_timeout` 上限 Pro 24h / Hobby 1h 只是运行寿命的类比）；宿主在
  「写租约成功、pause 之前」崩溃的窗口内依赖 E2B 侧自身的沙箱寿命收尾，
  元数据标签（`create(metadata)`）留作未来全量对账的钩子（现实现对账只按
  持久化身份，不 listing——沿用资源台账纪律）。
- 旧控制面对 `on_resume="reboot"` 的「静默降级为 restore」路径（2026-08-20
  之前的自托管控制面）：本文以 `keep_memory=False` 消解其风险（无内存快照可
  恢复、快照内无 agent 进程），未做真实控制面矩阵验证。
