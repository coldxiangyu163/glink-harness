# GLINK-ACP-15：宿主续接副本兼容修复

## 范围与结论

分支 `glink-acp-15`，基线 `d1766adacfdd1159b51d5fd5af2f3ef48405dad4`（含 DSH-03）。本次只修改 kernel 的续接目标准备、相应测试及最小文档；不修改 bundle、web 或真实运行数据，不 fork/patch dsh。

根因与基线红证据见 [阶段诊断](acp15-diagnosis.md)：父 session `0e848302-a564-43e4-9f6d-7903d2827273` 的旧 `web/jina-fetch-request` 在 seq 43 未标 ignorable，dsh 冻结词表拒绝。删 4 个 lock 无改善，保留 lock 且给退役审计补标可 resume。此前 bundle 已停止生产此事件；本修复落实 Lead 随后批准的 §19.6 既有会话兼容决定，**不是重复修改事件生产方**。

## 实现与各路径接线

- `kernel/acp_state_compat.py`：白名单硬编码只含 `web/jina-fetch-request`；只识别 `.dsh/sessions/<cwd>/<session>/session.v3.jsonl.zstd`。逐事件仅插入 envelope 顶层 `ignorable:true`，原 JSON 行的其他字节不动（包括 >2**53 整数），不删事件/重编号/改引用。已有 true 标记无需重压缩或替换，未知类型不加标。
- 编解码用已经要求的 Node ≥24 公共 `node:zlib` Zstandard API，通过 stdin/stdout 传二进制，不增加 Python 运行依赖、不导入 dsh 私有实现。完整帧物理边界与校验和验证；头单独一个压缩帧，事件可同批次成帧。压缩文件沿用 128 MiB 上限，所有候选解压日志合计上限 256 MiB；损坏、重复 JSON 键、非法 ignorable 等拒绝，不把损坏历史修成成功。
- `AcpExecutionAdapter.start()`：只对 `pack=dsh` 且有 `resume_session`，在全部状态/凭据交付之后、`sandbox.spawn` **之前**调用。无 prompt/无新会话回退；失败不启动 agent。
  - **本地/Docker**：继承完成后的本轮 `runtime.home`（Docker 的宿主挂载源，而非容器内路径），经 ArtifactTree 目录句柄无链接读取/原子替换。父 HOME 不动。
  - **E2B files 冷回灌**：`transfer.upload_home()` 上传继承状态与凭据，工作区报告交付后，修补 `sandbox.place("home", runtime.home)` 的沙箱目标。
  - **E2B 归档冷回灌**：`transfer.backfill_state()` 先拉取权威归档，`upload_home()` 注入本轮凭据，再修补同一沙箱目标。宿主权威 state/manifest 在此边界字节不变。
  - **E2B reboot 热路径**：代数核验后跳过回灌，重新上传本轮凭据，再修补重连沙箱 HOME。旧 agent 已在 pause 前停止且 `on_resume=reboot` 不恢复旧 writer；只处理无活 writer 的目标。正常子轮回收仍可按现行契约发布其最新状态。
- 远程只读取 SDK 声明为普通文件、所有祖先均为目录的已知路径；有界读取，临时文件写入后 `test ! -L`/目录与普通文件检查、0600、同目录 rename 发布。失败清理只针对本次随机命名的 scratch，不遮蔽原异常。lock 不删除也不替换。
- 过程不记录会话内容、URL 或 codec/SDK stdout；异常只有固定诊断摘要，最终错误仍经过 run 脱敏。对 codex/opencode 及首轮不调用此兼容处理。

## 修改文件

1. `host/pi_glink/kernel/acp_state_compat.py`（新模块，职责不塞进已过千行 adapter）
2. `host/pi_glink/kernel/acp_execution.py`（唯一定向接线）
3. `tests/test_kernel_acp_state_compat.py`（完整性/幂等/限制/链接/异常、真实 Manager 的模型联合链与三条远程路径）
4. `tests/fixtures/fake_acp.py`（仅外部 dsh 日志输入/冻结词表解析替身，消费真实继承文件，不手填最终状态）
5. `tests/test_kernel_acp_archive.py`（真实 node/tar/curl 回灌后的目标修补及权威字节不变；SDK write 接受 request_timeout）
6. `tests/test_repository_boundaries.py`（登记新 kernel 模块，仅放行既有 ArtifactTree 下层依赖）
7. `docs/architecture.md` §5.2、`docs/acp-integration-notes.md` §19.6（授权范围内最小契约例外）
8. `docs/evidence/acp15-diagnosis.md`、本证据文件。

## 已执行红绿验证

环境：worker brief 入口 `/Users/coldxiangyu/.local/bin/pigpt`，当前 `openai-codex/gpt-6.1-sol`、`xhigh`；Python `3.12.10`（主 checkout `.venv/bin/python` 只用依赖解释器）、Node `v24.16.0`。所有定向 pytest 均使用 `env -i`、临时 HOME、禁用外部插件/缓存、严格事件 schema。过程原始输出留 `/tmp/glink-acp15.l22MjJ/`，真实副本根 0700，不入仓。

