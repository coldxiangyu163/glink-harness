#!/usr/bin/env python3
"""pi ``--mode rpc`` 经 E2B 双向 stdio 的真实沙箱探针（GLINK-ARCH-3 spike）。

回答架构评审第 3 组的问题：pi-integration-notes.md 的旧前提「SDK 不提供持久
stdin」已被 ACP 面实测推翻（docs/evidence/acp-m3-e2b-framing.txt），pi 的 E2B
面能否因此收敛到 RPC 通道（今 ``PiExecutionAdapter`` + ``E2BSandbox`` 的 ``SandboxCommandProcess``），
从而删掉 ``--mode json`` 单向流、tee 补读与「运行中不能 abort」的旧路径。

方法（尽量走生产路径）：

* ``build_config``（--env-file）→ ``prepare_agent_dir`` 渲染真实 runtime；
* ``E2BSandbox`` 起 ``pi-glink-worker`` 模板沙箱并上传配置（生产
  上传路径原样）；
* 沙箱内以 ``background=True, stdin=True`` 启动 ``pi --mode rpc``，用仓库自己的
  ``PiRpcClient`` 驱动——``subprocess.Popen`` 在注入点替换为
  ``SandboxCommandProcess`` 的子类（收敛后 adapter 要走的同一条路）；
* 实测：get_state 握手、一次短 prompt 完整回合（消耗一次真实模型调用）、
  长输出 prompt 中途 abort（旧路径做不到的事）、abort 后状态、close_stdin
  后的进程退出行为。

只创建并操作自己的沙箱，结束按 sandbox id 终止；不打印任何凭据值。
"""

from __future__ import annotations

import argparse
import datetime
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "host"))

from pi_glink.config import build_config  # noqa: E402
from pi_glink.kernel import rpc as rpc_module  # noqa: E402
from pi_glink.kernel.agent_env import EgressEndpoint, prepare_agent_dir  # noqa: E402
from pi_glink.kernel.e2b_env import (  # noqa: E402
    SANDBOX_WORKSPACE,
    PiSandboxTransfer,
    SandboxCommandProcess,
)
from pi_glink.kernel.execution import ExecutionSpec  # noqa: E402
from pi_glink.kernel.harness_pi import PiHarnessFamily  # noqa: E402
from pi_glink.kernel.sandbox import E2BSandbox  # noqa: E402
from pi_glink.store import RunStore  # noqa: E402
from pi_glink.kernel.rpc import PiRpcClient, wait_for_idle  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" :: {detail}" if detail else ""))
    return bool(ok)


def info(line: str) -> None:
    print(line, flush=True)


class RpcSandboxProcess(SandboxCommandProcess):
    """``SandboxCommandProcess`` + ``PiRpcClient`` 需要的 ``returncode`` 属性。

    ACP 客户端不读 ``returncode``，所以生产类上没有它；pi 的 RPC 客户端在
    ``exit_code``/``_write`` 两处读它。收敛时这个属性应上移到生产类——
    本探针如实记录这个缺口，而不是绕过它。
    """

    @property
    def returncode(self) -> int | None:
        return self.poll() if self._exited.is_set() else None  # noqa: SLF001


