# 阶段 2（ADR 0020）真实 E2B 文件级冒烟

- 日期：2026-09-30；命令：`.venv/bin/python scripts/smoke-e2b-lease-pause.py`（见
  `environment.txt`；退出码 0，逐步输出在 `smoke-output.txt`）。
- 授权：任务简报明列的真实 E2B 文件级冒烟（起沙箱/写读文件/pause/resume/销毁），
  不跑 agent、不打模型；`E2B_API_KEY` 从仓库 `.env` 读取，不打印、不落日志。
- 覆盖：create → 写代数标记 → `pause(keep_memory=False)`（True，state=paused）→
  pause 幂等（False）→ `Sandbox.connect(id, on_resume="reboot")`（同 id，
  state=running，恢复耗时约 13s）→ 标记文件字节一致（FS 跨 pause/resume 保留）→
  `Sandbox.kill(id)`（True）→ 销毁后 connect 报 `SandboxNotFoundException`。
- 即：ADR 0020 依赖的 SDK 事实链（keep_memory=False + on_resume="reboot" 的
  暂停/恢复、文件系统亲和、按 id 销毁与 not-found 契约）在真实 E2B Cloud
  控制面上成立；本控制面未触发「reboot 被拒」或「静默降级 restore」路径
  （旧控制面风险仍以 ADR 0020 的 keep_memory=False 语义消解，未做矩阵验证）。
