# GLINK-DSH-03 交付回执

## 结论与代码范围

已完成源码接线和隔离验证，等待 Lead 评审与合并。当前服务、镜像、E2B 模板及历史报告未修改，也未宣称已生效。

- 分支：`glink-dsh03`，基线 `55b643d554bb1e01ef842b61973377c9489fde55`。
- 实现提交：`92854d731022b7dddf6b4905dd4a1e9af75fcd65`。
- 工作区：`/Users/coldxiangyu/.herdr/worktrees/pi-glink/glink-dsh03`。
- 入口：Lead 按 brief 启动的 `/Users/coldxiangyu/.local/bin/pigpt`，当前 runtime 的 `PI_PROVIDER=openai-codex`、`PI_MODEL=gpt-6.1-sol`；未另启 agent 或切换入口。
- 本报告随后单独提交，不改变已验证的执行代码。

### 实现

`OPENAI_MODEL` 与新增 `OPENAI_REASONING_EFFORT` 经 `.env`/进程环境解析到冻结的 `HostConfig`，随现有 `ExecutionSpec.config` 到 ACP driver。未新增存储 `RunSpec` 字段或改写历史格式。

包清单支持结构来源表，dsh 声明模型与 effort 的 config id、source 和 encoding，不固定部署值。缺声明来源值在宿主启动阶段拒绝，pi、codex、opencode 等未接线包不新增必填项，既有静态策略值仍可使用。

dsh 的模型值按真实 `modelValue()` 格式 `JSON.stringify([provider, model])` 构造 fake 广告。从广告路由先匹配完整模型 id，如 `openai/gpt-6-sol`，有歧义时用 `openrouter/openai/gpt-6-sol` 全路由消歧，不按首个斜杠拆模型 id，不自行拼造 ACP opaque value。

new/resume 都先应用模型，再从更新后的广告集应用 effort。缺 config id、缺可选值、无唯一路由、setConfig 错误、缺返回状态、实际 `currentValue` 不等于声明，均在 prompt 前失败。每次变更复核此前应用值，防止选择 effort 又撤销模型选择；已匹配不重复发送请求。

resume 使用当前部署冻结值，父 run 的原始实际值、事件和报告不回改。`settings.yaml` 删除失效的 `agent-default-model`，保留 provider 的 `apiKeyEnv` 引用结构。ADR 0016 记录原假设被证伪、组合验证改归部署验收及 resume 决策，README、环境示例和索引同步。

### 涉及文件

- 配置与协议：`host/pi_glink/config.py`、`host/pi_glink/harnesses.py`、`host/pi_glink/kernel/acp_execution.py`、新增 `host/pi_glink/kernel/acp_session_config.py`。
- 包：`harnesses/dsh/harness.toml`、`harnesses/dsh/home/.dsh/settings.yaml`。
- 文档：`.env.example`、`README.md`、`docs/adr/0016-acp-credential-env-single-source.md`、`docs/adr/README.md`、`docs/multi-harness-plan.md`。
- 测试：`tests/fixtures/fake_acp.py`、新增 `tests/test_dsh_session_config.py`、`tests/test_harness_packs.py`、`tests/test_host_app.py`、`tests/test_kernel_acp_config.py`、`tests/test_kernel_acp_runner.py`、`tests/test_repository_boundaries.py`。

最小新增模块理由：来源表解析必须纳入 `harnesses.py`；为不向千行 driver 增加职责，原 apply/project/select 逻辑抽入 133 行的 `acp_session_config.py`。边界测试仅登记这个 kernel 模块，未增加任何外部导入权限。`docs/multi-harness-plan.md` 仅改 §3.1 的 session_config 定义行，已获 Lead 明确授权。其余候选模块 `acp_env.py`、`harness_acp.py` 无需修改。

## 验证证据

环境：Python `3.12.10`，pytest `9.1.1`，Node `24.16.0`。worktree 的忽略项 `.venv` 只链接到既有项目开发解释器，未安装依赖或修改共享环境。定向测试清空继承环境，使用临时 HOME；全量门禁自行隔离 HOME、XDG、缓存和测试目录。测试只替换外部 agent，不手填 run 终态。

原始日志均保留在 `/tmp/glink-dsh03-A6Jp7Q/`，不把过程日志写入仓库。下列退出码均由实际进程取得，没有用输出截取掩盖状态。

