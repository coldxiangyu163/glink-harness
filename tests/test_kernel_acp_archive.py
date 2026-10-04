"""E2B 面归档通道全链路（docs/adr/0018）：fake 沙箱真跑 node/tar/curl。

这里的 fake 沙箱不跑 agent——它把 ``files.*`` 落在一棵真实目录树上，把
``commands.run`` 真实执行（``/home/user/acp/home`` 映射到 fake 根），于是
整条链路是真实的：宿主下发 node 打包器 → 沙箱端切片 + tar → curl POST 到
真实的 ``ThreadingHTTPServer`` listener → 宿主端流式落盘、校验、拼接、
替换式发布。回灌方向同理（curl GET + tar -x）。

覆盖：

* 首轮全量回收：conversation state 发布、manifest 事实、凭据目标不进归档；
* 第二轮增量回收：传输的 tar 只携带尾部成员（O(增量) 的直接证据）；
* 回灌：follow-up 沙箱拿到完整权威状态；
* 降级：无归档端点时 files 逐文件回收仍发布 conversation state；
* 回收门禁新鲜度：manifest 的 ``sourceRunId`` 不是父 run 时追问被拒。

容器契约与 HTTP 面的单元测试见 ``tests/test_archive_channel.py``。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tarfile
from pathlib import Path
from typing import Any

import pytest

from glink_harness.egress import EgressProxy
from glink_harness.kernel.acp_e2b_env import (
    BACKFILL_TAR_PATH,
    RECLAIM_TAR_PATH,
    AcpSandboxTransfer,
)
from glink_harness.kernel.acp_env import AcpRuntime
from glink_harness.kernel.archive_channel import ArchiveEndpoint
from glink_harness.kernel.sandbox import SANDBOX_FAMILY_STATE_HOME

pytestmark = pytest.mark.usefixtures("clean_reclaim_scratch")

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def clean_reclaim_scratch():
    """The packer's scratch paths live in the real /tmp; keep them tidy."""
    paths = [Path(RECLAIM_TAR_PATH), Path(BACKFILL_TAR_PATH)]
    for path in paths:
        path.unlink(missing_ok=True)
    yield
    for path in paths:
        path.unlink(missing_ok=True)


def _need_host_tools() -> None:
    missing = [tool for tool in ("node", "tar", "curl") if shutil.which(tool) is None]
    if missing:
        pytest.skip(f"宿主缺少 {missing}，无法真跑归档链路")


class _Info:
    def __init__(self, size: int) -> None:
        self.size = size


