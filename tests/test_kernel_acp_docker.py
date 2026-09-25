"""ACP family on the Docker execution face (GLINK-ACP-03 / M3a).

Two obligations are pinned here, both without a Docker daemon:

* **Plan composition** — ``docker run -i`` carries the ACP JSON-RPC transport on
  the container's stdio, mounts the run's own ``HOME`` (agent state root) with
  the injected credentials read-only inside it, reuses the shared container
  identity helpers, and keeps the pi face's resource ceilings and privilege
  drop. The image and the command come from the frozen configuration.
* **Image pin boundary** — ``docker/Dockerfile.acp-worker`` declares exact
  versions that must equal every version the integration notes record as
  measured (the notes are the repository's record of what was actually
  verified); a floating or missing declaration fails the test, not a warning.

A real container run is covered separately by the task's live evidence.
"""

from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path

import pytest

from pi_glink.config import ConfigError, build_config
from pi_glink.events import EventBus
from pi_glink.kernel import docker_env
from pi_glink.kernel.acp_env import prepare_acp_runtime
from pi_glink.kernel.execution import ExecutionSpec, build_adapter
from pi_glink.kernel.rpc import PiRpcClient
from pi_glink.kernel.runner import RunManager
from pi_glink.store import FAILED, FINISHED, RunStore

REPO_ROOT = Path(__file__).resolve().parents[1]
NOTES = REPO_ROOT / "docs" / "acp-integration-notes.md"
ACP_DOCKERFILE = REPO_ROOT / "docker" / "Dockerfile.acp-worker"
PI_DOCKERFILE = REPO_ROOT / "docker" / "Dockerfile.worker"


def make_config(tmp_path: Path, **overrides: str):
    values = {
        "OPENAI_API_KEY": "sk-test",
        "OPENAI_BASE_URL": "https://example.invalid/v1",
        "OPENAI_MODEL": "test/model",
        "PI_CODING_AGENT_DIR": str(REPO_ROOT / "agent"),
        # 测试绝不触碰操作员的真实数据根（~/.pi-glink）。
        "LOCAL_WEB_DATA_ROOT": str(tmp_path / "data"),
        "PI_GLINK_WEB_SEARCH_CONFIG": str(tmp_path / "absent-web-search.json"),
        "PI_GLINK_HARNESS": "acp",
        "PI_GLINK_ACP_COMMAND": "dsh --profile acp",
    }
    values.update(overrides)
    return build_config(values, REPO_ROOT)


def make_runtime(config, tmp_path: Path, name: str = "r1"):
    return prepare_acp_runtime(config, runtime_dir=tmp_path / name / "acp-agent")


def make_plan(config, runtime, tmp_path: Path, *, run_id: str = "abc123"):
    return docker_env.build_acp_plan(
        config,
        runtime,
        run_id=run_id,
        workspace=tmp_path / "workspace",
    )


# -- plan composition ---------------------------------------------------------


def test_the_plan_bridges_container_stdio_and_mounts_the_state_root(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path, LOCAL_WEB_EXECUTOR="docker")
    runtime = make_runtime(config, tmp_path)
    workspace = tmp_path / "workspace"
    plan = make_plan(config, runtime, tmp_path)

    argv = plan.argv
    assert argv[:4] == ["docker", "run", "--rm", "-i"], "stdio bridge needs -i"
    assert argv[argv.index("--name") + 1] == plan.container_name
    assert plan.container_name == docker_env.container_name_for("abc123")
    # The agent-visible workspace is the mount point, and the run's own HOME is
    # mounted as the agent state root.
    assert f"{workspace}:{docker_env.CONTAINER_WORKSPACE}" in argv
    assert argv[argv.index("-w") + 1] == docker_env.CONTAINER_WORKSPACE
    assert f"{runtime.home}:{docker_env.CONTAINER_HOME}" in argv
    assert f"HOME={docker_env.CONTAINER_HOME}" in argv
    # The frozen command runs inside the image, not the host.
    tail = argv[argv.index(plan.image) :]
    assert tail[1:] == ["dsh", "--profile", "acp"]
    assert plan.image == config.acp_worker_image
    assert docker_env.acp_worker_image(config) == config.acp_worker_image


