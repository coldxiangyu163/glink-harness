"""收尾扫描契约的回归（GLINK-OBS-01 第 1 项）。

原缺陷：runner 收尾的会话扫描（现为 ``SealStage.sweep`` → ``RunSecretBoundary.seal_sessions``） 无条件访问 ``runtime.session_dir``，ACP
运行时没有该属性，于是每次 ACP run 收尾都抛 ``AttributeError`` 并被外层
``except`` 吞掉（``session credential sweep failed``）——整个会话面脱敏被跳过。

修复后「哪些路径需要扫描、能否重写、哪些注入凭据副本要删除」是运行时自己的
声明（``session_sweeps``），runner 只消费契约。这里钉住四件事：

* ACP 状态根声明为只读扫描：凭据形状值并入本 run 的脱敏边界，但 agent 自己
  的会话存储一个字节都不改写（追问要续接的就是这些文件）；
* 注入凭据副本（``credential_copies``）在扫描**之后**从两个状态根逐个删除，
  且只删已知相对路径（链接/越界不跟随也不删）；
* pi 面的声明与行为不变（会话目录允许重写，主会话文件来自 run 记录）；
* 整个会话扫描路径（``SealStage.sweep``）在 ACP 运行时上不再抛错；
* pi 运行期配置目录里宿主渲染的 ``auth.json`` / ``web-search.json`` 收尾删除
  （决定 C 的 pi 面延伸），其余配置文件不动、不被扫描。
"""

from __future__ import annotations

import json
from pathlib import Path

from glink_harness.config import build_config
from glink_harness.events import EventBus
from glink_harness.kernel.acp_env import ACP_HOME_DIRNAME, ACP_RECOVERED_DIRNAME, AcpRuntime
from glink_harness.kernel.agent_env import AgentRuntime
from glink_harness.redaction import REPLACEMENT, SecretRedactor
from glink_harness.runs.runner import RunManager
from glink_harness.runs.secrets import RunSecretBoundary
from glink_harness.runs.state import RunExecution as _Execution
from glink_harness.store import RunRecord, RunStore, RunSpec

REPO_ROOT = Path(__file__).resolve().parents[1]

#: 合成值：``sk-`` 形状，避免任何真实凭据进入测试。
YAML_SECRET = "sk-live-yaml-0123456789abcdef"
JSON_SECRET = "sk-live-json-0123456789abcdef"


def make_config(tmp_path: Path, **overrides: str):
    values = {
        "OPENAI_API_KEY": "sk-test-host-boundary-0123456789",
        "OPENAI_BASE_URL": "https://example.invalid/v1",
        "OPENAI_MODEL": "test/model",
        "PI_CODING_AGENT_DIR": str(REPO_ROOT / "harnesses" / "pi"),
        "GLINK_HARNESS": "pi",
        "LOCAL_WEB_DATA_ROOT": str(tmp_path / "data"),
        "LOCAL_WEB_PORT": "0",
    }
    values.update(overrides)
    return build_config(values, REPO_ROOT)


def build_context(tmp_path: Path, run_id: str) -> tuple[RunManager, RunStore, RunRecord, Path]:
    config = make_config(tmp_path)
    store = RunStore(config.data_root / "runs")
    manager = RunManager(config, EventBus(), store)
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    record = store.create("收尾扫描", run_id=run_id, workspace=workspace, spec=RunSpec(family="pi", pack="pi", executor="inproc"))
    return manager, store, record, workspace


def sweep(manager: RunManager, execution: _Execution) -> tuple[bool, list[str]]:
    """Seal 阶段的扫描入口本身：绑定运行时声明后走 ``SealStage.sweep``。

    生产里运行时在 Prepare 阶段绑定到边界；这里直接构造的 execution 手动绑定。
    """
    secrets = manager._pipeline.secrets(execution)  # noqa: SLF001 - the sweep entry itself
    secrets.bind_runtime(execution.runtime)
    failures: list[str] = []
    changed = manager._pipeline.seal.sweep(execution, failures)  # noqa: SLF001
    return changed, failures


