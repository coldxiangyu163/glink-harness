"""对话租约台账与沙箱泊车（阶段 2，docs/adr/0020）的单元回归。

覆盖三层：

* **台账事实**（``kernel/conversation_lease.py``）：lease.json 的原子读写、
  字段校验（缺字段/错形状/代数非法一律 ``None``，不猜）、到期判定、
  config 旋钮与常量的对齐（默认 TTL、硬上限、0=禁用）；
* **泊车编排**（``kernel/acp_sandbox_park.py``）：成功链（删凭据副本 →
  写代数标记 → 写租约 → pause(keep_memory=False)）、代数递增、被替换旧
  沙箱的销毁与挂账（retired）、每一步失败的销毁降级（TTL=0、删凭据失败、
  写标记失败、pause 失败）；
* **沙箱侧标记**：写读回、损坏/缺失 → ``None``。

沙箱是内存 fake（files/pause/kill 表面）；不启动真实 agent，不碰网络。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from pi_glink.config import ConfigError, build_config
from pi_glink.kernel.acp_e2b_env import SANDBOX_ACP_HOME
from pi_glink.kernel.acp_sandbox_park import (
    GENERATION_MARKER_PATH,
    park_sandbox,
    read_generation_marker,
    write_generation_marker,
)
from pi_glink.kernel.conversation_lease import (
    LeaseCorruptError,
    settle_lease_after_destroy,
    DEFAULT_LEASE_TTL_SECONDS,
    LEASE_FILENAME,
    MAX_LEASE_TTL_SECONDS,
    ConversationLease,
    expire_lease_now,
    read_lease,
    remove_lease,
    write_lease,
)
from pi_glink.kernel.e2b_env import SandboxError

from e2b_fake import FakeFileNotFoundException, FakeSandbox


# -- 台账事实 -------------------------------------------------------------------


def test_lease_roundtrip_and_validation(tmp_path: Path) -> None:
    lease = ConversationLease(
        conversation_run="r1",
        sandbox_id="sbx-1",
        generation=3,
        template="glink-acp-worker",
        pack="dsh",
        run_id="r4",
        paused_at=1000.0,
        expires_at=1000.0 + 7200,
    )
    write_lease(tmp_path, lease)
    assert (tmp_path / LEASE_FILENAME).read_text(encoding="utf-8").startswith("{")
    assert read_lease(tmp_path) == lease
    remove_lease(tmp_path)
    assert read_lease(tmp_path) is None


def test_unparsable_or_invalid_lease_is_corrupt_not_absent(tmp_path: Path) -> None:
    """损坏租约不再当作无租约：抛 LeaseCorruptError，沙箱不会被静默遗忘（cc 终审 B2）。"""
    path = tmp_path / LEASE_FILENAME
    path.write_text("not json", encoding="utf-8")
    with pytest.raises(LeaseCorruptError):
        read_lease(tmp_path)
    for bad in (
        {},  # 缺字段
        {"version": 1, "conversationRun": "r1"},  # 其余缺
        {
            "version": 1,
            "conversationRun": "r1",
            "sandboxId": "s",
            "generation": 0,  # 代数必须 >= 1
            "template": "t",
            "pack": "p",
            "runId": "r2",
            "pausedAt": 1.0,
            "expiresAt": 2.0,
        },
        {
            "version": 1,
            "conversationRun": "r1",
            "sandboxId": "s",
            "generation": 1,
            "template": "t",
            "pack": "p",
            "runId": "r2",
            "pausedAt": 2.0,
            "expiresAt": 1.0,  # 到期早于暂停
        },
    ):
        path.write_text(json.dumps(bad), encoding="utf-8")
        with pytest.raises(LeaseCorruptError):
            read_lease(tmp_path)


def test_lease_expiry_and_expire_now(tmp_path: Path) -> None:
    lease = ConversationLease(
        conversation_run="r1",
        sandbox_id="sbx-1",
        generation=1,
        template="t",
        pack="p",
        run_id="r1",
        paused_at=1000.0,
        expires_at=2000.0,
    )
    write_lease(tmp_path, lease)
    assert not lease.is_expired(now=1999.0)
    assert lease.is_expired(now=2000.0)
    expired = expire_lease_now(tmp_path)
    assert expired is not None and expired.is_expired(time.time())
    assert read_lease(tmp_path) is not None  # fail-closed：记录保留


def test_config_ttl_knob_aligns_with_the_lease_module() -> None:
    """config 的默认值/上限与台账模块的真源一致（防漂移）。"""
    import pi_glink.config as config_module

    assert config_module._DEFAULT_LEASE_TTL_SECONDS == DEFAULT_LEASE_TTL_SECONDS
    assert config_module._MAX_LEASE_TTL_SECONDS == MAX_LEASE_TTL_SECONDS
    assert DEFAULT_LEASE_TTL_SECONDS == 2 * 60 * 60
    assert MAX_LEASE_TTL_SECONDS == 24 * 60 * 60


def test_config_ttl_validation(tmp_path: Path) -> None:
    values = {
        "OPENAI_API_KEY": "sk-test",
        "OPENAI_BASE_URL": "https://example.invalid/v1",
        "OPENAI_MODEL": "test/model",
        "LOCAL_WEB_DATA_ROOT": str(tmp_path / "data"),
    }
    config = build_config(values, REPO_ROOT)
    assert config.conversation_lease_ttl_seconds == DEFAULT_LEASE_TTL_SECONDS
    beyond = dict(values, PI_GLINK_CONVERSATION_LEASE_TTL_SECONDS=str(MAX_LEASE_TTL_SECONDS + 1))
    with pytest.raises(ConfigError, match="LEASE_TTL"):
        build_config(beyond, REPO_ROOT)
    zero = build_config(
        dict(values, PI_GLINK_CONVERSATION_LEASE_TTL_SECONDS="0"), REPO_ROOT
    )
    assert zero.conversation_lease_ttl_seconds == 0


REPO_ROOT = Path(__file__).resolve().parents[1]


def make_config(tmp_path: Path, *, ttl: int = 7200):
    values = {
        "OPENAI_API_KEY": "sk-test",
        "OPENAI_BASE_URL": "https://example.invalid/v1",
        "OPENAI_MODEL": "test/model",
        "LOCAL_WEB_DATA_ROOT": str(tmp_path / "data"),
        "PI_GLINK_CONVERSATION_LEASE_TTL_SECONDS": str(ttl),
    }
    return build_config(values, REPO_ROOT)


# -- 沙箱侧标记 -----------------------------------------------------------------


def test_generation_marker_roundtrip(tmp_path: Path) -> None:
    sandbox = FakeSandbox(tmp_path, sandbox_id="sbx-marker")
    write_generation_marker(sandbox, conversation_run="r1", generation=5)
    assert read_generation_marker(sandbox) == ("r1", 5)
    # 标记在 ACP 状态根之外（回收打包器与回灌都不会携带它）。
    state_root = sandbox.root.joinpath(*SANDBOX_ACP_HOME.lstrip("/").split("/"))
    assert not (state_root / Path(GENERATION_MARKER_PATH).name).exists()


def test_generation_marker_absent_or_corrupt_is_none(tmp_path: Path) -> None:
    sandbox = FakeSandbox(tmp_path, sandbox_id="sbx-empty")
    assert read_generation_marker(sandbox) is None
    sandbox.files.write(GENERATION_MARKER_PATH, b"garbage")
    assert read_generation_marker(sandbox) is None
    sandbox.files.write(GENERATION_MARKER_PATH, json.dumps({"version": 99}))
    assert read_generation_marker(sandbox) is None
    sandbox.files.write(
        GENERATION_MARKER_PATH,
        json.dumps({"version": 1, "conversation": "r1", "generation": "x"}),
    )
    assert read_generation_marker(sandbox) is None


def test_write_marker_failure_raises(tmp_path: Path) -> None:
    sandbox = FakeSandbox(tmp_path, sandbox_id="sbx-broken")

    def refuse(path: str, content: object) -> None:
        raise OSError("disk full")

    sandbox.files.write = refuse  # type: ignore[method-assign]
    with pytest.raises(SandboxError, match="代数标记"):
        write_generation_marker(sandbox, conversation_run="r1", generation=1)


# -- 泊车编排 -------------------------------------------------------------------


def park(
    sandbox: FakeSandbox,
    tmp_path: Path,
    *,
    ttl: int = 7200,
    dests: tuple[str, ...] = (".dsh/.credentials.yaml",),
    run_id: str = "r1",
    sandbox_id: str | None = None,
    now: float | None = None,
):
    conversation = tmp_path / "conversations" / "r1"
    conversation.mkdir(parents=True, exist_ok=True)
    return park_sandbox(
        sandbox,
        config=make_config(tmp_path, ttl=ttl),
        conversation_dir=conversation,
        conversation_run="r1",
        sandbox_id=sandbox_id or sandbox.sandbox_id,
        template="glink-acp-worker",
        pack="dsh",
        run_id=run_id,
        credential_dests=dests,
        kill_by_id=lambda sandbox_id: (FakeSandbox.kill_by_id(sandbox_id), "kill-by-id"),
        now=now,
    )


def test_park_success_writes_marker_lease_and_pauses(tmp_path: Path) -> None:
    sandbox = FakeSandbox(tmp_path, sandbox_id="sbx-park")
    sandbox.files.make_dir(f"{SANDBOX_ACP_HOME}/.dsh")
    sandbox.files.write(f"{SANDBOX_ACP_HOME}/.dsh/.credentials.yaml", b"version: 1\n")
    now = time.time()
    outcome = park(sandbox, tmp_path, now=now)
    assert outcome.ok, outcome.reason
    assert sandbox.state == "paused"
    # pause 不留内存快照：热路径的价值在文件系统，不在进程。
    assert sandbox.pause_calls == [{"keep_memory": False}]
    # 凭据副本不随沙箱持久化。
    assert not sandbox.root.joinpath("home/acp/home/.dsh/.credentials.yaml").exists()
    lease = read_lease(tmp_path / "conversations" / "r1")
    assert lease is not None
    assert lease.generation == 1
    assert lease.sandbox_id == "sbx-park"
    assert lease.run_id == "r1"
    assert lease.paused_at == now
    assert read_generation_marker(sandbox) == ("r1", 1)


def test_park_increments_generation_across_turns(tmp_path: Path) -> None:
    sandbox = FakeSandbox(tmp_path, sandbox_id="sbx-gen")
    assert park(sandbox, tmp_path, run_id="r1").ok
    assert park(sandbox, tmp_path, run_id="r2").ok
    lease = read_lease(tmp_path / "conversations" / "r1")
    assert lease is not None and lease.generation == 2 and lease.run_id == "r2"
    assert read_generation_marker(sandbox) == ("r1", 2)


def test_park_replaced_sandbox_is_destroyed(tmp_path: Path) -> None:
    old = FakeSandbox(tmp_path, sandbox_id="sbx-old")
    assert park(old, tmp_path, run_id="r1").ok
    new = FakeSandbox(tmp_path, sandbox_id="sbx-new")
    outcome = park(new, tmp_path, run_id="r2")
    assert outcome.ok
    assert old.killed
    lease = read_lease(tmp_path / "conversations" / "r1")
    assert lease is not None
    assert lease.sandbox_id == "sbx-new"
    assert lease.retired_sandbox_ids == ()


def test_park_keeps_undestroyable_sandbox_on_the_books(tmp_path: Path) -> None:
    old = FakeSandbox(tmp_path, sandbox_id="sbx-stuck")
    assert park(old, tmp_path, run_id="r1").ok
    new = FakeSandbox(tmp_path, sandbox_id="sbx-next")

    def kill_fails(sandbox_id: str):
        return False, "gateway wedged"

    conversation = tmp_path / "conversations" / "r1"
    outcome = park_sandbox(
        new,
        config=make_config(tmp_path),
        conversation_dir=conversation,
        conversation_run="r1",
        sandbox_id="sbx-next",
        template="glink-acp-worker",
        pack="dsh",
        run_id="r2",
        credential_dests=(),
        kill_by_id=kill_fails,
    )
    assert outcome.ok
    lease = read_lease(conversation)
    assert lease is not None
    # 销毁失败的旧沙箱挂账：对账 sweep 重试，不静默遗忘。
    assert lease.retired_sandbox_ids == ("sbx-stuck",)
    assert not old.killed


def test_park_with_ttl_zero_destroys(tmp_path: Path) -> None:
    sandbox = FakeSandbox(tmp_path, sandbox_id="sbx-ttl0")
    outcome = park(sandbox, tmp_path, ttl=0)
    assert not outcome.ok and outcome.destroyed
    assert sandbox.killed
    assert read_lease(tmp_path / "conversations" / "r1") is None


def test_park_credential_removal_failure_destroys(tmp_path: Path) -> None:
    sandbox = FakeSandbox(tmp_path, sandbox_id="sbx-cred")
    sandbox.files.make_dir(f"{SANDBOX_ACP_HOME}/.dsh")

    def refuse(path: str, **_kwargs: object) -> None:
        raise OSError("transport broke")

    sandbox.files.remove = refuse  # type: ignore[method-assign]
    sandbox.files.write(f"{SANDBOX_ACP_HOME}/.dsh/.credentials.yaml", b"secret")
    outcome = park(sandbox, tmp_path)
    assert not outcome.ok and "凭据" in outcome.reason
    assert sandbox.killed
    assert read_lease(tmp_path / "conversations" / "r1") is None


def test_park_credential_already_gone_is_fine(tmp_path: Path) -> None:
    sandbox = FakeSandbox(tmp_path, sandbox_id="sbx-clean")
    # 目标不存在：SDK 的 not-found 类型视为已删除，不是失败。
    sandbox.files.remove = lambda path, **_k: (_ for _ in ()).throw(
        FakeFileNotFoundException(path)
    )
    outcome = park(sandbox, tmp_path, dests=(".dsh/.credentials.yaml",))
    assert outcome.ok, outcome.reason


def test_park_pause_failure_destroys_and_drops_the_lease(tmp_path: Path) -> None:
    sandbox = FakeSandbox(tmp_path, sandbox_id="sbx-nopause")

    def refuse(**_kwargs: object) -> bool:
        raise OSError("control plane said no")

    sandbox.pause = refuse  # type: ignore[method-assign]
    outcome = park(sandbox, tmp_path)
    assert not outcome.ok and "pause" in outcome.reason
    assert sandbox.killed
    assert read_lease(tmp_path / "conversations" / "r1") is None


def test_park_outcome_payload_carries_the_lease_facts(tmp_path: Path) -> None:
    sandbox = FakeSandbox(tmp_path, sandbox_id="sbx-payload")
    outcome = park(sandbox, tmp_path)
    assert outcome.ok
    payload = outcome.lease.payload() if outcome.lease else {}
    assert payload["parked"] is True
    assert payload["sandbox"] == "sbx-payload"
    assert payload["generation"] == 1
    assert "retiredSandboxes" not in payload  # 空挂账不携带字段


def test_park_lease_write_failure_destroys(tmp_path: Path) -> None:
    sandbox = FakeSandbox(tmp_path, sandbox_id="sbx-readonly")
    conversation = tmp_path / "conversations" / "r1"
    conversation.mkdir(parents=True)
    conversation.chmod(0o500)  # 只读：租约写不进去
    if os.access(conversation, os.W_OK):  # pragma: no cover - root 不受权限约束
        pytest.skip("本环境以 root 运行，权限注入无效")
    outcome = park_sandbox(
        sandbox,
        config=make_config(tmp_path),
        conversation_dir=conversation,
        conversation_run="r1",
        sandbox_id="sbx-readonly",
        template="glink-acp-worker",
        pack="dsh",
        run_id="r1",
        credential_dests=(),
        kill_by_id=lambda _sid: (True, "ok"),
    )
    assert not outcome.ok and "租约写入失败" in outcome.reason
    assert sandbox.killed  # 沙箱销毁，无孤儿


def test_park_destroy_failure_is_reported_honestly(tmp_path: Path) -> None:
    sandbox = FakeSandbox(tmp_path, sandbox_id="sbx-unkillable")

    def refuse_kill() -> bool:
        raise OSError("control plane gone")

    sandbox.kill = refuse_kill  # type: ignore[method-assign]
    outcome = park_sandbox(
        sandbox,
        config=make_config(tmp_path, ttl=0),  # 第一步就销毁的路径
        conversation_dir=tmp_path / "conversations" / "r1",
        conversation_run="r1",
        sandbox_id="sbx-unkillable",
        template="t",
        pack="p",
        run_id="r1",
        credential_dests=(),
    )
    assert not outcome.ok and outcome.destroyed is False


def test_expire_lease_now_without_a_lease_is_none(tmp_path: Path) -> None:
    assert expire_lease_now(tmp_path) is None


def test_marker_reader_refuses_non_text_payloads(tmp_path: Path) -> None:
    sandbox = FakeSandbox(tmp_path, sandbox_id="sbx-weird")
    sandbox.files.read = lambda path, **_k: 42  # type: ignore[method-assign]
    assert read_generation_marker(sandbox) is None


def test_lease_validation_rejects_non_numeric_and_bad_retired(tmp_path: Path) -> None:
    path = tmp_path / LEASE_FILENAME
    base = {
        "version": 1,
        "conversationRun": "r1",
        "sandboxId": "s",
        "template": "t",
        "pack": "p",
        "runId": "r1",
        "pausedAt": 1.0,
        "expiresAt": 2.0,
    }
    path.write_text(json.dumps({**base, "generation": "one"}), encoding="utf-8")
    with pytest.raises(LeaseCorruptError):
        read_lease(tmp_path)
    # 非法形状的 retired 清单拒绝整条记录：遗忘待销毁沙箱就是静默孤儿（B2）。
    path.write_text(
        json.dumps({**base, "generation": 1, "retiredSandboxIds": "oops"}),
        encoding="utf-8",
    )
    with pytest.raises(LeaseCorruptError):
        read_lease(tmp_path)


# -- settle：销毁一个沙箱不得清掉其余挂账（cc 终审 B2） --------------------------


def _a_lease(retired: tuple[str, ...] = ()) -> ConversationLease:
    now = time.time()
    return ConversationLease(
        conversation_run="conv-1",
        sandbox_id="sbx-1",
        generation=1,
        template="glink-acp-worker",
        pack="dsh",
        run_id="conv-1",
        paused_at=now,
        expires_at=now + 3600.0,
        retired_sandbox_ids=retired,
    )


def test_settle_keeps_a_lease_with_pending_retired(tmp_path: Path) -> None:
    write_lease(tmp_path, _a_lease(retired=("sbx-retired-1",)))
    assert settle_lease_after_destroy(tmp_path) is False
    kept = read_lease(tmp_path)
    assert kept is not None
    assert kept.retired_sandbox_ids == ("sbx-retired-1",)
    assert kept.is_expired(time.time() + 1), "立即过期，下轮对账销毁剩余沙箱后清记录"


def test_settle_removes_a_lease_with_nothing_left(tmp_path: Path) -> None:
    write_lease(tmp_path, _a_lease())
    assert settle_lease_after_destroy(tmp_path) is True
    assert read_lease(tmp_path) is None


def test_corrupt_lease_refuses_delete_and_is_kept_by_the_sweep(tmp_path: Path) -> None:
    """损坏租约：对账不销毁不跳过（保留+告警），删除链路拒绝。"""

    path = tmp_path / LEASE_FILENAME
    path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(LeaseCorruptError):
        read_lease(tmp_path)
    # settle 遇损坏也抛（不删文件），由调用方保留：
    with pytest.raises(LeaseCorruptError):
        settle_lease_after_destroy(tmp_path)
    assert path.exists(), "损坏文件原样保留，沙箱名字不丢失"