def test_credentials_are_mounted_read_only_inside_the_writable_home(
    tmp_path: Path,
) -> None:
    source = tmp_path / "operator.json"
    source.write_text('{"tokens": {"access_token": "opaque-credential-123456"}}', encoding="utf-8")
    config = make_config(
        tmp_path,
        LOCAL_WEB_EXECUTOR="docker",
        PI_GLINK_ACP_CREDENTIAL_FILES=f"{source}=.codex/auth.json",
    )
    runtime = make_runtime(config, tmp_path)
    plan = make_plan(config, runtime, tmp_path)

    expected = f"{runtime.home / '.codex' / 'auth.json'}:{docker_env.CONTAINER_HOME}/.codex/auth.json:ro"
    assert expected in plan.argv, "凭据文件必须在可写 HOME 之上以只读挂载"
    assert runtime.credential_dests == (".codex/auth.json",)
    # The writable state root itself stays writable: agents must keep sessions.
    assert f"{runtime.home}:{docker_env.CONTAINER_HOME}" in plan.argv
    assert not any(
        item.startswith(f"{runtime.home}:{docker_env.CONTAINER_HOME}:")
        for item in plan.argv
    ), "HOME 挂载本身不能是只读的"


def test_no_credential_value_or_unmapped_host_path_leaks_into_the_plan(
    tmp_path: Path,
) -> None:
    source = tmp_path / "operator" / ".credentials.yaml"
    source.parent.mkdir()
    source.write_text("DEEPSEEK_API_KEY: sk-opaque-credential-1234567890\n", encoding="utf-8")
    config = make_config(
        tmp_path,
        LOCAL_WEB_EXECUTOR="docker",
        PI_GLINK_ACP_CREDENTIAL_FILES=f"{source}=.dsh/.credentials.yaml",
        PI_GLINK_ACP_ENV="ACME_MODE=workspace",
    )
    runtime = make_runtime(config, tmp_path)
    plan = make_plan(config, runtime, tmp_path)
    joined = " ".join(plan.argv)

    assert "sk-opaque-credential-1234567890" not in joined, "凭据值不得进入 argv"
    assert "ACME_MODE=workspace" not in joined, "env 值不得进入 argv（按名字传）"
    # Every host path on the command line must be one of the declared mounts.
    host_paths = [item for item in plan.argv if item.startswith(str(tmp_path))]
    assert host_paths, "至少工作区与 HOME 是宿主挂载"
    for item in host_paths:
        assert f":{docker_env.CONTAINER_WORKSPACE}" in item or f":{docker_env.CONTAINER_HOME}" in item


def test_acp_env_entries_travel_by_name_and_home_is_the_run_root(tmp_path: Path) -> None:
    config = make_config(
        tmp_path,
        LOCAL_WEB_EXECUTOR="docker",
        PI_GLINK_ACP_ENV="ACME_MODE=workspace;ACME_EMPTY=",
    )
    runtime = make_runtime(config, tmp_path)
    plan = make_plan(config, runtime, tmp_path)

    assert plan.argv[plan.argv.index("-e") :].count("ACME_MODE") == 1
    assert "ACME_EMPTY" in plan.argv
    assert plan.env["ACME_MODE"] == "workspace"
    assert plan.env["ACME_EMPTY"] == ""
    # The container's HOME is the mount, never the host's.
    assert f"HOME={docker_env.CONTAINER_HOME}" in plan.argv
    assert docker_env.CONTAINER_HOME != str(runtime.home)


def test_the_plan_reuses_the_pi_face_isolation_contract(tmp_path: Path) -> None:
    config = make_config(tmp_path, LOCAL_WEB_EXECUTOR="docker")
    acp_runtime = make_runtime(config, tmp_path, name="acp")
    acp_plan = make_plan(config, acp_runtime, tmp_path)

    from pi_glink.kernel import agent_env

    pi_runtime = agent_env.prepare_agent_dir(
        config,
        runtime_dir=tmp_path / "pi" / "pi-agent",
        session_dir=tmp_path / "pi" / "sessions",
    )
    pi_plan = docker_env.build_plan(
        config,
        pi_runtime,
        run_id="abc123",
        workspace=tmp_path / "workspace",
        session_dir=pi_runtime.session_dir,
    )

    def isolation(argv: list[str]) -> dict[str, str]:
        return {
            flag: argv[argv.index(flag) + 1]
            for flag in ("--cpus", "--memory", "--pids-limit", "--cap-drop", "--security-opt")
        }

    assert isolation(acp_plan.argv) == isolation(pi_plan.argv)
    assert isolation(acp_plan.argv) == {
        "--cpus": "2",
        "--memory": "2g",
        "--pids-limit": "512",
        "--cap-drop": "ALL",
        "--security-opt": "no-new-privileges",
    }


