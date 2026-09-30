# 0018 对话级权威状态与归档传输通道

- 状态：已接受（阶段 1；背景与裁决见任务简报，方案 cc 拟、用户批准）
- 范围：`kernel/archive_channel.py`（新）、`egress.py`（archive 路由挂载）、
  `kernel/acp_e2b_env.py`（回收/回灌改造）、`kernel/acp_env.py`（阈值）、
  `kernel/harness_acp.py`（对话根与门禁）、`runs/stages.py`、`runs/secrets.py`、
  `runs/state.py`、`kernel/execution.py`（spec 扩展）、`store.py`（目录布局）
- 关联：[0005](0005-acp-e2b-follow-up-via-recovered-state.md)（回收受理语义，本文扩展其落点）、
  [0008](0008-egress-proxy.md)（listener 与 run-scoped token 基础设施，本文复用并加写盘端点）、
  [0015](0015-acp-credential-channels-under-model-credential-policy.md)/
  [0016](0016-acp-credential-env-single-source.md)（凭据纪律，归档 skip 凭据目标）

## 背景

E2B 面 ACP/dsh 追问链路是每 run 一次性沙箱 + 有界回收回灌：files API 逐文件
（单文件 8MB、总量 64MB、4096 文件）+ 继承同限。dsh 会话文件每轮追加（实测
835KB/2 轮，线性增长），15-30 轮撞限，断崖表现为追问被拒
（`session_unavailable`）。resume 超时写死 60s 不是本阶段处理项。

## 决定

1. **宿主持有对话级权威状态（durable 真相源）**。E2B 面每轮回收发布到
   `<data_root>/conversations/<对话根 run>/state/`：整份成功才可见（staging +
   rename，沿用 0005 的发布纪律），`state/manifest.json` 记录
   `sourceRunId`/文件清单（size+sha256）/体量/模式。追问门禁的新鲜度校验要求
   manifest 的 `sourceRunId` 等于父 run 自己——最新一轮回收失败时状态停在
   更早轮次，追问被拒（与 0005「回收失败不受理」同语义，不呈现 agent 并不
   完整拥有的会话）。三面映射：inproc/docker 的权威状态就是最新轮 run 自己的
   `acp-agent/home`（宿主天然持有，不建对话目录）；公共层只抽象「宿主权威
   状态根路径」，不抹平各面差异。
2. **归档传输通道**：复用 0008 出口代理的同一 listener，独立模块
   （`kernel/archive_channel.py`）承载 `/archive/state` 路由。`POST`（沙箱→宿主
   回收）流式落盘 run 专属 staging、按 manifest 校验、增量拼接后替换式发布，
   整份成功才可见；`GET`（宿主→沙箱回灌）服务端从 grant 推导源目录，不接受
   客户端路径。token 独立前缀 `pga_`、只授归档路由、run-scoped、终帧提交后
   吊销，与 `pge_` 互不通用；token 值入本 run 脱敏边界，不进日志/事件。写盘
   端点扩大 0008 的攻击面，以三层收敛：token 只达本 run 对话状态目录、
   `ARCHIVE_MAX_BYTES`（默认 256MB）超限中止连接不发布、tar 解包白名单
   （`manifest.json`/`files/<rel>`/`delta/<rel>`）+ tarfile data filter（拒绝
   绝对路径与 `..`）+ 成员数上限 + socket 读超时（防慢速占用 handler 线程）。
3. **按 offset 增量回收（O(增量)）**。前置验证确认 dsh 会话落盘是纯追加的
   zstd 帧拼接（`[0, size)` 任意时刻是完整帧集），宿主下发上一版 manifest 作
   base，沙箱端打包器（宿主经 files API 下发的 node 脚本，模板镜像无需变更：
   node/tar/curl 已在）按「路径 + 前缀 sha256 + size 单调」锚定，未变文件不传
   （`base == size` 锚定项）、追加文件只传 `[base, size)` 尾部、新增/前缀不匹配
   文件传全量。宿主端拼接后整文件 sha256 复核；base 长度不符或哈希不匹配整次
   回收失败（下一轮自然全量），已发布状态不动。