def test_acp_runtime_declares_read_only_state_roots(tmp_path: Path) -> None:
    """ACP 的两个状态根都声明为「扫描值、不重写」，并带上注入凭据副本。"""
    directory = tmp_path / "data" / "runs" / "r1" / "acp-agent"
    runtime = AcpRuntime(
        directory=directory,
        home=directory / ACP_HOME_DIRNAME,
        command="dsh",
        credential_dests=(".dsh/.credentials.yaml",),
    )

    sweeps = runtime.session_sweeps()

    assert [
        (sweep.root, sweep.rewrite, sweep.carries_session_file, sweep.credential_copies)
        for sweep in sweeps
    ] == [
        (directory / ACP_HOME_DIRNAME, False, False, (".dsh/.credentials.yaml",)),
        (directory / ACP_RECOVERED_DIRNAME, False, False, (".dsh/.credentials.yaml",)),
    ]


def test_acp_session_sweep_learns_credentials_without_rewriting_state(tmp_path: Path) -> None:
    """原失败机制的直接回归：不抛错；会话存储不被改写；已知凭据副本被删除。"""
    manager, store, record, workspace = build_context(tmp_path, "acp-sweep")
    directory = tmp_path / "data" / "runs" / "acp-sweep" / "acp-agent"
    home = directory / ACP_HOME_DIRNAME
    credentials = home / ".dsh" / ".credentials.yaml"
    credentials.parent.mkdir(parents=True)
    credentials.write_text(f"apiKey: {YAML_SECRET}\n", encoding="utf-8")
    session = home / ".dsh" / "sessions" / "session.v3.jsonl"
    session.parent.mkdir(parents=True)
    session.write_text(
        json.dumps({"type": "message", "apiKey": JSON_SECRET}) + "\n", encoding="utf-8"
    )
    # 二进制会话存储（zstd）也必须原样保留：不能因为含 \x00 以外的原因被改写。
    session_bytes = session.read_bytes()
    runtime = AcpRuntime(
        directory=directory,
        home=home,
        command="dsh",
        credential_dests=(".dsh/.credentials.yaml",),
    )
    execution = _Execution(record=record, workspace=workspace)
    execution.runtime = runtime
    # 非空边界（生产里总是含 host 秘密与本 run 凭据）：避开 "空则回退 host" 分支，
    # 使断言直接证明值是本次扫描学到的。
    execution.secrets = RunSecretBoundary(SecretRedactor(["bootstrap-secret-0123456789"]))
    boundary = execution.secrets

    changed, _failures = sweep(manager, execution)

    assert changed is True, "扫描出的凭据形状值必须并入本 run 的边界"
    assert YAML_SECRET in boundary.known_values
    assert JSON_SECRET in boundary.known_values, "副本必须在删除之前被读到"
    # 已知相对路径的注入副本收尾删除。
    assert not credentials.exists(), "注入凭据副本必须在收尾时删除"
    # agent 自己的会话存储一个字节都不动（续接依赖它）。
    assert session.read_bytes() == session_bytes
    assert store.get(record.run_id) is not None


def test_acp_session_sweep_deletes_copies_from_both_state_roots(tmp_path: Path) -> None:
    """home 与 E2B 回收的 recovered 两处副本都删，非凭据文件保留。"""
    manager, _store, record, workspace = build_context(tmp_path, "acp-both")
    directory = tmp_path / "data" / "runs" / "acp-both" / "acp-agent"
    home = directory / ACP_HOME_DIRNAME
    recovered = directory / ACP_RECOVERED_DIRNAME
    for root, marker in ((home, "home"), (recovered, "recovered")):
        (root / ".dsh").mkdir(parents=True)
        (root / ".dsh" / ".credentials.yaml").write_text(
            f"apiKey: {YAML_SECRET}\n", encoding="utf-8"
        )
        (root / ".dsh" / "settings.yaml").write_text(f"profile: {marker}\n", encoding="utf-8")
        (root / ".dsh" / "sessions").mkdir()
        (root / ".dsh" / "sessions" / "session.v3.jsonl").write_text(
            "{\"type\": \"message\"}\n", encoding="utf-8"
        )
    runtime = AcpRuntime(
        directory=directory,
        home=home,
        command="dsh",
        credential_dests=(".dsh/.credentials.yaml",),
    )
    execution = _Execution(record=record, workspace=workspace)
    execution.runtime = runtime
    execution.secrets = RunSecretBoundary(SecretRedactor(["bootstrap-secret-0123456789"]))

    _changed, failures = sweep(manager, execution)

    for root in (home, recovered):
        assert not (root / ".dsh" / ".credentials.yaml").exists()
        assert (root / ".dsh" / "settings.yaml").is_file(), "非凭据状态文件必须保留"
        assert (root / ".dsh" / "sessions" / "session.v3.jsonl").is_file(), "会话文件必须保留"
    assert failures == []