def test_the_pi_docker_plan_is_unchanged_by_the_acp_plan(tmp_path: Path) -> None:
    """The pi face's argv contract stays byte-for-byte where it was."""
    from pi_glink.kernel import agent_env

    config = make_config(tmp_path, PI_GLINK_HARNESS="pi", LOCAL_WEB_EXECUTOR="docker")
    runtime = agent_env.prepare_agent_dir(
        config,
        runtime_dir=tmp_path / "pi" / "pi-agent",
        session_dir=tmp_path / "pi" / "sessions",
    )
    plan = docker_env.build_plan(
        config,
        runtime,
        run_id="abc123",
        workspace=tmp_path / "workspace",
        session_dir=runtime.session_dir,
    )
    argv = plan.argv
    assert argv[:4] == ["docker", "run", "--rm", "-i"]
    assert argv[argv.index("--entrypoint") + 1] == agent_env.IMAGE_PACKAGE_GUARD
    assert f"{runtime.directory}:{docker_env.CONTAINER_CONFIG}" in argv
    assert f"{runtime.session_dir}:{docker_env.CONTAINER_SESSIONS}" in argv
    assert docker_env.CONTAINER_HOME not in argv, "pi 面不使用 ACP 的 HOME 挂载"


def test_the_acp_image_is_frozen_at_configuration_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config(
        tmp_path,
        LOCAL_WEB_EXECUTOR="docker",
        LOCAL_WEB_ACP_WORKER_IMAGE="pi-glink-acp-worker:pinned",
    )
    runtime = make_runtime(config, tmp_path)
    monkeypatch.setenv("LOCAL_WEB_ACP_WORKER_IMAGE", "pi-glink-acp-worker:other")
    plan = make_plan(config, runtime, tmp_path)
    assert plan.image == "pi-glink-acp-worker:pinned"
    assert docker_env.acp_worker_image(config) == "pi-glink-acp-worker:pinned"


# -- image pin boundary -------------------------------------------------------


def acp_worker_version(text: str, name: str) -> str:
    """One exact version ARG from the ACP worker Dockerfile.

    A missing or floating declaration (``latest``, a major-only range, a shell
    expansion) is a failure, not a warning: the image is the isolation boundary.
    """
    match = re.search(rf"^ARG\s+{name}=(\S+)\s*$", text, re.MULTILINE)
    if match is None:
        raise ValueError(f"ACP worker Dockerfile needs an exact {name}")
    value = match.group(1)
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[\w.]+)?", value):
        raise ValueError(f"{name} must be an exact version; got {value!r}")
    return value


def notes_package_pin(text: str, package: str) -> str:
    """Every version the integration notes record for one package.

    The notes are the repository's record of the versions that were actually
    measured; all mentions must agree so a half-done version bump is caught.
    """
    found = set(re.findall(rf"{re.escape(package)}@(\d+\.\d+\.\d+(?:-[\w.]+)?)", text))
    if not found:
        raise ValueError(f"集成笔记没有记录 {package} 的实测版本")
    if len(found) != 1:
        raise ValueError(f"{package} 的实测版本在集成笔记中不一致：{sorted(found)}")
    return next(iter(found))


