"""``select_family`` 的族映射、防御性拒绝与会话来源窄协议注入（R1-1）。

族实例在 ``RunManager.__init__`` 由 ``select_family`` 唯一选出；本文件钉住
映射表（pi/acp 各自的实现类型与 ``name``）、未知 harness 的 fail-closed，以及
``RunManager.__init__`` 的唯一选择点（runner 重接线已落地）。R1-1 起追加钉住：
族构造注入的是 ``SessionSource`` 窄协议（宿主侧 ``RunStoreSessionSource``
适配器）而非具体 ``RunStore``，以及适配器对六个协议面（设计 §2.3）的委托行为。
R1-3 起构造点的 isinstance(RunStore) 过渡兼容退场：注入什么持有什么。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from acp_pack import write_acp_pack
from glink_harness.config import ConfigError, HostConfig, build_config
from glink_harness.kernel.harness import HarnessFamily, select_family
from glink_harness.kernel.harness_acp import AcpHarnessFamily
from glink_harness.kernel.harness_pi import PiHarnessFamily
from glink_harness.kernel.run_views import RunStoreSessionSource
from glink_harness.store import RunRecord, RunSpec, RunStore

REPO_ROOT = Path(__file__).resolve().parents[1]
FAKE_ACP = Path(__file__).resolve().parent / "fixtures" / "fake_acp.py"


def make_config(tmp_path: Path, harness: str) -> HostConfig:
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


def test_select_family_returns_the_pi_family(tmp_path: Path) -> None:
    config = make_config(tmp_path, "pi")
    family = select_family(config.harness, config=config, store=RunStore(tmp_path / "runs"))
    assert type(family) is PiHarnessFamily
    assert isinstance(family, HarnessFamily)
    assert family.name == "pi"


def test_select_family_returns_the_acp_family(tmp_path: Path) -> None:
    config = make_config(tmp_path, "acp")
    family = select_family(config.harness, config=config, store=RunStore(tmp_path / "runs"))
    assert type(family) is AcpHarnessFamily
    assert isinstance(family, HarnessFamily)
    assert family.name == "acp"


def test_select_family_refuses_an_unknown_harness(tmp_path: Path) -> None:
    """config 层已校验取值；这里是 fail-closed 的防御性兜底。"""
    config = make_config(tmp_path, "pi")
    with pytest.raises(ConfigError, match="未知 harness"):
        select_family("bogus", config=config, store=RunStore(tmp_path / "runs"))


def test_run_manager_resolves_the_configured_family_at_init(tmp_path: Path) -> None:
    """``RunManager.__init__`` 按冻结配置解析一次族（启动即 fail-closed）。

    原义务：进程级唯一族实例 ``manager._family``。新义务（S2）：族按 run 自己
    的 ``RunSpec.family`` 取（``_family_for``，按名缓存），准入 spec 来自冻结
    配置；初始化时已解析配置族并缓存同一实例。
    """
    from glink_harness.events import EventBus  # noqa: PLC0415
    from glink_harness.runs.runner import RunManager  # noqa: PLC0415

    for harness, family_type in (("pi", PiHarnessFamily), ("acp", AcpHarnessFamily)):
        config = make_config(tmp_path / harness, harness)
        manager = RunManager(config, EventBus(), RunStore(config.data_root / "runs"))
        family = manager._admission.family_for(config.harness)  # noqa: SLF001
        assert type(family) is family_type
        assert family.name == config.harness
        assert manager._admission.families == {config.harness: family}  # noqa: SLF001
        assert manager._admission.family_for(config.harness) is family  # noqa: SLF001
        assert manager._admission.spec(None).family == config.harness  # noqa: SLF001


# -- R1-1：会话来源窄协议注入点 -----------------------------------------------


def test_select_family_injects_the_session_source_adapter(tmp_path: Path) -> None:
    """族构造拿到的是窄协议适配器，不是具体 RunStore（设计 §2.3）。"""
    config = make_config(tmp_path, "pi")
    family = select_family(config.harness, config=config, store=RunStore(tmp_path / "runs"))
    source = family._store  # noqa: SLF001
    assert type(source) is RunStoreSessionSource
    assert not isinstance(source, RunStore)


def test_family_constructor_holds_the_session_source_as_given(tmp_path: Path) -> None:
    """R1-3：isinstance(RunStore) 过渡兼容退场——构造点不再把具体 RunStore 包成适配器。

    注入什么持有什么：kernel 内唯一接线点 ``select_family`` 统一注入
    ``RunStoreSessionSource``（由 test_select_family_injects_the_session_source_adapter
    钉住），仓外调用点直接构造时须自己给出 SessionSource 协议对象。
    """
    config = make_config(tmp_path, "pi")
    source = RunStoreSessionSource(RunStore(tmp_path / "runs"))
    family = PiHarnessFamily(config=config, store=source)
    assert family._store is source  # noqa: SLF001


def make_source(tmp_path: Path) -> tuple[RunStore, RunStoreSessionSource, RunRecord]:
    """Synthetic store + adapter + one admitted run（临时目录，无真实执行面）。"""
    store = RunStore(tmp_path / "runs")
    record = store.create(
        "第一轮", spec=RunSpec(family="pi", pack="pi", executor="inproc")
    )
    return store, RunStoreSessionSource(store), record


def test_session_source_record_view_and_event_scan(tmp_path: Path) -> None:
    """面 1/2：父记录只读视图与父 run 事件扫描。"""
    store, source, record = make_source(tmp_path)
    assert source.get(record.run_id) == record
    assert source.get("missing") is None
    store.append_event(
        record.run_id, {"type": "kernel_status", "seq": 1, "payload": {"detail": "x"}}
    )
    assert [event["type"] for event in source.iter_events(record.run_id)] == [
        "kernel_status"
    ]


def test_session_source_directory_layout(tmp_path: Path) -> None:
    """面 3：目录布局推导与 store 的回答逐点一致（不泄露 SQLite 布局假设）。"""
    store, source, record = make_source(tmp_path)
    assert source.run_dir(record.run_id) == store.run_dir(record.run_id)
    assert source.session_dir(record.run_id) == store.session_dir(record.run_id)
    assert source.conversations_root() == store.conversations_root()
    assert source.conversation_dir(record.run_id) == store.conversation_dir(record.run_id)


def test_session_source_session_seed_bounded_read(tmp_path: Path) -> None:
    """面 4：父会话种子有界读；嵌套路径是调用方缺陷，fail-closed。"""
    store, source, record = make_source(tmp_path)
    session_dir = store.session_dir(record.run_id)
    session_dir.mkdir(parents=True)
    (session_dir / "parent.jsonl").write_bytes(b'{"k": 1}\n')
    assert (
        source.read_session_seed(record.run_id, "parent.jsonl", max_bytes=1024)
        == b'{"k": 1}\n'
    )
    with pytest.raises(ValueError, match="直系文件"):
        source.read_session_seed(record.run_id, "nested/parent.jsonl", max_bytes=1024)


def test_session_source_child_documents_round_trip(tmp_path: Path) -> None:
    """面 5：子代理文档双向读写。"""
    _store, source, record = make_source(tmp_path)
    source.write_child(record.run_id, {"n": 0, "status": "running"})
    document = source.read_child(record.run_id, 0)
    assert document is not None and document["status"] == "running"
    page = source.read_children(record.run_id)
    assert len(page.documents) == 1
    assert page.skipped == 0
