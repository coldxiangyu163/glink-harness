# GLINK-DSH-03 Review R1 修复回执

## 范围与结论

R1 的 B1、B2、B3、B4 已按 Lead 裁决修复并自测，等待原 reviewer 进行 R2 增量复核。本报告不代表 R2 已通过。

- 分支：`glink-dsh03`；本轮起点：`9caa120e2c1dada1334dd273b826d210b7fa6c54`。
- 修复提交：`c93f5a97b22835372a6a1a366beb8892102c85d0`。
- 本轮执行代码仅三个文件：`host/pi_glink/kernel/acp_session_config.py`、`host/pi_glink/kernel/acp_execution.py`、`tests/test_dsh_session_config.py`；本报告另行提交。
- 角色、入口未变：原写 worker，Lead 按 brief 启动的 `/Users/coldxiangyu/.local/bin/pigpt`，runtime 为 `openai-codex / gpt-6.1-sol`。未委派或切换会话。
- 已实读 Lead 核验的 `/tmp/glink-dsh03-review-r1.WxLgNj/probe-results.json` 与 `ambiguous_route_after_display_limit/result.json`。

### 四项修复

| 阻塞 | 实现 | 回归 |
|---|---|---|
| B1 显示截断伪造模型唯一性 | new/resume 直接将完整 wire `configOptions` 交给控制校验；每次 setConfig 也读取完整返回矩阵。20 项配置选项与 100 项显示值上限只在全部校验完成后的投影应用，不参与唯一性判断 | `test_model_uniqueness_uses_catalogue_beyond_display_limit`：101 项完整广告中的两个同模型路由，裸模型必须失败；显式 provider/model 消歧仍完成，最终显示仍为 100 项 |
| B2 同名 id 自选首项 | 对声明的 config id 收集完整匹配集，重复即失败；应用前与每次返回的实际状态复核均走同一严格查找 | `test_declared_config_id_must_be_unique_in_every_advertisement`：model/effort 重复，初始、模型响应、effort 响应均失败；补充第 20 项显示上限之外的重复 id 负例 |
| B3 非 select 与缺 type 绕过广告值校验 | 只支持显式 `type=select`，缺 type 或其他类型失败；应用前与返回状态复核都校验广告值，不默认为 select | `test_applied_options_require_explicit_select_type_and_advertised_value`：`type=text` 且无 options、缺 type，初始和 setConfig 响应都失败，不发送研究 prompt |
| B4 TOML 行序决定应用顺序 | 宿主在应用时稳定排序，将 `reasoning_effort` 放到模型选择之后；其他策略相对顺序保留，不修改 manifest 解析的行序 | `test_reasoning_choices_are_read_after_model_switch`：正序和反序来源表行为相同，fallback 没有 effort、模型切换后才广告 effort，两者都先发送 model 再发送 effort 并完成 |

未扩大为其他评审建议或全仓重构，既有显示事件结构不变。

### 非 JSON 旁项语义

`mixed_non_json_route` 的 finished 行为保留。非 JSON 的 opaque value 没有可解析的 dsh provider/model 身份，不参与路由候选；只要完整广告中合法 JSON 路由唯一，选中该广告值并核验实际值即可。不会选择非 JSON 值，也不会把它解释成第二条合法路由。

`test_non_json_route_siblings_cannot_supply_or_hide_a_valid_route` 固定两个结果：only_non_json_route 失败、无 prompt；mixed_non_json_route 完成，实际模型为目标 JSON 路由，显示保留原始旁项。原正确负例未回退。

## 红绿与定向证据

原始日志保留在 `/tmp/glink-dsh03-r1fix-E0bUQA/`。每项都先加入回归，在对应阻塞尚未修复时实际变红，再修复对应生产代码并转绿。按 B1、B2、B3、B4 顺序推进；后项红测允许复用已修复的前项，不伪称四次都运行在相同未修改 HEAD。

共同命令前缀：`env -i PATH="$PATH" HOME=/tmp/glink-dsh03-r1fix-E0bUQA/home PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest tests/test_dsh_session_config.py`，后缀为表内 `-k` 选择器与 `-p no:cacheprovider`。

| 阻塞 | `-k` 选择器 | 红测原退出码与结果 | 绿测原退出码与结果 | 日志 |
|---|---|---|---|---|
| B1 | `catalogue_beyond_display_limit` | `1`：1 failed、1 passed；裸模型错误 finished | `0`：2 passed | `b1-red.log`、`b1-green.log` |
| B2 | `config_id_must_be_unique` | `1`：6 failed；重复 id 错误 finished | `0`：6 passed；随后显示上限外两个负例纳入最终定向与全量 | `b2-red.log`、`b2-green.log` |
| B3 | `require_explicit_select` | `1`：4 failed；非 select/缺 type 错误 finished | `0`：4 passed | `b3-red.log`、`b3-green.log` |
| B4 | `reasoning_choices_are_read_after_model_switch` | `1`：1 failed、1 passed；反序表 execution_error、模型请求未发送 | `0`：2 passed | `b4-red.log`、`b4-green.log` |

红测典型原始断言为 B1/B2/B3 的 `status='finished' ... 'finished' == 'failed'`，B4 为 `status='failed' ... 'failed' == 'finished'`。日志保留完整失败输出和测试名，退出码没有被摘要或管道替换。

最终定向命令：

```bash
.venv/bin/python -m pytest tests/test_dsh_session_config.py \
  tests/test_kernel_acp_runner.py tests/test_kernel_acp_execution.py \
  tests/test_kernel_acp_resume.py -p no:cacheprovider
```

同样在清空继承环境、临时 HOME 下运行，原退出码 **0**，**143 passed**，原始日志 `targeted.log`。覆盖四项增量、原 dsh 接线、声明与实际不一致、resume，以及既有 ACP 相邻合法输入。

## 全量与生效边界

本轮修改了控制矩阵读取和校验，原交付全量证据不能覆盖，已按要求重新执行：

- 命令：`bash scripts/check.sh`，原退出码 **0**，日志 `/tmp/glink-dsh03-r1fix-E0bUQA/check.log`。
- 环境：Python `3.12.10`，pytest `9.1.1`，Node `24.16.0`。沿用 worktree 忽略的既有开发解释器链接，未安装依赖或修改共享环境。
- schema 生成物通过；ruff 通过；mypy 63 文件零错误。
- Python **1616 passed、1 deselected**；覆盖率 **92.3%**，基线 **92.2%**，未改门槛。
- Node 前端/扩展 **199 passed**，dsh fetch bundle **41 passed**；均零失败、零跳过。
- 外部标准库逐值 UI 对齐仍显式 SKIP，对应 Python 用例 deselected，不计入通过。既有 Node 包类型警告未处理。
- 全量后执行代码未再变化，提交不触发机械重跑；`git diff --check` 退出码 0。本报告的标点检查另通过。

本轮只修改源码、合成回归和本回执。未触碰前端 owned files、真实 `.env`、用户 pi/dsh 配置、历史数据、7872 等既有服务、镜像或 E2B 模板；未进行真实模型/检索调用、push、发布或部署。

门禁临时资源由退出清理，fake ACP 子进程由生产执行链收尾；无外部沙箱/容器身份或测试服务遗留，日志留给 Lead 核验。真实包与模型组合、模型质量、子代理模型继承和 UI 期望/实际展示仍未验收。ownership、pane、worktree、分支保留，等待 R2，不接新任务。