def test_the_acp_worker_dockerfile_pins_exactly_what_the_notes_measured() -> None:
    text = ACP_DOCKERFILE.read_text(encoding="utf-8")
    notes = NOTES.read_text(encoding="utf-8")

    assert acp_worker_version(text, "DSH_VERSION") == notes_package_pin(
        notes, "@deepseek-ai/dsh"
    )
    assert acp_worker_version(text, "CODEX_ACP_VERSION") == notes_package_pin(
        notes, "@agentclientprotocol/codex-acp"
    )
    # M4 adds the claude pair: the ACP adapter and the CLI it drives. Both are
    # pinned exactly, and the notes record the measured versions.
    assert acp_worker_version(text, "CLAUDE_CODE_ACP_VERSION") == notes_package_pin(
        notes, "@zed-industries/claude-code-acp"
    )
    assert acp_worker_version(text, "CLAUDE_CODE_VERSION") == notes_package_pin(
        notes, "@anthropic-ai/claude-code"
    )
    # The install line consumes the pins instead of repeating a literal.
    assert '"@deepseek-ai/dsh@${DSH_VERSION}"' in text
    assert '"@agentclientprotocol/codex-acp@${CODEX_ACP_VERSION}"' in text
    assert '"@zed-industries/claude-code-acp@${CLAUDE_CODE_ACP_VERSION}"' in text
    assert '"@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}"' in text


def test_the_acp_worker_dockerfile_self_checks_every_agent() -> None:
    """构建期自检：四个组件的版本都能跑，且与 ARG 精确一致。

    原义务（M3a）：默认 CMD 直接跑 `dsh --version && codex-acp --version`。
    新义务（M4）：默认 CMD 指向镜像内自检脚本 `acp-self-check`（与 e2b 模板同形），
    覆盖 dsh、codex-acp、claude 与 claude-code-acp；claude-code-acp 没有 --version，
    用 `npm ls -g` 的本行输出复验。
    """
    text = ACP_DOCKERFILE.read_text(encoding="utf-8")
    assert "dsh --version" in text
    assert "codex-acp --version" in text
    assert "claude --version" in text
    assert 'grep -qx "${DSH_VERSION}"' in text
    assert 'grep -qx "@agentclientprotocol/codex-acp ${CODEX_ACP_VERSION}"' in text
    assert 'grep -qx "${CLAUDE_CODE_VERSION} (Claude Code)"' in text
    assert 'grep -qx "${CLAUDE_CODE_ACP_VERSION}"' in text
    assert "@zed-industries/claude-code-acp/package.json" in text
    # The default command is the same self-check, so an existing image can be
    # verified without knowing the pins.
    assert "/usr/local/bin/acp-self-check" in text
    assert 'CMD ["acp-self-check"]' in text
    assert "ENTRYPOINT []" in text, "宿主提供容器命令，镜像不得占用 entrypoint"


def test_the_acp_worker_shares_the_pi_node_tag_and_stays_self_contained() -> None:
    acp = ACP_DOCKERFILE.read_text(encoding="utf-8")
    pi = PI_DOCKERFILE.read_text(encoding="utf-8")
    acp_node = re.search(r"^FROM\s+(\S+)\s*$", acp, re.MULTILINE)
    pi_node = re.search(r"^FROM\s+(\S+)\s*$", pi, re.MULTILINE)
    assert acp_node is not None and pi_node is not None
    assert acp_node.group(1) == pi_node.group(1) == "node:24.21.0-bookworm-slim"
    # Both images install their agents at build time; nothing is installed by a
    # run inside the container.
    assert "npm install -g" in acp
    assert "apt-get install -y --no-install-recommends" in acp
    assert "/workspace" in acp


@pytest.mark.parametrize(
    "text",
    [
        "ARG DSH_VERSION=latest\n",
        "ARG DSH_VERSION=0.1\n",
        "ARG CODEX_ACP_VERSION=1.13\n",
        "ARG CODEX_ACP_VERSION=${SOMETHING_ELSE}\n",
        "ARG CLAUDE_CODE_VERSION=2.1\n",
        "ARG CLAUDE_CODE_ACP_VERSION=latest\n",
        "",
    ],
)
def test_a_floating_or_missing_acp_version_fails(text: str) -> None:
    with pytest.raises(ValueError, match="exact"):
        acp_worker_version(text, "DSH_VERSION")


def test_the_package_pin_boundary_detects_drift() -> None:
    notes = "pin: @deepseek-ai/dsh@0.1.5-rc.3 and @deepseek-ai/dsh@0.1.6"
    with pytest.raises(ValueError, match="不一致"):
        notes_package_pin(notes, "@deepseek-ai/dsh")
    with pytest.raises(ValueError, match="没有记录"):
        notes_package_pin(notes, "@agentclientprotocol/codex-acp")


# -- runner wiring ------------------------------------------------------------


