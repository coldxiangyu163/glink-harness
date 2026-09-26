# pi E2B 面的 RPC 通道探针（GLINK-ARCH-3）

`rpc_channel_probe.py` 是**真实 E2B 沙箱**探针，不是默认检查的一部分；它只创建并操作
自己创建的沙箱，结束时按 `sandbox_id` 终止（不做模式匹配/批量清理），不打印任何凭据值。
`E2B_API_KEY` 与 `OPENAI_*` 从进程环境或 `--env-file` 读取，绝不写入证据/日志。
**会消耗两次真实模型调用**（一次短回合、一次中途 abort 的长输出回合）。

| 脚本 | 用途 | 证据 |
|---|---|---|
| `rpc_channel_probe.py` | 验证架构评审第 3 组前提：pi 的 E2B 面能否收敛到 RPC 通道。生产渲染与上传路径（`build_config` → `prepare_agent_dir` → `E2BSandboxClient.upload_config`）起 `pi-glink-worker` 沙箱；`pi --mode rpc` 以 `background=True, stdin=True` 启动，`subprocess.Popen` 注入点替换为 `SandboxCommandProcess` 子类后用仓库自己的 `PiRpcClient` 驱动：get_state 握手、完整回合、运行中 abort（旧 `--mode json` 路径做不到）、abort 后状态、`close_stdin` 退出行为 | `docs/evidence/pi-e2b-rpc-stdin-spike.txt` |

结论（2026-09-26，15/15 PASS）：RPC 通道在真实沙箱可用，运行中 abort 可用，`close_stdin`
后 pi 以退出码 0 结束。收敛到 `RpcExecutionAdapter` 的缺口只有一个已记录项：
`PiRpcClient` 读 `returncode`，需上移到生产 `SandboxCommandProcess`。收敛本身是另案重构
（删掉 tee 补读/LFLineFramer/不能 abort 的旧路径，断流恢复需重新设计），不在本探针范围。

复现命令（在仓库根目录；`--env-file` 指向含 `E2B_API_KEY` 与 `OPENAI_*` 的冻结配置）：

```bash
.venv/bin/python scripts/spikes/pi_e2b_rpc/rpc_channel_probe.py --env-file .env
```
