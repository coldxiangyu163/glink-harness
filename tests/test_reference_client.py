"""R2（multi-harness-plan §6）：独立脚本客户端经接口完成执行与重放。

验收口径的两半都在这里：子进程真实跑 ``scripts/glink-api-client.py``
（创建 → 直播到终帧 → 详情 → 重放比对），以及机械门禁——脚本的导入面
⊆ 标准库且源码不出现 ``glink_harness``，即它不导入产品代码，只消费
HTTP/SSE 接口。运行子进程时显式剥离 ``PYTHONPATH``：隔离是脚本的
真实运行方式，不依赖门禁环境的路径巧合。
"""

from __future__ import annotations

import ast
import http.server
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from glink_harness.app import GlinkHarnessServer, create_server
from glink_harness.config import build_config
from glink_harness.kernel.execution import (
    ArtifactRecovery,
    CleanupReport,
    ExecutionAdapter,
    ExecutionOutcome,
    SessionInfo,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
CLIENT = REPO_ROOT / "scripts" / "glink-api-client.py"


def _product_free_env() -> dict[str, str]:
    """子进程环境：仓库不出现在导入路径里（参考客户端的真实运行方式）。"""
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    return env


def make_config(tmp_path: Path, **overrides: str):
    values = {
        "OPENAI_API_KEY": "sk-test",
        "OPENAI_BASE_URL": "https://example.invalid/v1",
        "OPENAI_MODEL": "test/model",
        "PI_CODING_AGENT_DIR": str(REPO_ROOT / "harnesses" / "pi"),
        "GLINK_HARNESS": "pi",
        "LOCAL_WEB_DATA_ROOT": str(tmp_path / "data"),
        "LOCAL_WEB_PORT": "0",
    }
    values.update(overrides)
    return build_config(values, REPO_ROOT)


class _Adapter(ExecutionAdapter):
    """A run that settles at once — the reference flow never needs pi."""

    kind = "inproc"

    def __init__(self) -> None:
        super().__init__(None, on_frame=lambda frame: None)  # type: ignore[arg-type]

    def start(self) -> None:
        return None

    def session_info(self) -> SessionInfo | None:
        return None

    def deliver_input(self) -> None:
        return None

    def wait(self, *, cancel, deadline: float) -> ExecutionOutcome:
        return ExecutionOutcome(settled=True, cancelled=False)

    def cancel(self) -> None:
        return None

    def recover_artifacts(self, destination: Path) -> ArtifactRecovery:
        return ArtifactRecovery()

    def close(self) -> CleanupReport:
        return CleanupReport(released=True)

    def identity(self) -> dict[str, str]:
        return {}


@pytest.fixture()
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> GlinkHarnessServer:
    monkeypatch.setattr(
        "glink_harness.kernel.harness_pi.PiExecutionAdapter", lambda spec, **kwargs: _Adapter()
    )
    server = create_server(make_config(tmp_path))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def test_the_reference_client_executes_streams_and_replays(host: GlinkHarnessServer) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(CLIENT),
            "--base-url",
            f"http://127.0.0.1:{host.server_address[1]}",
            "--prompt",
            "参考客户端的一次 API-only 执行",
            "--timeout",
            "60",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=REPO_ROOT,
        env=_product_free_env(),
    )
    assert result.returncode == 0, result.stderr
    assert "terminal: run_finished" in result.stdout
    assert "status: finished" in result.stdout
    assert "sequences match: True" in result.stdout