def test_e2b_is_no_longer_refused_for_the_acp_family(tmp_path: Path) -> None:
    """原义务（M3a）：acp+e2b 在配置层与装配层都拒绝，直到 M3b 实测完成。

    新义务（M3b）：E2B 面已实现，配置接受该组合，装配层返回沙箱适配器。
    这里只钉“不再拒绝且交给沙箱面”的接缝；E2B 沙箱客户端、凭据交付、桥接
    与终态走 ``tests/test_kernel_acp_e2b.py`` 的 fake SDK 回归。
    """
    config = make_config(tmp_path, LOCAL_WEB_EXECUTOR="e2b")
    assert config.harness == "acp" and config.executor == "e2b"
    assert config.acp_e2b_template == "glink-acp-worker"

    runtime = make_runtime(config, tmp_path)
    spec = ExecutionSpec(
        config=config,
        runtime=runtime,
        run_id="contract-run",
        prompt="契约",
        system_prompt="",
        workspace=tmp_path,
        timeout_seconds=5,
    )

    class _FakeSandbox:
        sandbox_id = "fake-sandbox-id"
        argv = ["fake-acp"]
        template = "glink-acp-worker"

        def start(self, **_kwargs) -> None:
            pass

        def spawn(self):  # pragma: no cover - 装配契约只检查可调用
            raise AssertionError("spawn 不应在本用例中被调用")

    with pytest.raises(ConfigError, match="acp_e2b_plan"):
        # 没有沙箱 plan 的装配必须显式失败，不能静默回退到 on-prem 面。
        build_adapter(
            spec,
            harness="acp",
            executor="e2b",
            client_factory=PiRpcClient,
            on_frame=lambda _frame: None,
            rpc_plan=lambda _spec: ([], {}, None),
            docker_plan=lambda _spec: ([], {}, "container"),
            docker_stop=lambda _name: [],
            acp_plan=lambda _spec: ([], {}, None),
        )

    from pi_glink.kernel.acp_execution import AcpSandboxExecutionAdapter

    adapter = build_adapter(
        spec,
        harness="acp",
        executor="e2b",
        client_factory=PiRpcClient,
        on_frame=lambda _frame: None,
        rpc_plan=lambda _spec: ([], {}, None),
        docker_plan=lambda _spec: ([], {}, "container"),
        docker_stop=lambda _name: [],
        acp_plan=lambda _spec: ([], {}, None),
        acp_e2b_plan=lambda _spec: _FakeSandbox(),
    )
    assert isinstance(adapter, AcpSandboxExecutionAdapter)
    assert adapter.identity() == {"sandbox": "fake-sandbox-id"}