4. **INHERIT/UPLOAD 上限语义从「会话寿命」改为「异常检测」**。继承与回收共享的
   阈值抬高并与之对齐通道上限：文件数 16384、单文件 128MB、总量 256MB、扫描
   预算 64MB。正常研究会话远低于此（dsh 30 轮 ≈ 12-25MB）；达到即异常量级，
   fail-closed 拒绝（回收失败→无发布→追问按新鲜度门禁拒绝），事实进事件流。
   追问回灌走通道后 files API 的 8MB/文件上传上限不再约束状态文件。
5. **回灌沿用准入认可的同一状态来源**。族门禁返回状态根路径，在权威准入后随
   `AdmittedRun` 传给 Prepare；族的 `StateTransfer` 决定回灌/热路径，编排层不再按
   ACP 具体类型分派。只有该来源是已发布的对话 `state/` 且通道可用时（E2B + public URL），
   追问 run 的宿主 HOME 才只渲染凭据，沙箱端 curl GET 拉取同一权威状态根
   （冷路径仍是新沙箱 + 回灌 + session/resume）；凭据上传在后，同路径时凭据副本赢。
   旧 E2B `acp-agent/recovered` 或 inproc/docker 父轮 `home` 作为来源时，仍继承该根并
   经 files 上传，即使归档通道已开启也不从另一个空目录回灌；本轮回收仍可通过归档通道发布。
   混合通道历史中，父轮自己的完整 `recovered/` 优先于早期轮次的对话级归档；没有自身
   镜像时仍必须由父轮自己发布 manifest，陈旧归档拒绝续接。已存在但损坏/缺失 manifest
   的对话状态不以 `recovered/` 隐藏其不确定性。
   对话 `state/` 缺少合法 manifest 时门禁拒绝；归档 GET 在响应头前返回 404，直接 serve
   同样拒绝，不以成功的空 tar 冒充已有状态。
6. **降级链（缺失降级，非安全降级）**。通道不可用（无 public URL）或通道失败
   时：追问回灌退回宿主侧继承 + files API 逐文件上传（现状能力）；回收退回
   files 逐文件镜像，发布目标仍是对话状态根（mode=`files-fallback`），凭据目标
   一样不进。通道在而回收降级时发 `kernel_status`（warning，
   `acp_state_channel_degraded`）——降级可见，不静默。
7. **凭据纪律不变**：凭据目标不进归档（沙箱端打包器排除 + 宿主端 manifest
   见即拒绝，双保险）；回灌打包 skip 凭据目标与 manifest；解包后的状态树由
   本 run 的秘密边界做只读扫描吸收凭据形状值。凭据每 run 渲染（0003/0016）。
8. **观测**：回收的体量/文件数/耗时/模式（`stateRecovery`）与回灌的
   体量/耗时（`stateBackfill`）进 `session_ready`（open payload 透传）；降级与
   超限走 `kernel_status`。

## 不兼容

- 新回收不再写 `runs/<id>/acp-agent/recovered`；旧 run 的该目录保留并继续作为
  兼容回退的继承源（无对话状态根时 `state_home` 回退旧布局），不迁移、不清理。
- 回收内容不再含凭据副本（旧 `recovered` 镜像含、继承时 skip、收尾删除；
  对话状态根是跨轮 durable 对象，凭据副本进去就没有删除时机）。
- E2B + ACP 面要获得通道能力需要 `PI_GLINK_EGRESS_PUBLIC_URL`（0008 已要求的
  部署义务延伸到 ACP 面；未配置时按降级链运行，不拒绝启动）。

## 理由与残留

- 传输格式是**裸 tar**：沙箱模板镜像（bookworm-slim + node）没有 zstd CLI，
  两端压缩能力不一致时通道反而失效；主要体量（dsh 会话）本身已是 zstd 压缩
  数据。压缩层（tar+zstd）留给模板演进后再启用，协议按 `Content-Type` 自然可扩。
- 沙箱端打包器是宿主下发的 node 脚本而非模板内固化：不依赖模板重建即可上线；
  代价是每次回收多一次小文件写入（脚本 + base manifest + token，均 KB 级）。
- 拼接发布是两段 rename（旧树移开、新树就位）：中间崩溃留下旧状态或无状态，
  新鲜度门禁保证不呈现陈旧 manifest 所述的可用性；不做跨目录原子交换。
- 阶段 2（E2B pause/resume 热路径、generation 一致性、对话租约 TTL）已由
  [0020](0020-conversation-sandbox-lease-hot-path.md) 决定；本文件只落阶段 1 的权威状态与通道。
