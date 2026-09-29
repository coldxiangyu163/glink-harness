# 0017 声明式交付文件与宿主封存

- 状态：已接受（2026-09-29）
- 范围：ACP 投影、run 产物封存、E2B 工作区回收、读模型与前端交付卡片

## 背景

[ADR 0007](0007-report-served-only-from-host-artifact.md) 的决定仍成立：`report.md` 主报告只能从宿主拥有的 `runs/<id>/artifacts/report.md` 下载。但仅回收钉名文件会让辖区/维度备忘录等补充文件没有归宿。dsh 的 `present` 工具将现有工作区文件连同说明声明为交付物；仅在会话里声明并不意味着宿主已经安全回收。

## 决定

1. ACP `present` 工具的 `tool_call`（`rawInput.files`）投影为 `deliverables_declared` v1，仍保留普通 `tool_started`/`tool_finished`。事件只表示声明，不表示存在、可下载或工具执行成功；只收合法形状、单次 1–8 项的声明。同一 run 去重后最多封存 8 个不同路径，多次声明超额如实写入 `artifactFailures`。pi 族没有该事件来源，不伪造；`report.md` 保底不变。dsh 包自带 `dsh-tool-present` 但 base bundle 不挂载，本仓 ACP profile 的用户 patch 启用它并设 `maxFiles: 8`，不改内核或镜像。
2. 宿主 Seal 阶段读取已提交的声明（未提交事件绝不引导回收）；每条路径必须是非绝对、非空的工作区相对 POSIX 路径（不允许 `..`、`.`、空分量、反斜杠或 NUL，UTF-8 编码 ≤512 字节），最多 8 段；目录句柄逐级 `O_NOFOLLOW`、仅普通文件，单件上限 32 MiB。`report.md` 仍由主报告封存路径负责；被声明时只复用已成功的主报告副本。其余文件在秘密清扫之后脱敏并原子复制到该 run 的 `artifacts/<相对路径>`。读取失败/链接/超限/缺失逐件 `rejected` + 原因，不阻终态，也不改变主报告的 `no_report`/`report_rejected` 判定。终帧 `deliverables` 与终态 RunRecord 各保留同一份逐件 `{path, description?, status, reason?}`；超出的声明有 `artifactFailures`，不宣称已回收。
3. E2B 工作区回收在同一 64 个文件/64 MiB 总量/32 MiB 单件配额下，**先主报告与合法声明，再补普通直系文件**；嵌套目录按每级 SDK 列表元数据核对目录且不穿过已知链接。超配额记失败，终态照常收口；不为多文件扩大沙箱回收预算。inproc/Docker 直接从本 run 工作区按同一宿主目录句柄校验。
4. `GET /api/runs/{id}`（列表同形）提供逐件结果与 `available`（读面复查宿主副本）；`GET /api/runs/{id}/deliverables/{0..7}` 只用索引在本 run 的终态清单里查成功项，不接受任意文件路径作为下载能力。先 owner 过滤，再固定宿主副本目录句柄/大小校验，最后本 run 的 redactor 二次脱敏，带 `X-Redacted`。未知 run、异租户、失败项、丢失/换链副本同形 404。UTF-8 文本与二进制文件以 surrogateescape 保持原始字节，仅脱敏命中片段；任意编码外泄不在已有脱敏保证内。
5. 前端 SSE 先显示未就绪声明，终帧或详情以逐件结果收口；有成功副本才可打开/下载；没有声明的历史 run 不出现卡片。内部工具名不进入界面文案，主报告文件卡保留。旧事件版本水位纪律不变；新事件仅 v1 新类型，无旧类型 bump。

## 取舍与边界

- dsh 会话 `deliverables/presented` 的成功事件是其原生语义；ACP 当前只暴露工具调用，宿主不能观测私有会话事件且不应据此改写控制路径。因此产品事件表述“声明”，最终交付以宿主封存结果为准。
- 与 ADR 0007 无冲突：主报告的下载仍**只认宿主副本**，新增的文件也只认宿主副本，不恢复历史工作区报告下载。
- 工作约定条件式提示具备 `present` 的 harness 声明补充文件；pi 保持 `report.md`。引用来源前先打开的底线不变。
- 不修改用户历史数据或在用户服务上回填旧 run；新的终态字段对没有声明的运行为空列表。未构建或切换 Docker/E2B 模板，既有镜像是否含工具包须只读核验并在真实 run 验收中验证 profile 加载。