def test_a_missing_daemon_fails_closed_and_keeps_the_persisted_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ACP docker path is really wired: it reaches the daemon gate and the
    run record keeps its container name for the online ledger."""
    config = make_config(tmp_path, LOCAL_WEB_EXECUTOR="docker")
    store = RunStore(config.data_root / "runs")
    monkeypatch.setattr("pi_glink.kernel.runner.docker_available", lambda: False)
    manager = RunManager(config, EventBus(), store)
    record = manager.start("docker 不可用时应当失败")
    final = manager.wait(record.run_id, timeout=30)

    assert final is not None and final.status == FAILED
    assert final.error is not None and "Docker daemon" in final.error
    assert final.container_name == docker_env.container_name_for(record.run_id)


def test_the_acp_container_identity_is_reconciled_by_its_persisted_name(
    tmp_path: Path,
) -> None:
    """``confirm_absent`` semantics apply to ACP runs through the same ledger."""
    config = make_config(tmp_path, LOCAL_WEB_EXECUTOR="docker")
    store = RunStore(config.data_root / "runs")
    store.create("残留的 ACP run", run_id="acp-leak")
    store.update(
        "acp-leak",
        status=FINISHED,
        finished_at=1.0,
        container_name=docker_env.container_name_for("acp-leak"),
        cleanup={"released": False, "failures": ["synthetic"], "external": {}},
    )
    probed: list[str] = []

    def probe(name: str) -> tuple[bool, str]:
        probed.append(name)
        return True, "fake docker: 容器不存在"

    manager = RunManager(config, EventBus(), store, docker_probe=probe)
    assert manager.reconcile_resources() == ["acp-leak"]
    assert probed == [docker_env.container_name_for("acp-leak")]
    assert store.holding_records() == []
    assert store.get("acp-leak").resource_adjudication["method"] == "docker-inspect"


def test_the_acp_docker_plan_feeds_the_adapter_through_the_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The adapter receives the container plan and the container's session cwd.

    A fake client records what ``build_adapter`` handed the adapter; no daemon
    is contacted and no container is started.
    """
    config = make_config(
        tmp_path,
        LOCAL_WEB_EXECUTOR="docker",
        LOCAL_WEB_ACP_WORKER_IMAGE="pi-glink-acp-worker:test",
        PI_GLINK_ACP_COMMAND="codex-acp --flag value",
        PI_GLINK_ACP_ENV="ACME_MODE=workspace",
    )
    runtime = make_runtime(config, tmp_path, name="plan")
    seen: dict[str, object] = {}

    class _RecordingClient:
        def __init__(self, argv, **kwargs):
            seen["argv"] = list(argv)
            seen["kwargs"] = kwargs

        def start(self) -> None:
            pass

        def request(self, method, params=None, *, timeout=None):
            if method == "initialize":
                return {"protocolVersion": 1}
            if method == "session/new":
                seen["session_new"] = dict(params or {})
                return {"sessionId": "fake-session"}
            raise AssertionError(method)

        def notify(self, *_args, **_kwargs) -> None:
            pass

        def close(self, **_kwargs):
            return 0

    removed: list[list[str]] = []

    def fake_run(argv, **_kwargs):
        removed.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    spec = ExecutionSpec(
        config=config,
        runtime=runtime,
        run_id="abc123",
        prompt="契约",
        system_prompt="",
        workspace=tmp_path / "workspace",
        timeout_seconds=5,
    )
    manager = RunManager(config, EventBus(), RunStore(config.data_root / "runs"))
    monkeypatch.setattr("pi_glink.kernel.runner.docker_available", lambda: True)
    monkeypatch.setattr("pi_glink.kernel.execution.AcpRpcClient", _RecordingClient)
    monkeypatch.setattr("pi_glink.kernel.acp_execution.subprocess.run", fake_run)
    adapter = build_adapter(
        spec,
        harness="acp",
        executor="docker",
        client_factory=PiRpcClient,
        on_frame=lambda _frame: None,
        rpc_plan=lambda _spec: ([], {}, None),
        docker_plan=lambda _spec: ([], {}, "container"),
        docker_stop=docker_env.stop_argv,
        acp_plan=lambda _spec: ([], {}, None),
        acp_docker_plan=manager._acp_docker_plan,
    )
    expected = docker_env.build_acp_plan(
        config, runtime, run_id="abc123", workspace=tmp_path / "workspace"
    )
    assert adapter.identity() == {"container": expected.container_name}

    adapter.start()
    assert seen["argv"] == expected.argv, "运行期 argv 来自 docker_env.build_acp_plan"
    assert seen["argv"][seen["argv"].index("pi-glink-acp-worker:test") + 1 :] == [
        "codex-acp",
        "--flag",
        "value",
    ]
    assert seen["kwargs"]["cwd"] is None, "容器面没有宿主 cwd"
    assert seen["kwargs"]["env"] == expected.env
    # The ACP session works in the container mount, not a host path.
    assert seen["session_new"]["cwd"] == docker_env.CONTAINER_WORKSPACE
    session = adapter.session_info()
    assert session is not None and session.payload["cwd"] == docker_env.CONTAINER_WORKSPACE

    report = adapter.close()
    assert report.released is True and report.external == {"container": expected.container_name}
    assert removed == [["docker", "rm", "-f", expected.container_name]]


def test_the_command_is_split_without_a_shell(tmp_path: Path) -> None:
    config = make_config(
        tmp_path,
        LOCAL_WEB_EXECUTOR="docker",
        PI_GLINK_ACP_COMMAND="python -m fake --config /workspace/cfg.json",
    )
    runtime = make_runtime(config, tmp_path)
    plan = make_plan(config, runtime, tmp_path)
    tail = plan.argv[plan.argv.index(plan.image) :]
    assert tail[1:] == ["python", "-m", "fake", "--config", "/workspace/cfg.json"]
    assert shlex.split(config.acp_command) == tail[1:]