def load_env_file(path: str | None) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path:
        return values
    p = Path(path)
    if not p.is_file():
        return values
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def resolve_e2b_key(values: dict[str, str]) -> str:
    return os.environ.get("E2B_API_KEY") or values.get("E2B_API_KEY", "")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=".env", help="含 E2B_API_KEY 与 OPENAI_* 的冻结配置")
    parser.add_argument("--timeout", type=int, default=300, help="沙箱与命令寿命（秒）")
    parser.add_argument(
        "--evidence",
        default=str(REPO_ROOT / "docs" / "evidence" / "pi-e2b-rpc-stdin-spike.txt"),
        help="证据输出路径",
    )
    args = parser.parse_args()

    values = load_env_file(args.env_file)
    if not resolve_e2b_key(values):
        print("E2B_API_KEY 缺失（进程环境或 --env-file）", file=sys.stderr)
        return 2
    os.environ["E2B_API_KEY"] = resolve_e2b_key(values)

    started = datetime.datetime.now().astimezone()
    info("pi E2B RPC 通道实测（GLINK-ARCH-3 spike）")
    info(f"日期: {started:%Y-%m-%d %H:%M:%S}")

    sandbox_id = ""
    exit_code = 1
    with tempfile.TemporaryDirectory(prefix="pi-e2b-rpc-spike-") as tmp:
        tmp_path = Path(tmp)
        values["LOCAL_WEB_DATA_ROOT"] = str(tmp_path / "data")
        config = build_config(values, REPO_ROOT)
        info(f"模板: {config.e2b_template}（pi 面模板，与 ACP dev 模板无关）")
        try:
            import e2b as _e2b  # noqa: PLC0415

            info(f"e2b SDK: {getattr(_e2b, '__version__', 'unknown')}")
        except Exception:  # noqa: BLE001
            pass

        # Spike only: a placeholder egress endpoint (docs/adr/0008); a live probe
        # that needs the model must run behind a real host egress proxy.
        runtime = prepare_agent_dir(
            config,
            runtime_dir=tmp_path / "agent",
            egress=EgressEndpoint(model_base_url="http://127.0.0.1:9/model", token="pge_spike"),
        )
        run_id = f"spike-{int(time.time())}"
        sandbox = E2BSandbox(config=config, run_id=run_id, workspace=tmp_path / "workspace")
        spec = ExecutionSpec(
            config=config,
            runtime=runtime,
            run_id=run_id,
            prompt="",
            system_prompt="",
            workspace=tmp_path / "workspace",
            timeout_seconds=args.timeout,
        )
        family = PiHarnessFamily(config=config, store=RunStore(tmp_path / "runs"))
        launch = family.launch(spec, sandbox)
        try:
            sandbox.open(launch, timeout_seconds=args.timeout)
            sandbox_id = sandbox.sandbox_id
            info(f"sandbox_id: {sandbox_id}")

            # 1) 模板携带 pi，且版本符合钉住值。
            probe = sandbox.handle.commands.run("pi --version", timeout=60)
            version = str(probe.stdout).strip()
            check("模板内 pi 可用", bool(version), f"pi --version = {version!r}")

            # 2) 生产上传路径交付配置（auth.json/models.json 在 manifest 里）。
            transfer = PiSandboxTransfer(sandbox.handle, runtime)
            transfer.upload_config()
            names = [rel for rel, _ in transfer.upload_manifest]
            check(
                "配置上传（生产路径）",
                "auth.json" in names and "models.json" in names,
                f"files={len(names)}",
            )

            # 3) pi --mode rpc 以持久 stdin 启动，PiRpcClient 经沙箱外观驱动。
            command = (
                f"cd {SANDBOX_WORKSPACE} && "
                f"pi --mode rpc --provider {runtime.provider_id} --model {runtime.model_id}"
            )
            handle = sandbox.handle.commands.run(
                command,
                background=True,
                stdin=True,
                envs=dict(launch.env),  # 与生产命令同一组 envs
                timeout=args.timeout,
            )
            proc = RpcSandboxProcess(handle, sandbox_id=sandbox_id)
            events: list[dict] = []
            client = PiRpcClient(
                ["pi", "--mode", "rpc"],
                on_event=lambda frame: events.append(dict(frame)),
            )
            # 注入点：收敛后由 adapter 持有的进程外观。patch 只覆盖
            # PiRpcClient.start() 的这一次构造调用。
            with mock.patch.object(rpc_module.subprocess, "Popen", lambda *a, **k: proc):
                client.start()

            try:
                check("get_state 握手", client.is_ready(timeout=30.0))

                # 4) 短 prompt 完整回合（消耗一次真实模型调用）。
                accepted = client.request(
                    {"type": "prompt", "message": "Reply with exactly: SPIKE-OK"},
                    timeout=60.0,
                )
                if check("prompt 被受理", accepted.success, f"error={accepted.error}"):
                    state = wait_for_idle(client, timeout=180.0)
                    check("回合结束（isStreaming=False）", state is not None and not state.get("isStreaming"))
                    text = "".join(
                        str(
                            (
                                frame.get("assistantMessageEvent")
                                if isinstance(frame.get("assistantMessageEvent"), dict)
                                else {}
                            ).get("delta")
                            or frame.get("delta")
                            or ""
                        )
                        for frame in events
                        if isinstance(frame, dict)
                    )
                    # 事件形状如实记录：类型分布比文本内容更能说明流式到达。
                    types = sorted({str(f.get("type")) for f in events if isinstance(f, dict)})
                    check(
                        "回合文本经事件流到达",
                        "SPIKE-OK" in text,
                        f"event_types={types}",
                    )
                    state2 = client.request({"type": "get_state"}, timeout=15.0)
                    data = state2.data
                    check(
                        "回合后 get_state 事实",
                        state2.success and bool(data.get("sessionId")),
                        f"sessionId={'present' if data.get('sessionId') else 'missing'} "
                        f"isStreaming={data.get('isStreaming')}",
                    )

                # 5) 长输出 prompt 中途 abort——旧 --mode json 路径做不到的事。
                for frame in list(events):
                    frame.clear()
                events.clear()
                accepted2 = client.request(
                    {
                        "type": "prompt",
                        "message": (
                            "Count from 1 to 300, one number per line, "
                            "no commentary, do not stop early."
                        ),
                    },
                    timeout=60.0,
                )
                if check("第二个 prompt 被受理", accepted2.success, f"error={accepted2.error}"):
                    # 等到确实在流式输出再 abort（否则证明不了「运行中」）。
                    deadline = time.monotonic() + 90.0
                    streaming = False
                    while time.monotonic() < deadline:
                        probe = client.request({"type": "get_state"}, timeout=10.0)
                        if probe.success and probe.data.get("isStreaming"):
                            streaming = True
                            break
                        time.sleep(0.2)
                    check("abort 前确认在流式", streaming)
                    aborted = client.request({"type": "abort"}, timeout=15.0)
                    check("运行中 abort 被受理", aborted.success, f"error={aborted.error}")
                    state3 = wait_for_idle(client, timeout=30.0)
                    check(
                        "abort 后不再流式",
                        state3 is not None and not state3.get("isStreaming"),
                        f"isStreaming={state3.get('isStreaming') if state3 else 'unknown'}",
                    )
                    after = len(events)
                    time.sleep(2.0)
                    check(
                        "abort 后事件流停止增长",
                        len(events) <= after + 2,
                        f"events@{after} -> {len(events)}",
                    )

                # 6) close_stdin 后的退出行为如实记录（pi 是否响应 EOF）。
                rc = client.close(timeout=15.0)
                check(
                    "close 后进程退出",
                    rc is not None,
                    f"exit_code={rc}（None=仍在运行，需 kill）",
                )
            finally:
                if client.running:
                    proc.kill()
        finally:
            if sandbox_id:
                failures = sandbox.close().failures
                info(f"按 sandbox_id={sandbox_id} 终止: {not failures}")

    lines = ["", "结果汇总"]
    for name, ok, detail in RESULTS:
        lines.append(f"  {'PASS' if ok else 'FAIL'} {name}" + (f" :: {detail}" if detail else ""))
    for line in lines:
        info(line)
    failed = [name for name, ok, _ in RESULTS if not ok]
    if RESULTS and not failed:
        exit_code = 0

    evidence = Path(args.evidence)
    evidence.parent.mkdir(parents=True, exist_ok=True)
    header = [
        "pi --mode rpc 经 E2B 双向 stdio 通道实测（GLINK-ARCH-3 spike）",
        f"日期: {started:%Y-%m-%d %H:%M:%S}",
        f"模板: {config.e2b_template}",
        f"sandbox_id: {sandbox_id}",
        "凭据: E2B_API_KEY 与 OPENAI_API_KEY 经 --env-file 注入，未写入本证据",
        "",
    ]
    body = [f"{'PASS' if ok else 'FAIL'} {name}" + (f" :: {d}" if d else "") for name, ok, d in RESULTS]
    evidence.write_text("\n".join(header + body) + "\n", encoding="utf-8")
    info(f"证据: {evidence}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
