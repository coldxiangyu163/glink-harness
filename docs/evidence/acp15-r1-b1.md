# GLINK-ACP-15 R1：B1 远程 HOME 目录链修复

## 裁决与范围

R1：FAIL，1 个成立 BLOCKER。基线为首次提交 `f31056bbeff220ba183e2ce503a649fae6d1915c`，分支仍为 `glink-acp-15`，原 worker/ownership 保留。本轮只处理 B1 及其启动/失败回收消费者，不另行扩审、委派或修改审计白名单/编解码。

原缺陷：`_repair_remote` 只核验 HOME 以下的祖先；HOME 链接可在最后 rename 拒绝前发生日志读取与 scratch 写入，父分量链接则连最后检查也缺失。泊车/reboot 无活 writer 不证明遗留目录链不是链接。

## 修复点

- `acp_state_compat.validate_remote_home()`：以沙箱 `/` 为可信锚点，只 `files.list(已核验的父目录, depth=1)`，按精确 child path 和 SDK entry kind 逐级验证 `/home` → `/home/user` → `/home/user/acp` → HOME。链接、非目录、缺失/重复条目或无法核验均拒绝；不靠 realpath/stat 跟随链接。
- 冷续接仅对不存在的目录逐级 make_dir，随即重新 list 已核验父目录确认该新 entry 为目录，再深入下一层。不能用 create 覆盖链接。所有请求带现有 request_timeout。
- `_repair_remote` 在递归列举 HOME、解压读取之前核验整条根链；`_publish_remote` 在 `files.write(temporary)` **之前**复验，并让原有 `test ! -L` / `test -d` / `test -f` / chmod / rename 检查包含 HOME 的所有父分量，最后守卫不替代先验。
- `AcpExecutionAdapter`：热重连拿到 handle 后，先验证目录链，再读 generation marker 与上传凭据；冷路径在回灌/上传之前创建并核验目标。拒绝标记阻止失败收尾再次读取/发布该 HOME，也不泊车；安全工作区产物仍沿既有回收链处理。早于 transfer 建立的热拒绝本就没有可回收状态。
- **本地/Docker ArtifactTree 代码完全不变**；codec、白名单、幂等与旧事件 payload 也未修改。

涉及文件：两个 kernel 模块；新增 `tests/test_kernel_acp_state_compat_paths.py`；相邻真实命令/SDK fakes 补可信锚点目录映射与正确 symlink entry kind（`test_kernel_acp_e2b.py`、`test_kernel_acp_archive.py`、`test_kernel_acp_state_compat.py`）；架构/§19.6 最小同步及证据。fixture 扩展只是外部 SDK/沙箱路径表面的真实表示，不手填 Manager 的终态，也不放宽已有安全断言。

## 红绿回归

全部使用合成日志/临时目录、真实 Manager + 子进程 fake ACP；无真实数据、模型或云调用。每个场景先完成父轮、停掉 agent、回收并生成租约，在 **pause 前**把 HOME 或 `/home/user/acp` 移到临时 outside 目录、原路径替为链接；随后同沙箱 `on_resume=reboot` 追问。TrackingFiles 记录目标日志读取、目标 scratch 写入与目标目录遍历，不仅检查最终 status。

实际命令形式（解释器 Python 3.12.10 / Node 24.16.0；worker 仍为 brief `pigpt`，`openai-codex/gpt-6.1-sol`，`xhigh`）：

```bash
tmp=$(mktemp -d /tmp/acp15-r1-env.XXXXXX)
env -i HOME="$tmp" PATH="$PATH" PYTHONPATH="$PWD/host:$PWD/tests" \
  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PI_GLINK_EVENT_SCHEMA=strict \
  /Users/coldxiangyu/AI/git/pi-glink/.venv/bin/python -m pytest -p no:cacheprovider \
  tests/test_kernel_acp_state_compat_paths.py -k hot_manager -q
```

- **修复前红**：原始 exit **1（预期）**，两条热 Manager 回归均失败：`external_log_reads == 2` 而期望 0。HOME 场景 run 在写 scratch 后才失败；父分量场景错误继续。日志 `/tmp/glink-acp15.l22MjJ/r1-b1-red.log`。未复用 reviewer 私有/已删除 scratch。
- **修复后绿**：上面环境改为下面 3 个模块 `-q -s`，原始 exit **0**，日志 `r1-b1-green-2.log`：

  ```bash
  ... -m pytest -p no:cacheprovider \
    tests/test_kernel_acp_state_compat_paths.py \
    tests/test_kernel_acp_state_compat.py tests/test_kernel_acp_archive.py -q -s
  ```

  ```text
  B1 home external_log_reads= 0 external_scratch_writes= 0 external_directory_lists= 0 status= failed
  B1 parent external_log_reads= 0 external_scratch_writes= 0 external_directory_lists= 0 status= failed
  ```

  两者还断言：只有父轮一次 initialize、无 session_resume、无 child spawn，外部日志和宿主权威 manifest 字节不变、cleanup.released=true。四条直接 SDK 负例覆盖 `/home`、`/home/user`、`/home/user/acp`、HOME 自身，断言发现链接后不向该目录 list/read/write。原有冷 files/归档/reboot 正例、真实 tar/curl 以及本地无链接/完整性/幂等仍通过。
- 中间验证 `r1-b1-green.log` exit 1：B1 两例已为 0，但 fake 真实 shell 的新父目录 guards 映射错误导致 3 个合法续接失败。修正 fixture 的最长前缀 placeholder 映射（防止 `/acp/home` 的尾部被误替换），未放宽生产守卫或断言，得到上述 exit 0。
- 定向 ruff/mypy 各 exit **0**，日志 `r1-static.log` / `r1-mypy.log`。

## 最终门禁与证据复用

实际执行：

```bash
PATH="/Users/coldxiangyu/AI/git/pi-glink/.venv/bin:$PATH" bash scripts/check.sh \
  > /tmp/glink-acp15.l22MjJ/r1-check.log 2>&1
```

原始 **exit 0**（`r1-check.exit`），绑定本轮源码/fixture diff；随后只填证据与提交：

- schema / ruff / mypy 全 exit 0，mypy 64 source files 零错误。
- Python **1646 passed，1 deselected，213.27s**；唯一 deselect 仍为默认入口的外部 UI 标准库对齐，不计入通过。
- 覆盖率 **92.2% ≥ 基线 92.2%**，ratchet exit 0；未改基线或规则。
- Node 前端/扩展 **201 passed**，dsh bundle **41 passed**，fail/skip 均 0。
- `git diff --check` exit 0。

此次重跑必要：kernel 热启动/冷回灌与失败回收行为、共享 SDK fixture 都已改变，`f31056b` 的旧门禁不能覆盖 B1 增量。原真实 dsh 红绿证据继续复用：本轮没改本地 ArtifactTree、codec、白名单或协议方法，不为角色/提交变化再调用真实 agent。

## 边界与资源

仅源码与隔离自测，未改真实数据/7872 服务、未部署/构建/推送；E2B live、模型质量及真实云端控制面仍挂账。测试 sandbox 均显式 kill，父轮/追问 fake 进程按既有 close 链回收；链接目标是本测试专属目录，未操作未知资源。R2 由原 reviewer 仅复核 B1 增量，本 worker 不自行宣称 Review 已通过。
