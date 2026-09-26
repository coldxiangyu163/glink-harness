"""ACP 族策略面的单元测试。

钉住自 runner 搬迁的 ACP 族行为：stopReason 到产品终态词汇的逐格映射
（oracle 即 runner ``_decide`` 现分支字面量）、追问追加门禁的五个拒绝点
（全部 ``session_unavailable``）、能力矩阵的记录字段/legacy 帧双路语义
（含 50 帧扫描上限）、状态根按父记录 executor 推导与 legacy 回退、父工作
区复用与恒为 None 的续接种子。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Mapping

import pytest

from pi_glink.config import HostConfig, build_config
from pi_glink.kernel.harness import FollowupUnavailable, select_family
from pi_glink.kernel.harness_acp import AcpHarnessFamily
from pi_glink.redaction import SecretRedactor
from pi_glink.store import FINISHED, RunStore

REPO_ROOT = Path(__file__).resolve().parents[1]
FAKE_ACP = Path(__file__).resolve().parent / "fixtures" / "fake_acp.py"
SESSION_ID = "fake-acp-session"


def make_config(tmp_path: Path, **overrides: str) -> HostConfig:
    values = {
        "OPENAI_API_KEY": "sk-test",
        "OPENAI_BASE_URL": "https://example.invalid/v1",
        "OPENAI_MODEL": "test/model",
        "PI_CODING_AGENT_DIR": str(REPO_ROOT / "agent"),
        "LOCAL_WEB_DATA_ROOT": str(tmp_path / "data"),
        "PI_GLINK_WEB_SEARCH_CONFIG": str(tmp_path / "absent-web-search.json"),
        "PI_GLINK_HARNESS": "acp",
        "PI_GLINK_ACP_COMMAND": f"{sys.executable} {FAKE_ACP} --config settings.json",
    }
    values.update(overrides)
    return build_config(values, REPO_ROOT)


def make_family(tmp_path: Path, **overrides: str) -> tuple[HostConfig, RunStore, AcpHarnessFamily]:
    config = make_config(tmp_path, **overrides)
    store = RunStore(config.data_root / "runs")
    family = select_family(config.harness, config=config, store=store)
    assert isinstance(family, AcpHarnessFamily)
    return config, store, family


def write_parent(
    store: RunStore,
    run_id: str,
    *,
    harness: str | None = None,
    executor: str | None = None,
    frame_marker: bool = True,
    state_root: str | None = "home",
    session_id: str | None = SESSION_ID,
    capabilities: Mapping[str, Any] | None = None,
    leading_events: int = 0,
) -> None:
    """手写一份 ACP 追问门禁能消费的父记录（同 test_kernel_acp_resume 的先例）。

    手写 meta.json 是为了能构造无 harness/executor 字段的 legacy 记录；
    ``frame_marker=False`` 模拟能力帧缺 ``harness: "acp"`` 标记；
    ``state_root=None`` 模拟状态根缺失（或未回收）的父 run；
    ``leading_events`` 在 session_ready 帧之前垫事件，用于扫描上限断言。

    注意：RunStore 的记录索引每个 root 只建一次，同一 store 里手写多份父
    记录时必须先全部写完再 ``store.get``（见 craft_parent 的注释）。
    """
    directory = store.run_dir(run_id)
    meta: dict[str, Any] = {
        "run_id": run_id,
        "prompt": "第一轮",
        "status": FINISHED,
        "created_at": time.time(),
        "workspace": str(directory / "workspace"),
        "session_id": session_id,
        "finished_at": time.time(),
    }
    if harness is not None:
        meta["harness"] = harness
    if executor is not None:
        meta["executor"] = executor
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    (directory / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    for index in range(leading_events):
        store.append_event(
            run_id, {"type": "kernel_status", "seq": index + 1, "payload": {}}
        )
    payload: dict[str, Any] = {
        "sessionId": session_id,
        "agentCapabilities": (
            capabilities
            if capabilities is not None
            else {"sessionCapabilities": {"resume": {}}}
        ),
    }
    if frame_marker:
        payload["harness"] = "acp"
    store.append_event(
        run_id, {"type": "session_ready", "seq": leading_events + 1, "payload": payload}
    )
    if state_root is not None:
        root = directory / "acp-agent" / state_root
        root.mkdir(parents=True, exist_ok=True)


def craft_parent(store: RunStore, run_id: str, **kwargs: Any) -> Any:
    """write_parent + store.get（单父记录的便捷封装）。

    RunStore 的记录索引每个 root 只在首次读取时建一次：同一 store 需要
    多份手写父记录的测试必须先用 write_parent 全部落盘，再逐个 get。
    """
    write_parent(store, run_id, **kwargs)
    return store.get(run_id)


# -- (e) 终态词汇归一化 ----------------------------------------------------------


@pytest.mark.parametrize(
    ("stop", "expected"),
    [
        (None, ("failed", "execution_error", "ACP session/prompt 没有返回 stopReason")),
        ("bogus", ("failed", "execution_error", "未知的 ACP stopReason: 'bogus'")),
        ("end turn", ("failed", "execution_error", "未知的 ACP stopReason: 'end turn'")),
        ("refusal", ("failed", "model_error", "ACP agent 拒绝继续（stopReason=refusal）")),
        ("cancelled", ("cancelled", "abort", None)),
        ("max_tokens", ("finished", "length", None)),
        ("max_turn_requests", ("finished", "max_turn_requests", None)),
        ("end_turn", None),
    ],
)
def test_acp_model_stop_verdict_table(
    tmp_path: Path, stop: str | None, expected: tuple | None
) -> None:
    """逐格断言：返回值逐字等于 runner ``_decide`` 的 ACP 分支字面量。"""
    _config, _store, family = make_family(tmp_path)
    verdict = family.model_stop_verdict(stop, model_error=None)
    if expected is None:
        assert verdict is None, "end_turn 落入 runner 的交付策略（报告检查）"
    else:
        assert verdict is not None
        assert (verdict.status, verdict.stop_reason, verdict.error) == expected


# -- (b) 追问追加门禁 ------------------------------------------------------------


def test_check_continuation_admits_a_continuable_parent(tmp_path: Path) -> None:
    _config, store, family = make_family(tmp_path)
    parent = craft_parent(store, "parent000001", harness="acp", executor="inproc")
    family.check_continuation(parent)  # 不抛即受理


def test_check_continuation_refuses_a_parent_without_capability_proof(
    tmp_path: Path,
) -> None:
    """拒绝点 1：legacy 记录（无 harness 字段）且帧无标记 → 无法证明族。"""
    _config, store, family = make_family(tmp_path)
    parent = craft_parent(store, "parent000002", frame_marker=False)
    assert parent.harness is None
    with pytest.raises(FollowupUnavailable) as excinfo:
        family.check_continuation(parent)
    assert excinfo.value.code == "session_unavailable"


def test_check_continuation_refuses_a_parent_without_session_id(
    tmp_path: Path,
) -> None:
    """拒绝点 2：有能力矩阵但没有记录的 session id → 没有可 resume 之物。"""
    _config, store, family = make_family(tmp_path)
    parent = craft_parent(store, "parent000003", harness="acp", session_id=None)
    with pytest.raises(FollowupUnavailable) as excinfo:
        family.check_continuation(parent)
    assert excinfo.value.code == "session_unavailable"


def test_check_continuation_refuses_a_parent_without_a_resume_channel(
    tmp_path: Path,
) -> None:
    """拒绝点 3：能力矩阵为空（无续接通道）→ 拒绝，不降格为新会话。"""
    _config, store, family = make_family(tmp_path)
    parent = craft_parent(store, "parent000004", harness="acp", capabilities={})
    with pytest.raises(FollowupUnavailable) as excinfo:
        family.check_continuation(parent)
    assert excinfo.value.code == "session_unavailable"


def test_check_continuation_refuses_a_parent_with_a_missing_state_root(
    tmp_path: Path,
) -> None:
    """拒绝点 4：状态根不存在（e2b 回收失败/未尝试即如此）→ 拒绝。"""
    _config, store, family = make_family(tmp_path)
    parent = craft_parent(
        store, "parent000005", harness="acp", executor="inproc", state_root=None
    )
    with pytest.raises(FollowupUnavailable) as excinfo:
        family.check_continuation(parent)
    assert excinfo.value.code == "session_unavailable"


def test_check_continuation_refuses_a_state_root_that_is_not_a_directory(
    tmp_path: Path,
) -> None:
    """拒绝点 5：状态根路径存在但不是目录 → 拒绝。"""
    _config, store, family = make_family(tmp_path)
    parent = craft_parent(
        store, "parent000006", harness="acp", executor="inproc", state_root=None
    )
    fake = store.run_dir(parent.run_id) / "acp-agent"
    fake.mkdir(parents=True, exist_ok=True)
    (fake / "home").write_text("not a directory", encoding="utf-8")
    with pytest.raises(FollowupUnavailable) as excinfo:
        family.check_continuation(parent)
    assert excinfo.value.code == "session_unavailable"


# -- 能力矩阵双路语义与扫描上限 ----------------------------------------------------


def test_recorded_capabilities_prefers_the_record_field(tmp_path: Path) -> None:
    """记录字段证明族时，帧无需自带标记也是合法的能力来源。"""
    _config, store, family = make_family(tmp_path)
    parent = craft_parent(
        store, "parent000007", harness="acp", executor="inproc", frame_marker=False
    )
    capabilities = family.recorded_capabilities(parent)
    assert capabilities == {"sessionCapabilities": {"resume": {}}}


def test_recorded_capabilities_legacy_path_needs_the_frame_marker(
    tmp_path: Path,
) -> None:
    """legacy 记录：帧带标记则矩阵成立；无标记则族未证明，返回 None。"""
    _config, store, family = make_family(tmp_path)
    write_parent(store, "parent000008", frame_marker=True)
    write_parent(store, "parent000009", frame_marker=False)
    marked = store.get("parent000008")
    unmarked = store.get("parent000009")
    assert family.recorded_capabilities(marked) == {
        "sessionCapabilities": {"resume": {}}
    }
    assert family.recorded_capabilities(unmarked) is None


def test_recorded_capabilities_scan_is_bounded_at_fifty_frames(tmp_path: Path) -> None:
    """session_ready 紧随 run 打开即提交；扫描上限 50 帧，超出即不证明。"""
    _config, store, family = make_family(tmp_path)
    write_parent(store, "parent000010", harness="acp", frame_marker=False, leading_events=50)
    write_parent(store, "parent000011", harness="acp", frame_marker=False, leading_events=49)
    beyond = store.get("parent000010")
    within = store.get("parent000011")
    assert family.recorded_capabilities(beyond) is None
    assert family.recorded_capabilities(within) == {
        "sessionCapabilities": {"resume": {}}
    }


# -- 状态根推导 ------------------------------------------------------------------


def test_state_home_follows_the_parent_recorded_executor(tmp_path: Path) -> None:
    config, store, family = make_family(tmp_path)
    assert config.executor == "inproc"
    write_parent(store, "parent000012", harness="acp", executor="e2b", state_root="recovered")
    write_parent(store, "parent000013", harness="acp", executor="inproc")
    e2b_parent = store.get("parent000012")
    inproc_parent = store.get("parent000013")
    assert family.state_home(e2b_parent) == (
        store.run_dir(e2b_parent.run_id) / "acp-agent" / "recovered"
    )
    assert family.state_home(inproc_parent) == (
        store.run_dir(inproc_parent.run_id) / "acp-agent" / "home"
    )


def test_state_home_legacy_parent_falls_back_to_the_current_executor(
    tmp_path: Path,
) -> None:
    """无 executor 字段的旧记录回退为按当前配置推导（原行为，不回填）。"""
    _config, store, family = make_family(tmp_path, LOCAL_WEB_EXECUTOR="e2b")
    parent = craft_parent(store, "parent000014", state_root="recovered")
    assert parent.executor is None
    assert family.state_home(parent) == (
        store.run_dir(parent.run_id) / "acp-agent" / "recovered"
    )


# -- 续接种子与工作区 --------------------------------------------------------------


def test_continuation_workspace_reuses_the_parent_workspace(tmp_path: Path) -> None:
    """ACP 追问复用父 run 的工作区（会话 cwd 记在 agent 自己的状态里）。"""
    _config, store, family = make_family(tmp_path)
    parent = craft_parent(store, "parent000015", harness="acp", executor="inproc")
    assert family.continuation_workspace(parent) == Path(parent.workspace)


def test_resume_seed_is_always_none(tmp_path: Path) -> None:
    """ACP 续接住在父状态根，宿主没有会话文件种子可读。"""
    _config, store, family = make_family(tmp_path)
    parent = craft_parent(store, "parent000016", harness="acp", executor="inproc")
    assert family.resume_seed(parent, redactor=SecretRedactor()) is None


def test_resume_value_is_the_parent_session_id(tmp_path: Path) -> None:
    _config, store, family = make_family(tmp_path)
    parent = craft_parent(store, "parent000017", harness="acp", executor="inproc")
    assert family.resume_value(parent) == SESSION_ID
    assert family.resume_value(None) is None


def test_acp_always_verifies_continuation_before_turn(tmp_path: Path) -> None:
    """ACP 各面在 session/resume 之后即知 session id，回合前即可校验。"""
    _config, _store, family = make_family(tmp_path)
    for executor in ("inproc", "docker", "e2b"):
        assert family.verifies_continuation_before_turn(executor) is True