- **真实原机制红**：`repro.py baseline2` → **exit 1（预期）**，`-32603 Internal error` / `web/jina-fetch-request` seq 43；`repro.py no-lock` → **exit 1（预期）**，相同拒绝。
- **源码回归红**：在 adapter 未接兼容模块时，`python -m pytest -p no:cacheprovider tests/test_kernel_acp_state_compat.py -q` → **exit 1（预期）**，1 failed / 1 passed；真实 Manager 子轮因 `AcpRequestError -32603` 失败，未知 required 事件负例仍失败。日志 `regression-red.log`。
- **真实 dsh 源码修复绿**：

  ```bash
  PYTHONPATH="$PWD/host" /Users/coldxiangyu/AI/git/pi-glink/.venv/bin/python \
    /tmp/glink-acp15.l22MjJ/repro.py production
  ```

  原始 **exit 0**（`production.log`）。由当前生产 `repair_resume_home` 修补全量原恢复副本，然后逐项比对所有字段、重复修补字节幂等，再执行 `initialize → session/resume`。只删除复制品里的凭据与 Linux profile，协议参数与基线红一致，不发 prompt、不调用模型。

  ```text
  production repair integrity/idempotence PASS files 4 annotated 135 locks unchanged 4
  initialize ... deepseek-harness-acp ... sessionCapabilities.resume: {}
  resume PASS response keys ['configOptions'] elapsed 1.427
  agent exit 0 hostStopped False
  stderr tail []
  ```

- **定向组合绿**：兼容、归档、执行、热路径、resume、Docker/E2B 与 DSH-03 8 个测试模块 → **exit 0**（`targeted-2.log`）。实际命令：

  ```bash
  tmp=$(mktemp -d /tmp/acp15-testenv.XXXXXX)
  env -i HOME="$tmp" PATH="$PATH" PYTHONPATH="$PWD/host:$PWD/tests" \
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PI_GLINK_EVENT_SCHEMA=strict \
    /Users/coldxiangyu/AI/git/pi-glink/.venv/bin/python -m pytest -p no:cacheprovider \
    tests/test_kernel_acp_state_compat.py tests/test_kernel_acp_archive.py \
    tests/test_kernel_acp_execution.py tests/test_kernel_acp_hot_path.py \
    tests/test_kernel_acp_resume.py tests/test_kernel_acp_docker.py \
    tests/test_kernel_acp_e2b.py tests/test_dsh_session_config.py -q
  ```

  包含 fake agent 消费实际 v3 日志、resume → model/reasoning setConfig → prompt → turn_started；未知类型拒绝，模型不在错误 resume 后应用，父事件与权威状态不变。真实 tar/curl 回灌补充证明不是手填目标状态。
- 开发中间失败如实保留：首次 Node 单帧解码未迭代整份 append 流，模型联合回归仍红（`regression-green.log`）；Node 对 torn checksum 尾可返回部分数据，完整性负例抓到（`compat-tests.log`），后补公共 frame 边界检查。未删除/放宽这些断言。首次静态检查仅报新测试未使用的 Path import，exit 1；删除无用 import后纳入最终门禁。mypy 首次 exit 0。

## 最终门禁

实际命令：

```bash
PATH="/Users/coldxiangyu/AI/git/pi-glink/.venv/bin:$PATH" bash scripts/check.sh \
  > /tmp/glink-acp15.l22MjJ/check.log 2>&1
```

**原始退出码 0**（`check.exit`）；解释器由当前 worktree 的 PATH 选出，源码/PYTHONPATH 由脚本固定到当前 worktree，不运行主 checkout 的代码。

- schema 生成物、ruff、mypy 全部 exit 0（mypy：64 source files 零错误）。
- Python：**1640 passed，1 deselected，212.10s**。唯一 deselect 是默认入口规定的外部 UI 标准库逐值对齐，不计入通过；没有静默 optional-dependency skip。
- 总覆盖率 **92.3%**，基线 **92.2%**，ratchet exit 0；未改基线或检查规则。
- Node 前端/扩展 **201 passed**，dsh bundle **41 passed**，各自 fail/skip 均为 0、exit 0；没有浏览器或真实模型调用。
- `git diff --check` exit 0。

全量检查绑定本文列出的源码/测试范围及当前 worktree diff，随后只更新证据文字并提交，不机械重跑无代码变化的同范围门禁。

## 生效、风险与资源

- 只交付源码与隔离回归；不代表 7872 当前服务已加载、不更新 Docker 镜像/E2B 模板、不改历史报告或权威原数据。
- 真实 dsh 验证零 prompt/零模型；E2B live、真实模型质量、真实云端 pause/reboot 仍未验收，按 brief 挂账。
- 本机 codec 需要 PATH 上的 Node 公共 Zstandard API（现有 Node ≥24 前置要求）；候选解压体量超 256 MiB 时拒绝，防止压缩炸弹。该限幅不代表已测大型真实会话吞吐。
- 没有创建真实服务/云资源；真实 dsh 探针进程均 stdin 关闭后自行 exit 0。定向 fake 沙箱在测试后显式 kill；默认检查资源由入口清理。真实数据临时副本仅保留给 Lead 核验，不传播不提交。
- 回退仅 revert 本提交及文档例外：恢复旧副本拒接行为，不需要数据回滚。是否修改真实原数据仍须另行授权。