def test_the_reference_client_replays_with_an_idempotency_key(
    host: GlinkHarnessServer,
) -> None:
    """R3 冒烟：--idempotency-key 透传到头，同键两次执行是同一个 run。"""
    base = [
        "--base-url",
        f"http://127.0.0.1:{host.server_address[1]}",
        "--timeout",
        "60",
        "--idempotency-key",
        "ref-1",
    ]
    runs: list[str] = []
    for _attempt in range(2):
        result = subprocess.run(
            [
                sys.executable,
                str(CLIENT),
                *base,
                "--prompt",
                "参考客户端的幂等创建",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=REPO_ROOT,
            env=_product_free_env(),
        )
        assert result.returncode == 0, result.stderr
        (run_line,) = [
            line for line in result.stdout.splitlines() if line.startswith("run: ")
        ]
        runs.append(run_line.removeprefix("run: "))
    assert runs[0] == runs[1], "同键两次执行必须重放同一个 run"
    assert [record.run_id for record in host.store.list(limit=None)] == [runs[0]]


def test_the_reference_client_imports_no_product_code() -> None:
    """机械门禁：脚本只消费接口——导入面 ⊆ 标准库，源码不出现 glink_harness。

    两层互补：字符串检查拦直接引用；AST 门禁拦拼接/别名形态的静态导入
    与字面量 ``import_module``/``__import__``（顶层名必须是标准库模块，
    ``glink``/``glink_harness`` 及任何第三方包都进不来）。
    """
    source = CLIENT.read_text(encoding="utf-8")
    assert "glink_harness" not in source

    imported: set[str] = set()
    for node in ast.walk(ast.parse(source, filename=str(CLIENT))):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call) and node.args:
            function = node.func
            dynamic = isinstance(function, ast.Name) and function.id == "__import__"
            dynamic |= (
                isinstance(function, ast.Attribute)
                and isinstance(function.value, ast.Name)
                and function.value.id == "importlib"
                and function.attr == "import_module"
            )
            if (
                dynamic
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                imported.add(node.args[0].value.split(".")[0])
    non_standard = imported - set(sys.stdlib_module_names)
    assert non_standard == set(), f"参考客户端引入了非标准库导入: {sorted(non_standard)}"


def _load_client_module():
    """把参考客户端作为模块载入（多字节切分回归直接调 stream_events）。"""
    spec = importlib.util.spec_from_file_location("glink_api_client", CLIENT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_stream_events_survives_multibyte_frames_split_across_chunks() -> None:
    """回归：多字节 UTF-8 字符跨 chunk 边界切断时帧解析仍完整。

    服务端 ``ensure_ascii=False`` 原样输出 UTF-8，中文 prompt 的帧可在任意
    字节处被 HTTP chunk/TCP 边界切开；按 chunk 独立解码会抛
    UnicodeDecodeError 使整个参考流程失败。帧必须在字节层积累、只在完整
    块上解码。
    """
    prompt = "参考客户端的一次 API-only 执行"
    frame = (
        "id: 1\n"
        "event: run_started\n"
        f"data: {json.dumps({'prompt': prompt, 'type': 'run_started'}, ensure_ascii=False)}\n"
        "\n"
    ).encode("utf-8")
    terminal = (
        "id: 2\n"
        "event: run_finished\n"
        f"data: {json.dumps({'type': 'run_finished'}, ensure_ascii=False)}\n"
        "\n"
    ).encode("utf-8")
    # 切点落在“参”字的 UTF-8 序列中间：第一个 HTTP chunk 以半个字符收尾。
    payload_start = frame.index("参".encode("utf-8"))
    cut = payload_start + 1
    head, tail = frame[:cut], frame[cut:]

    class _SplittingHandler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802 - http.server 命名
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for piece in (head, tail, terminal):
                self.wfile.write(f"{len(piece):x}\r\n".encode("ascii") + piece + b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()

        def log_message(self, *args: object) -> None:
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _SplittingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = _load_client_module()
        frames = client.stream_events(
            "127.0.0.1",
            server.server_address[1],
            "run-under-test",
            stop_at_terminal=False,
            deadline=time.monotonic() + 30.0,
        )
    finally:
        server.shutdown()
        server.server_close()

    assert [frame_[1] for frame_ in frames] == ["run_started", "run_finished"]
    assert frames[0][0] == 1 and frames[1][0] == 2
    assert frames[0][2] is not None and frames[0][2]["prompt"] == prompt
