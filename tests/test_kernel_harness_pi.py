"""pi 族策略面的单元测试。

钉住自 runner 搬迁的 pi 族行为：缺模型凭据的准入拒绝
原文、续接种子值与会话身份校验时机，以及 pi stop 词汇到产品终态词汇的
逐格映射（oracle 即 runner ``_decide`` 现分支字面量）。R1-2 起追加钉住：
族只经 SessionSource 窄协议访问宿主事实——共享词汇镜像与 store 逐字相等、
``resume_seed`` 经协议有界读且重脱敏留族侧（各失败点拒绝文本与次序原样）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from acp_pack import write_acp_pack
from glink_harness.config import ConfigError, HostConfig, build_config
from glink_harness.contracts import FollowupUnavailable
from glink_harness.kernel import harness_pi
from glink_harness.kernel.agent_env import RunCredentials
from glink_harness.kernel.harness import select_family
from glink_harness.redaction import SecretRedactor
from glink_harness.store import (
    CANCELLED,
    CHILDREN_DIRNAME,
    FAILED,
    FINISHED,
    SUBAGENTS_DIRNAME,
    RunStore,
    RunSpec,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
FAKE_ACP = Path(__file__).resolve().parent / "fixtures" / "fake_acp.py"


def make_config(tmp_path: Path, harness: str = "pi") -> HostConfig:
    values = {
        "OPENAI_API_KEY": "sk-test",
        "OPENAI_BASE_URL": "https://example.invalid/v1",
        "OPENAI_MODEL": "test/model",
        "PI_CODING_AGENT_DIR": str(REPO_ROOT / "harnesses" / "pi"),
        "LOCAL_WEB_DATA_ROOT": str(tmp_path / "data"),
        "GLINK_HARNESS": harness,
    }
    if harness == "acp":
        packs = write_acp_pack(
            tmp_path, f"{sys.executable} {FAKE_ACP} --config settings.json"
        )
        values["GLINK_HARNESS"] = "dsh"
        values["GLINK_HARNESSES_DIR"] = str(packs)
    return build_config(values, REPO_ROOT)


def make_family(tmp_path: Path, harness: str = "pi"):
    config = make_config(tmp_path, harness)
    store = RunStore(config.data_root / "runs")
    return config, store, select_family(config.harness, config=config, store=store)


# -- (a) 准入 -----------------------------------------------------------------


def test_pi_validate_credentials_refuses_a_missing_model_key(tmp_path: Path) -> None:
    _config, _store, family = make_family(tmp_path)
    with pytest.raises(ConfigError) as excinfo:
        family.validate_credentials(RunCredentials(model_api_key=""))
    assert str(excinfo.value) == (
        "OPENAI_API_KEY is not configured; set it in .env before starting a run"
    )


def test_pi_validate_credentials_accepts_a_model_key(tmp_path: Path) -> None:
    _config, _store, family = make_family(tmp_path)
    family.validate_credentials(RunCredentials(model_api_key="sk-test"))


def test_acp_validate_credentials_is_a_noop_for_single_user(tmp_path: Path) -> None:
    """单用户下 ACP agent 自带模型配置：无宿主 key 也通过准入。

    多租户下的政策分流（tenant 拒绝 / shared no-op）见
    tests/test_acp_credential_policy.py（ADR 0015）。
    """
    _config, _store, family = make_family(tmp_path, "acp")
    family.validate_credentials(RunCredentials(model_api_key=""))


# -- (b) 续接种子与校验时机 ------------------------------------------------------


def test_pi_resume_value(tmp_path: Path) -> None:
    _config, store, family = make_family(tmp_path)
    parent = store.create("第一轮", spec=RunSpec(family="pi", pack="pi", executor="inproc"))
    assert family.resume_value(parent) == "continued.jsonl"
    assert family.resume_value(None) is None


def test_pi_verifies_continuation_before_turn_per_executor(tmp_path: Path) -> None:
    """三面同规则（恒 True）：e2b 面走 RPC 通道后 get_state 在回合前即知会话身份。

    原义务（沙箱面流中才发现 session id，只能回合后校验）随旧 ``--mode
    json`` 适配器删除。
    """
    _config, _store, family = make_family(tmp_path)
    # 与 sandbox 无关（S2 起无 executor 参数）。
    assert family.verifies_continuation_before_turn() is True


def test_continuation_workspace_defaults_to_none(tmp_path: Path) -> None:
    """pi 每 run 独立工作区（基类默认）；ACP 覆写见 test_kernel_harness_acp。"""
    _config, store, family = make_family(tmp_path)
    parent = store.create("第一轮", spec=RunSpec(family="pi", pack="pi", executor="inproc"))
    assert family.continuation_workspace(parent) is None


# -- R1-2：族经 SessionSource 窄协议访问宿主事实 ----------------------------------


def test_pi_family_vocabulary_mirrors_the_store_vocabulary() -> None:
    """族不再 import store；与宿主共享的词汇（终态词与 run 目录子布局）逐字相等在此钉住。

    唯一真源是 ``store.py``；harness_pi.py 的同名常量只是镜像，漂移即红。
    """
    assert harness_pi.FINISHED == FINISHED
    assert harness_pi.FAILED == FAILED
    assert harness_pi.SUBAGENTS_DIRNAME == SUBAGENTS_DIRNAME
    assert harness_pi.CHILDREN_DIRNAME == CHILDREN_DIRNAME


def make_parent_with_session(store: RunStore, content: bytes = b'{"k": 1}\n'):
    """一条带会话文件事实的父 run（合成输入，临时目录）。"""
    parent = store.create("第一轮", spec=RunSpec(family="pi", pack="pi", executor="inproc"))
    session_dir = store.session_dir(parent.run_id)
    session_dir.mkdir(parents=True)
    session_file = session_dir / "session.jsonl"
    session_file.write_bytes(content)
    return store.update(
        parent.run_id, session_file=str(session_file), session_id="pi-session-1"
    )


def test_pi_resume_seed_reads_via_the_session_source_and_re_redacts(tmp_path: Path) -> None:
    """种子经协议有界读取出；重脱敏职责留族侧（历史文件按最新规则再脱敏）。"""
    _config, store, family = make_family(tmp_path)
    parent = make_parent_with_session(store, b'{"key": "sk-parent-secret-1"}\n')
    seed = family.resume_seed(parent, redactor=SecretRedactor(["sk-parent-secret-1"]))
    assert seed is not None
    assert b"sk-parent-secret-1" not in seed
    assert seed != b'{"key": "sk-parent-secret-1"}\n'


@pytest.mark.parametrize(
    "content",
    [b"", b"\n"],
)
def test_pi_resume_seed_refuses_an_empty_seed(tmp_path: Path, content: bytes) -> None:
    _config, store, family = make_family(tmp_path)
    parent = make_parent_with_session(store, content)
    with pytest.raises(FollowupUnavailable, match="session_unavailable"):
        family.resume_seed(parent, redactor=SecretRedactor())


def test_pi_resume_seed_refuses_a_path_outside_the_session_dir(tmp_path: Path) -> None:
    """记录的路径不是能力：必须是本 run 会话目录的直系 .jsonl 文件。"""
    _config, store, family = make_family(tmp_path)
    parent = store.create("第一轮", spec=RunSpec(family="pi", pack="pi", executor="inproc"))
    outside = tmp_path / "elsewhere.jsonl"
    outside.write_bytes(b'{"k": 1}\n')
    parent = store.update(
        parent.run_id, session_file=str(outside), session_id="pi-session-1"
    )
    with pytest.raises(FollowupUnavailable, match="session_unavailable"):
        family.resume_seed(parent, redactor=SecretRedactor())
    session_dir = store.session_dir(parent.run_id)
    session_dir.mkdir(parents=True)
    wrong_suffix = session_dir / "session.txt"
    wrong_suffix.write_bytes(b'{"k": 1}\n')
    parent = store.update(parent.run_id, session_file=str(wrong_suffix))
    with pytest.raises(FollowupUnavailable, match="session_unavailable"):
        family.resume_seed(parent, redactor=SecretRedactor())


def test_pi_resume_seed_refuses_a_missing_or_undecodable_file(tmp_path: Path) -> None:
    _config, store, family = make_family(tmp_path)
    parent = store.create("第一轮", spec=RunSpec(family="pi", pack="pi", executor="inproc"))
    session_dir = store.session_dir(parent.run_id)
    session_dir.mkdir(parents=True)
    parent = store.update(
        parent.run_id,
        session_file=str(session_dir / "missing.jsonl"),
        session_id="pi-session-1",
    )
    with pytest.raises(FollowupUnavailable, match="session_unavailable"):
        family.resume_seed(parent, redactor=SecretRedactor())
    parent = make_parent_with_session(store, b"\xff\xfe invalid utf-8\n")
    with pytest.raises(FollowupUnavailable, match="session_unavailable"):
        family.resume_seed(parent, redactor=SecretRedactor())


def test_pi_resume_seed_refuses_a_parent_without_session_id(tmp_path: Path) -> None:
    _config, store, family = make_family(tmp_path)
    parent = make_parent_with_session(store)
    parent = store.update(parent.run_id, session_id=None)
    with pytest.raises(FollowupUnavailable, match="session_unavailable"):
        family.resume_seed(parent, redactor=SecretRedactor())


# -- (e) 终态词汇归一化 ----------------------------------------------------------


@pytest.mark.parametrize(
    ("stop", "model_error", "expected"),
    [
        ("error", "模型炸了", (FAILED, "model_error", "模型炸了")),
        ("error", None, (FAILED, "model_error", "模型返回了错误结果")),
        ("aborted", "被截断", (FAILED, "aborted", "被截断")),
        ("aborted", None, (FAILED, "aborted", "模型输出被中断")),
        ("length", None, (FINISHED, "length", None)),
        ("length", "无关错误", (FINISHED, "length", None)),
        (None, None, None),
        ("stop", None, None),
        ("end_turn", None, None),
        ("未知的词", None, None),
    ],
)
def test_pi_model_stop_verdict_table(
    tmp_path: Path, stop: str | None, model_error: str | None, expected: tuple | None
) -> None:
    """逐格断言：返回值逐字等于 runner ``_decide`` 的 pi 分支字面量。"""
    _config, _store, family = make_family(tmp_path)
    verdict = family.model_stop_verdict(stop, model_error=model_error)
    if expected is None:
        assert verdict is None, "None 表示落入 runner 的交付策略（报告检查）"
    else:
        assert verdict is not None
        assert (verdict.status, verdict.stop_reason, verdict.error) == expected


def test_pi_verdict_never_uses_acp_vocabulary(tmp_path: Path) -> None:
    """pi 族不认识 ACP 的 stopReason 词汇：refusal/cancelled 落到交付策略。"""
    _config, _store, family = make_family(tmp_path)
    assert family.model_stop_verdict("refusal", model_error=None) is None
    assert family.model_stop_verdict("cancelled", model_error=None) is None
    assert family.model_stop_verdict("max_turn_requests", model_error=None) is None
    # CANCELLED 等词汇只由 runner `_decide` 的共同前缀产出，不由族方法产出。
    verdict = family.model_stop_verdict("max_tokens", model_error=None)
    assert verdict is None or verdict.status != CANCELLED