| 阶段 | 命令 | 原退出码与结果 | 原始日志 |
|---|---|---|---|
| 预期红测，未修复生产实现 | `.venv/bin/python -m pytest tests/test_kernel_acp_runner.py::test_acp_declared_model_must_match_actual_after_set_config -p no:cacheprovider` | `1`，1 failed，原实现错误地 finished | `red.log` |
| 首轮断言修复 | `.venv/bin/python -m pytest tests/test_kernel_acp_runner.py -k 'session_config or declared_model' -p no:cacheprovider` | `0`，4 passed，Lead 已实读核验 | `green-assertion.log` |
| 来源接线预期红测 | `.venv/bin/python -m pytest tests/test_dsh_session_config.py -p no:cacheprovider` | `1`，原解析器拒绝 source 表 | `red-wiring.log` |
| 来源接线绿测 | `.venv/bin/python -m pytest tests/test_dsh_session_config.py tests/test_kernel_acp_runner.py -k 'session_config or declared_model or dotenv_model' -p no:cacheprovider` | `0`，5 passed | `green-wiring.log` |
| 最终定向矩阵 | `.venv/bin/python -m pytest tests/test_dsh_session_config.py tests/test_kernel_acp_config.py tests/test_harness_packs.py tests/test_host_app.py -p no:cacheprovider` | `0`，134 passed | `targeted.log` |
| 全量交付门禁 | `bash scripts/check.sh` | `0`，schema、ruff、mypy、Python、覆盖率、Node 全通过 | `check.log` |
| 文档新增行标点与 diff 检查 | `check-punctuation.sh --lang zh /tmp/glink-dsh03-A6Jp7Q/prose-additions.md`；`git diff --check` | `0` | 终端输出 |

全量实测：mypy 63 个源文件零错误；Python **1599 passed、1 deselected**；覆盖率 **92.3%**，基线 **92.2%**，未改门槛；Node 前端/扩展 **199 passed**，dsh fetch bundle **41 passed**，Node 均无失败或跳过。

明确未计入通过项：外部标准库逐值 UI 对齐，门禁显式 SKIP，对应 Python 用例 deselected。Node 有既有 `MODULE_TYPELESS_PACKAGE_JSON` 警告，未修改仓库外 package.json 来消除它。

全量证据覆盖实现提交全部执行内容。全量后唯一代码整理是移除 `acp_execution.py` 尾部空行，无行为变化，随后 diff 检查通过；未因提交机械重跑全量。

### 原事故红测摘录

```text
assert final is not None and final.status == FAILED
E ... status='finished' ... and 'finished' == 'failed'
FAILED ...::test_acp_declared_model_must_match_actual_after_set_config
1 failed in 0.72s
RED_EXIT=1
```

该回归通过真实 Manager、运行目录、ACP 子进程和协议请求，fake agent 广告 GPT 路由但对 setConfig 返回 DeepSeek `currentValue`，原实现仍发送 prompt 并完成报告。修复后 run 为 `failed / execution_error`，不发送 prompt，不伪造 session_ready。

## 风险、生效与资源

- dsh 部署现在必须配置非空 `OPENAI_MODEL`、`OPENAI_REASONING_EFFORT`。新增 effort 键无默认值，如 `xhigh`，不改变 pi thinking，也不是 `OPENAI_MAX_TOKENS`。当前 `.env` 未改，操作员在将来获授权部署前须补齐。
- 真实包版本与模型组合、模型质量、真实 ACP/Docker/E2B、新模型是否可用、子代理继承核验及前端期望/实际展示未验收；fake 协议通过不代表这些通过。
- 未触碰 `web/`、`tests/web/`、`tests/browser/`、`docs/acp-integration-notes.md`；未读取或修改真实 `.env`、运行数据根、用户 pi/dsh 配置，未操作 7872 等既有服务。
- 未调用真实模型/检索，未构建镜像/模板，未 push、发布或部署。无外部容器/沙箱身份待释放，无真实数据清理或人工资源裁决。
- 全量临时资源由门禁退出时删除；定向测试的 fake 子进程由生产执行链收尾，临时日志保留给 Lead 核验。无持续运行的测试服务。
- 资源处置仍由 Lead 管理；本 worker 保留 pane、worktree、分支和 ownership，等待 Review follow-up，不接新任务。
- 回退以本分支实现提交为边界，由 Lead 按合并状态回退。没有真实配置、历史数据或模板变更需要回滚。