class _ChunkStream:
    """The SDK's ``format="stream"`` shape: chunks plus close."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self.closed = False

    def __iter__(self) -> Any:
        return iter(self._chunks)

    def close(self) -> None:
        self.closed = True


class _Entry:
    def __init__(self, path: str, real: Path) -> None:
        self.path = path
        self.type = "symlink" if real.is_symlink() else ("dir" if real.is_dir() else "file")
        self.size = real.stat().st_size if self.type == "file" else 0


class _Files:
    def __init__(self, sandbox: "RealCommandFakeSandbox") -> None:
        self._sandbox = sandbox

    def _real(self, path: str) -> Path:
        # Scratch paths (/tmp/…) are the host's own: the fake's commands run
        # on the host too, so they read exactly these files.
        if path.startswith("/tmp/"):
            return Path(path)
        return self._sandbox.translate(path)

    def make_dir(self, path: str) -> None:
        self._real(path).mkdir(parents=True, exist_ok=True)

    def write(self, path: str, content: Any, **_: Any) -> None:
        data = content if isinstance(content, bytes) else str(content).encode()
        target = self._real(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def read(self, path: str, format: str | None = None, **_: Any) -> Any:
        data = self._real(path).read_bytes()
        if format == "stream":
            return _ChunkStream([data])
        return data

    def get_info(self, path: str, **_: Any) -> _Info:
        return _Info(self._real(path).stat().st_size)

    def list(self, path: str, depth: int = 1, **_: Any) -> list[_Entry]:
        # This fake virtualizes /home/user as root, with trusted virtual
        # ancestors. Immediate entry kinds model the SDK's no-follow lookup.
        if path == "/":
            return [_Entry("/home", self._sandbox.root.parent)]
        if path == "/home":
            return [_Entry("/home/user", self._sandbox.root)]
        base = self._real(path)
        found: list[_Entry] = []
        for child in sorted(base.rglob("*")):
            relative = child.relative_to(base)
            if len(relative.parts) > depth:
                continue
            found.append(_Entry(f"{path}/{relative.as_posix()}", child))
        return found


class _Result:
    def __init__(self, exit_code: int) -> None:
        self.exit_code = exit_code


class _ExitException(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(f"command exited with {code}")
        self.exit_code = code


class _Commands:
    def __init__(self, sandbox: "RealCommandFakeSandbox") -> None:
        self._sandbox = sandbox

    def run(self, cmd: str, timeout: float | None = None, **_: Any) -> _Result:
        # Sandbox paths mean nothing to a host shell: map the state root onto
        # the fake tree (the commands are host-constructed and quote every
        # argument, so the literal prefix can only be our own constant).
        # Map longest sandbox prefixes through placeholders, so replacing
        # /home later cannot rewrite an already translated host path.
        mapped = cmd
        mappings = [(SANDBOX_FAMILY_STATE_HOME, self._sandbox.translate(SANDBOX_FAMILY_STATE_HOME)),
                    ("/home/user/acp", self._sandbox.root / "acp"),
                    ("/home/user", self._sandbox.root), ("/home", self._sandbox.root.parent)]
        for index, (prefix, _real) in enumerate(mappings):
            mapped = mapped.replace(prefix, f"__ACP_ROOT_{index}__")
        for index, (_prefix, real) in enumerate(mappings):
            mapped = mapped.replace(f"__ACP_ROOT_{index}__", str(real))
        self._sandbox.commands_run.append(mapped)
        completed = subprocess.run(
            ["bash", "-c", mapped],
            capture_output=True,
            timeout=timeout or 120,
        )
        if completed.returncode != 0:
            raise _ExitException(completed.returncode)
        return _Result(completed.returncode)


class RealCommandFakeSandbox:
    """A fake sandbox whose files and commands are real."""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.commands_run: list[str] = []
        self.files = _Files(self)
        self.commands = _Commands(self)
        self.killed = False
        self.sandbox_id = "fake-archive-sandbox"

    def translate(self, path: str) -> Path:
        if path == "/home/user":
            return self.root
        assert path.startswith("/home/user/")
        return self.root / path[len("/home/user/"):]

    def kill(self) -> None:
        self.killed = True


def make_runtime(tmp_path: Path, *, dests: tuple[str, ...] = (".dsh/.credentials.yaml",)) -> AcpRuntime:
    directory = tmp_path / "acp-agent"
    home = directory / "home"
    home.mkdir(parents=True, exist_ok=True)
    return AcpRuntime(
        directory=directory,
        home=home,
        command="dsh",
        credential_dests=dests,
    )


@pytest.fixture()
def conversation(tmp_path: Path) -> Path:
    path = tmp_path / "conversations" / "r1"
    path.mkdir(parents=True)
    return path


@pytest.fixture()
def proxy(conversation: Path) -> EgressProxy:
    proxy = EgressProxy(model_upstream="https://m.example", search_upstream="https://s.example")
    yield proxy
    proxy.close()


def make_transfer(
    sandbox: RealCommandFakeSandbox,
    runtime: AcpRuntime,
    *,
    proxy: EgressProxy | None,
    conversation: Path,
    run_id: str,
    workspace: Path,
) -> AcpSandboxTransfer:
    archive = None
    if proxy is not None:
        grant = proxy.issue_archive(
            run_id=run_id,
            conversation_dir=conversation,
            credential_dests=runtime.credential_dests,
        )
        assert proxy.port is not None
        archive = ArchiveEndpoint(
            origin=f"http://127.0.0.1:{proxy.port}", token=grant.token
        )
    return AcpSandboxTransfer(
        sandbox,
        runtime,
        workspace=workspace,
        archive=archive,
        conversation_dir=conversation,
        run_id=run_id,
    )


def state_files(conversation: Path) -> dict[str, bytes]:
    state = conversation / "state"
    return {
        p.relative_to(state).as_posix(): p.read_bytes()
        for p in sorted(state.rglob("*"))
        if p.is_file() and p.name != "manifest.json"
    }


def seed_state(sandbox: RealCommandFakeSandbox, session: bytes) -> None:
    sandbox.files.make_dir(f"{SANDBOX_FAMILY_STATE_HOME}/.dsh/sessions")
    sandbox.files.write(f"{SANDBOX_FAMILY_STATE_HOME}/.dsh/sessions/s.v4.jsonl.zstd", session)
    # The credential copy the host uploaded: it must never enter an archive.
    sandbox.files.write(f"{SANDBOX_FAMILY_STATE_HOME}/.dsh/.credentials.yaml", b"version: 1")


# -- reclaim ---------------------------------------------------------------------


def test_full_reclaim_publishes_and_excludes_credentials(
    tmp_path: Path, conversation: Path, proxy: EgressProxy
) -> None:
    _need_host_tools()
    sandbox = RealCommandFakeSandbox(tmp_path / "fake")
    runtime = make_runtime(tmp_path)
    transfer = make_transfer(
        sandbox, runtime, proxy=proxy, conversation=conversation,
        run_id="r1", workspace=tmp_path / "ws",
    )
    seed_state(sandbox, b"zstd-frame-1")
    recovery = transfer.recover_state()
    assert recovery.ok, recovery.failures
    assert recovery.mode == "archive-full"
    assert recovery.files == (".dsh/sessions/s.v4.jsonl.zstd",)
    assert state_files(conversation) == {".dsh/sessions/s.v4.jsonl.zstd": b"zstd-frame-1"}
    manifest = json.loads((conversation / "state" / "manifest.json").read_text())
    assert manifest["sourceRunId"] == "r1"
    assert manifest["mode"] == "full"


def test_second_round_reclaim_sends_only_the_tail(
    tmp_path: Path, conversation: Path, proxy: EgressProxy
) -> None:
    _need_host_tools()
    runtime = make_runtime(tmp_path)
    first = make_transfer(
        RealCommandFakeSandbox(tmp_path / "fake1"), runtime, proxy=proxy,
        conversation=conversation, run_id="r1", workspace=tmp_path / "ws",
    )
    seed_state(tmp_path / "fake1" and first.handle, b"zstd-frame-1")
    assert first.recover_state().ok

    # Round two: a fresh sandbox whose session file kept the old prefix and
    # appended (the append-only fact V1 verified for dsh's session store).
    second_sandbox = RealCommandFakeSandbox(tmp_path / "fake2")
    seed_state(second_sandbox, b"zstd-frame-1" + b"-appended-round-2")
    second = make_transfer(
        second_sandbox, runtime, proxy=proxy, conversation=conversation,
        run_id="r2", workspace=tmp_path / "ws",
    )
    recovery = second.recover_state()
    assert recovery.ok, recovery.failures
    assert recovery.mode == "archive-incremental"
    assert state_files(conversation) == {
        ".dsh/sessions/s.v4.jsonl.zstd": b"zstd-frame-1-appended-round-2"
    }
    # The transported archive itself carried only the tail member: the O(增量)
    # contract, checked on the bytes that actually crossed the wire (directory
    # records and the ``./`` prefix both normalize away).
    with tarfile.open(RECLAIM_TAR_PATH) as tar:
        names = sorted(
            m.name.lstrip("./")
            for m in tar.getmembers()
            if m.isfile()
        )
    assert names == ["delta/.dsh/sessions/s.v4.jsonl.zstd", "manifest.json"]
    with tarfile.open(RECLAIM_TAR_PATH) as tar:
        tail = tar.extractfile("./delta/.dsh/sessions/s.v4.jsonl.zstd")
        assert tail is not None and tail.read() == b"-appended-round-2"
    manifest = json.loads((conversation / "state" / "manifest.json").read_text())
    assert manifest["sourceRunId"] == "r2"
    assert manifest["mode"] == "incremental"


def test_files_fallback_publishes_when_there_is_no_channel(
    tmp_path: Path, conversation: Path
) -> None:
    _need_host_tools()
    sandbox = RealCommandFakeSandbox(tmp_path / "fake")
    runtime = make_runtime(tmp_path)
    transfer = make_transfer(
        sandbox, runtime, proxy=None, conversation=conversation,
        run_id="r1", workspace=tmp_path / "ws",
    )
    seed_state(sandbox, b"plain-state")
    recovery = transfer.recover_state()
    assert recovery.ok, recovery.failures
    assert recovery.mode == "files-fallback"
    assert state_files(conversation) == {".dsh/sessions/s.v4.jsonl.zstd": b"plain-state"}
    manifest = json.loads((conversation / "state" / "manifest.json").read_text())
    assert manifest["sourceRunId"] == "r1"
    assert manifest["mode"] == "files-fallback"


def test_failed_channel_degrades_to_the_files_walk(
    tmp_path: Path, conversation: Path, proxy: EgressProxy
) -> None:
    _need_host_tools()
    sandbox = RealCommandFakeSandbox(tmp_path / "fake")
    runtime = make_runtime(tmp_path)
    transfer = make_transfer(
        sandbox, runtime, proxy=proxy, conversation=conversation,
        run_id="r1", workspace=tmp_path / "ws",
    )
    seed_state(sandbox, b"state-via-fallback")
    # Break the channel after the grant was issued: the curl target dies.
    proxy.close()
    recovery = transfer.recover_state()
    assert recovery.ok, recovery.failures
    assert recovery.mode == "files-fallback"
    assert state_files(conversation) == {".dsh/sessions/s.v4.jsonl.zstd": b"state-via-fallback"}


# -- backfill ----------------------------------------------------------------------


def test_backfill_pulls_the_published_state_into_the_sandbox(
    tmp_path: Path, conversation: Path, proxy: EgressProxy
) -> None:
    _need_host_tools()
    runtime = make_runtime(tmp_path)
    first = make_transfer(
        RealCommandFakeSandbox(tmp_path / "fake1"), runtime, proxy=proxy,
        conversation=conversation, run_id="r1", workspace=tmp_path / "ws",
    )
    seed_state(first.handle, b"authoritative-bytes")
    assert first.recover_state().ok

    follow_up = RealCommandFakeSandbox(tmp_path / "fake2")
    second = make_transfer(
        follow_up, runtime, proxy=proxy, conversation=conversation,
        run_id="r2", workspace=tmp_path / "ws",
    )
    second.backfill_state()
    assert second.backfill_facts is not None
    assert second.backfill_facts["bytes"] == len(b"authoritative-bytes")
    assert second.backfill_facts["files"] == 1
    assert second.backfill_facts["durationMs"] >= 0
    assert (
        follow_up.translate(f"{SANDBOX_FAMILY_STATE_HOME}/.dsh/sessions/s.v4.jsonl.zstd")
        .read_bytes()
        == b"authoritative-bytes"
    )
    # The manifest never travels to the sandbox (it is host bookkeeping).
    assert not follow_up.translate(f"{SANDBOX_FAMILY_STATE_HOME}/manifest.json").exists()


def test_real_archive_backfill_repairs_only_the_target_copy(tmp_path, conversation, proxy) -> None:
    """Actual tar/curl channel → copied HOME → real codec/atomic rename."""
    from glink_harness.kernel.acp_state_compat import repair_resume_home
    from test_kernel_acp_state_compat import decoded, rows, seed

    _need_host_tools()
    runtime = make_runtime(tmp_path)
    source = RealCommandFakeSandbox(tmp_path / "legacy")
    source_home = source.translate(SANDBOX_FAMILY_STATE_HOME)
    original, data = seed(source_home)
    first = make_transfer(source, runtime, proxy=proxy, conversation=conversation,
                          run_id="r1", workspace=tmp_path / "ws")
    assert first.recover_state().ok
    authority = conversation / "state" / original.relative_to(source_home)
    manifest = (conversation / "state/manifest.json").read_bytes()
    target = RealCommandFakeSandbox(tmp_path / "followup")
    second = make_transfer(target, runtime, proxy=proxy, conversation=conversation,
                           run_id="r2", workspace=tmp_path / "ws")
    second.backfill_state()
    copied = target.translate(SANDBOX_FAMILY_STATE_HOME) / original.relative_to(source_home)
    assert copied.read_bytes() == data
    repair_resume_home(SANDBOX_FAMILY_STATE_HOME, handle=target)
    expected = rows()
    expected[1]["ignorable"] = True
    assert decoded(copied.read_bytes()) == expected
    assert authority.read_bytes() == data and original.read_bytes() == data
    assert (conversation / "state/manifest.json").read_bytes() == manifest
    assert (copied.parent / "session.lock").read_bytes() == b""
    assert not list(copied.parent.glob("*.acp15-*"))


def test_backfill_failure_fails_the_run(
    tmp_path: Path, conversation: Path, proxy: EgressProxy
) -> None:
    _need_host_tools()
    runtime = make_runtime(tmp_path)
    sandbox = RealCommandFakeSandbox(tmp_path / "fake")
    transfer = make_transfer(
        sandbox, runtime, proxy=proxy, conversation=conversation,
        run_id="r2", workspace=tmp_path / "ws",
    )
    proxy.close()  # the follow-up was admitted against state it can no longer reach
    from glink_harness.kernel.e2b_env import SandboxError

    with pytest.raises(SandboxError, match="回灌"):
        transfer.backfill_state()


# -- Prepare issues the archive grant on a remote ACP face -------------------------


class _FaceAdmission:
    """The one StageServices collaborator archive_endpoint actually reads."""

    def __init__(self, family: Any) -> None:
        self._family = family

    def family_of(self, record: Any) -> Any:
        return self._family


class _RemoteSandbox:
    remote = True

    def __init__(self, *, origin: str | None) -> None:
        self._origin = origin

    def egress_origin(self, port: int) -> str:
        if self._origin is None:
            from glink_harness.config import ConfigError

            raise ConfigError("no public url")
        return self._origin


def test_prepare_issues_an_archive_grant_for_a_remote_acp_face(tmp_path: Path) -> None:
    from glink_harness.config import build_config
    from glink_harness.kernel.harness_acp import AcpHarnessFamily
    from glink_harness.kernel.run_views import RunStoreSessionSource
    from glink_harness.redaction import SecretRedactor
    from glink_harness.runs.secrets import RunSecretBoundary
    from glink_harness.runs.state import RunExecution
    from glink_harness.runs.stages import PrepareStage, StageServices
    from glink_harness.store import RunRecord, RunSpec, RunStore

    from acp_pack import apply_acp_pack

    values = {
        "OPENAI_API_KEY": "sk-test",
        "OPENAI_BASE_URL": "https://example.invalid/v1",
        "OPENAI_MODEL": "test/model",
        "LOCAL_WEB_DATA_ROOT": str(tmp_path / "data"),
        "LOCAL_WEB_EXECUTOR": "inproc",
        "GLINK_HARNESS": "acp",
        "TEST_ACP_COMMAND": "dsh --profile acp",
    }
    apply_acp_pack(values, tmp_path)
    config = build_config(values, REPO_ROOT)

    store = RunStore(tmp_path / "runs")
    # R1-3 评审 SHOULD：构造点只收 SessionSource 协议对象（「注入什么持有
    # 什么」），仓外调用点自己包 RunStoreSessionSource 适配器。
    family = AcpHarnessFamily(config=config, store=RunStoreSessionSource(store))
    secrets = RunSecretBoundary(SecretRedactor(()), run_id="run000001")
    services = StageServices(
        config=config,
        store=store,
        bus=None,
        egress=EgressProxy(
            model_upstream="https://m.example", search_upstream="https://s.example"
        ),
        admission=_FaceAdmission(family),
        channel=None,
        secrets=lambda _execution: secrets,
        plugins=lambda _execution: (),
        sandbox_factory=lambda: None,
        client_factory=lambda: None,
    )
    try:
        record = RunRecord(
            run_id="run000001",
            prompt="第一轮",
            status="running",
            created_at=0.0,
            workspace=str(tmp_path / "ws"),
            spec=RunSpec(family="acp", pack="dsh", executor="e2b"),
        )
        execution = RunExecution(record=record, workspace=Path(record.workspace))
        transfer = family.state_transfer(record, parent=None, source=None,
                                         sandbox=_RemoteSandbox(origin="http://pub.example"))
        assert transfer is not None
        reachable = PrepareStage(services).archive_endpoint(
            execution, _RemoteSandbox(origin="http://pub.example"), secrets, transfer
        )
        assert reachable is not None
        assert reachable.url == "http://pub.example/archive/state"
        assert reachable.token.startswith("pga_")
        assert execution.conversation_dir == store.conversation_dir("run000001")
        assert secrets.redact(reachable.token) != reachable.token
        # Without a reachable origin the run keeps the host-side path (None),
        # never a refusal.
        other = RunExecution(record=record, workspace=Path(record.workspace))
        assert (
            PrepareStage(services).archive_endpoint(
                other, _RemoteSandbox(origin=None), secrets, transfer
            )
            is None
        )
    finally:
        services.egress.close()


def test_channel_success_without_this_runs_manifest_degrades(
    tmp_path: Path, conversation: Path, proxy: EgressProxy
) -> None:
    """命令成功但发布者不是本 run（陈旧 manifest）：降级 files 逐文件。"""
    _need_host_tools()
    sandbox = RealCommandFakeSandbox(tmp_path / "fake")
    runtime = make_runtime(tmp_path)
    # The transfer speaks for run r9, but its grant was minted for another id
    # (a mis-wired host path): the POST succeeds and publishes *that* run's
    # manifest, so the freshness check must refuse it and degrade.
    transfer = make_transfer(
        sandbox, runtime, proxy=proxy, conversation=conversation,
        run_id="r9", workspace=tmp_path / "ws",
    )
    transfer.archive = ArchiveEndpoint(
        origin=transfer.archive.origin, token=transfer.archive.token
    )
    from glink_harness.egress import EgressProxy as _Proxy  # noqa: F401 - clarity
    grant = proxy.archive.grant_for(transfer.archive.token)
    assert grant is not None
    proxy.revoke_archive(grant.token)
    reissued = proxy.issue_archive(
        run_id="someone-else",
        conversation_dir=conversation,
        credential_dests=runtime.credential_dests,
    )
    transfer.archive = ArchiveEndpoint(
        origin=transfer.archive.origin, token=reissued.token
    )
    seed_state(sandbox, b"state-9")
    recovery = transfer.recover_state()
    assert recovery.ok, recovery.failures
    assert recovery.mode == "files-fallback"
    # The stale manifest has been replaced by this run's own fallback publish.
    manifest = json.loads((conversation / "state" / "manifest.json").read_text())
    assert manifest["sourceRunId"] == "r9"
    assert manifest["mode"] == "files-fallback"


# -- swap 崩溃窗口自愈（cc 终审 S4） ----------------------------------------------


def test_a_crashed_swap_is_healed_on_the_next_read_or_publish(tmp_path: Path) -> None:
    """state→retired 后、staging→state 前崩溃：下次读取/发布把 retired 还原。"""
    from glink_harness.kernel.archive_channel import _heal_swap_window, read_state_manifest

    state = tmp_path / "state"
    retired = tmp_path / ".state.retired"
    state.mkdir()
    (state / "manifest.json").write_text(
        '{"version": 1, "sourceRunId": "r1", "files": {}}', encoding="utf-8"
    )
    # 模拟崩溃窗口：旧树已挪走、新树未落位
    state.rename(retired)
    assert read_state_manifest(tmp_path) is not None, "读取路径自愈后仍能读到 manifest"
    assert state.is_dir() and not retired.exists()

    # 同窗口再次出现时，_heal_swap_window 直接还原
    state.rename(retired)
    _heal_swap_window(tmp_path)
    assert state.is_dir()


def test_a_failed_token_cleanup_never_fails_the_transfer(tmp_path, proxy) -> None:
    """token 清理命令失败：只告警，传输/发布不受影响（S2 best-effort 契约）。"""
    _need_host_tools()
    runtime = make_runtime(tmp_path)
    sandbox = RealCommandFakeSandbox(tmp_path / "fake")
    real_run = sandbox.commands.run

    def run(cmd, timeout=None, **kwargs):
        if cmd.startswith("rm -f /tmp/glink-harness-archive-token"):
            raise RuntimeError("cleanup refused")
        return real_run(cmd, timeout=timeout, **kwargs)

    sandbox.commands.run = run  # type: ignore[method-assign]
    conversation = tmp_path / "conversations" / "c1"
    conversation.mkdir(parents=True)
    transfer = make_transfer(
        sandbox, runtime, proxy=proxy, conversation=conversation,
        run_id="r1", workspace=tmp_path / "ws",
    )
    seed_state(sandbox, b"zstd-frame-1")
    recovery = transfer.recover_state()
    assert recovery.ok, recovery.failures
    from glink_harness.kernel.archive_channel import read_state_manifest

    assert read_state_manifest(conversation) is not None, "发布不受清理失败影响"