def test_acp_session_sweep_never_follows_a_linked_or_escaping_dest(tmp_path: Path) -> None:
    """链接 dest 与越界 dest 不被跟随：外部目标保留，失败记入收尾事实。"""
    manager, _store, record, workspace = build_context(tmp_path, "acp-hostile")
    directory = tmp_path / "data" / "runs" / "acp-hostile" / "acp-agent"
    home = directory / ACP_HOME_DIRNAME
    (home / ".dsh").mkdir(parents=True)
    outside = tmp_path / "outside-credentials.yaml"
    outside.write_text(f"apiKey: {YAML_SECRET}\n", encoding="utf-8")
    (home / ".dsh" / ".credentials.yaml").symlink_to(outside)
    runtime = AcpRuntime(
        directory=directory,
        home=home,
        command="dsh",
        credential_dests=(".dsh/.credentials.yaml", "../../outside-credentials.yaml"),
    )
    execution = _Execution(record=record, workspace=workspace)
    execution.runtime = runtime
    execution.secrets = RunSecretBoundary(SecretRedactor(["bootstrap-secret-0123456789"]))

    _changed, failures = sweep(manager, execution)

    assert outside.read_text(encoding="utf-8") == f"apiKey: {YAML_SECRET}\n", "不跟随链接"
    assert (home / ".dsh" / ".credentials.yaml").is_symlink(), "链接本身也不删（拒绝处理）"
    details = "\n".join(failures)
    assert ".dsh/.credentials.yaml" in details
    assert "outside-credentials.yaml" in details


def test_acp_session_sweep_ignores_a_missing_recovered_root(tmp_path: Path) -> None:
    """inproc/Docker 面没有 ``recovered``：缺失的根按「无可扫之物」处理。"""
    manager, _store, record, workspace = build_context(tmp_path, "acp-norecovered")
    directory = tmp_path / "data" / "runs" / "acp-norecovered" / "acp-agent"
    home = directory / ACP_HOME_DIRNAME
    home.mkdir(parents=True)
    execution = _Execution(record=record, workspace=workspace)
    execution.runtime = AcpRuntime(directory=directory, home=home, command="dsh")
    execution.secrets = RunSecretBoundary(SecretRedactor(["bootstrap-secret-0123456789"]))

    assert sweep(manager, execution)[0] is False


def test_pi_runtime_sweep_declaration_keeps_rewriting_sessions(tmp_path: Path) -> None:
    """pi 面行为不变：会话目录仍允许重写。"""
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    session = session_dir / "session.jsonl"
    session.write_text(
        json.dumps({"type": "message", "text": f"OPENAI_API_KEY={YAML_SECRET}"}) + "\n",
        encoding="utf-8",
    )
    runtime = AgentRuntime(
        directory=tmp_path / "pi-agent",
        provider_id="frontier",
        model_id="test/model",
        models_path=tmp_path / "pi-agent" / "models.json",
        session_dir=session_dir,
    )

    sweeps = runtime.session_sweeps()

    # 原义务（GLINK-OBS-01）：另声明运行期配置目录，收尾删除渲染的凭据文件。
    # 新义务（ADR 0008）：渲染文件只含已吊销的 egress token，pi 只声明会话目录。
    assert [
        (sweep.root, sweep.rewrite, sweep.carries_session_file, sweep.credential_copies)
        for sweep in sweeps
    ] == [(session_dir, True, True, ())]

    manager, store, record, workspace = build_context(tmp_path, "pi-sweep")
    store.update(record.run_id, session_file=str(session))
    execution = _Execution(record=store.get(record.run_id), workspace=workspace)
    execution.runtime = runtime
    execution.secrets = RunSecretBoundary(SecretRedactor(["bootstrap-secret-0123456789"]))

    assert sweep(manager, execution)[0] is True
    text = session.read_text(encoding="utf-8")
    assert YAML_SECRET not in text
    assert REPLACEMENT in text
    assert json.loads(text.splitlines()[0])["text"].endswith(REPLACEMENT)
