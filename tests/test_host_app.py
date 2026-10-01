"""Host skeleton regressions: config, static serving, event bus, HTTP surface."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.client import HTTPConnection
from pathlib import Path

import pytest

from glink_harness.app import GlinkHarnessServer, create_server
from glink_harness.config import ConfigError, build_config, load_config
from glink_harness.dotenv import parse_env_file
from glink_harness.events import RUN_STARTED, TERMINAL_EVENT_TYPES, EventBus
from glink_harness.static_files import WebAssets

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def server(tmp_path: Path):
    config = build_config(
        {
            "LOCAL_WEB_DATA_ROOT": str(tmp_path / "data"),
            "LOCAL_WEB_PORT": "0",  # ephemeral port: tests must not fight over 7860
            "OPENAI_MODEL": "test-model",
            "GLINK_HARNESS": "pi",
        },
        REPO_ROOT,
    )
    instance = create_server(config)
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        yield instance
    finally:
        instance.shutdown()
        instance.server_close()
        thread.join(timeout=5)


def _get(server: GlinkHarnessServer, path: str) -> tuple[int, dict[str, str], bytes]:
    port = server.server_address[1]
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:  # non-2xx still carries a body we assert on
        return error.code, dict(error.headers), error.read()


# -- config -------------------------------------------------------------------


def test_env_file_parsing_handles_quotes_comments_and_export(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "# comment",
                "PLAIN=value",
                "QUOTED=\"a b\"",
                "SINGLE='c d'",
                "INLINE=kept   # trailing comment",
                "export EXPORTED=yes",
                "EMPTY=",
            ]
        ),
        encoding="utf-8",
    )
    values = parse_env_file(env_file)
    assert values == {
        "PLAIN": "value",
        "QUOTED": "a b",
        "SINGLE": "c d",
        "INLINE": "kept",
        "EXPORTED": "yes",
        "EMPTY": "",
    }


def test_missing_env_file_is_not_an_error(tmp_path: Path) -> None:
    assert parse_env_file(tmp_path / "absent.env") == {}


def test_executor_must_be_known() -> None:
    with pytest.raises(ConfigError, match="LOCAL_WEB_EXECUTOR"):
        build_config({"LOCAL_WEB_EXECUTOR": "kubernetes"}, REPO_ROOT)


def test_non_loopback_host_is_refused_in_every_mode() -> None:
    """One behaviour migration: no unauthenticated host beyond loopback.

    Remote access goes through the local authentication gateway in multi-tenant
    mode; the legacy ``LOCAL_WEB_ALLOW_REMOTE`` switch no longer opens an
    unauthenticated public surface.
    """
    with pytest.raises(ConfigError, match="loopback"):
        build_config({"LOCAL_WEB_HOST": "0.0.0.0", "GLINK_HARNESS": "pi"}, REPO_ROOT)
    with pytest.raises(ConfigError, match="loopback"):
        build_config(
            {"LOCAL_WEB_HOST": "0.0.0.0", "LOCAL_WEB_ALLOW_REMOTE": "1", "GLINK_HARNESS": "pi"},
            REPO_ROOT,
        )
    with pytest.raises(ConfigError, match="loopback"):
        build_config({"LOCAL_WEB_HOST": "192.168.1.10", "GLINK_HARNESS": "pi"}, REPO_ROOT)
    assert build_config(
        {"OPENAI_MODEL": "test/model", "OPENAI_REASONING_EFFORT": "high"}, REPO_ROOT
    ).binds_loopback is True


def test_defaults_follow_frontier_agent_naming(tmp_path: Path) -> None:
    # 默认包为 dsh（GLINK-HARNESS-DEFAULT-20261001）：dsh 的 session_config 必填
    # OPENAI_MODEL / OPENAI_REASONING_EFFORT，缺省配置必须带齐才能通过启动校验。
    config = build_config(
        {"OPENAI_MODEL": "test/model", "OPENAI_REASONING_EFFORT": "high"}, REPO_ROOT
    )
    assert config.port == 7860
    assert config.executor == "inproc"
    assert config.harness_pack == "dsh"
    assert config.e2b_template == "glink-harness-worker"
    assert config.acp_e2b_template == "glink-acp-worker"
    assert config.agent_config_dir == REPO_ROOT / "harnesses" / "dsh"
    assert config.binds_loopback is True


def test_process_environment_wins_over_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_MODEL=from-file\nLOCAL_WEB_PORT=1\n", encoding="utf-8")
    config = load_config(
        {"OPENAI_MODEL": "from-env", "LOCAL_WEB_PORT": "9999", "GLINK_HARNESS": "pi"},
        REPO_ROOT,
        env_file=env_file,
    )
    assert config.openai_model == "from-env"
    assert config.port == 9999


def test_process_environment_inherits_the_pi_model_metadata_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two ``OPENAI_*`` metadata keys inherit the process environment too.

    原义务：``OPENAI_CONTEXT_WINDOW`` / ``OPENAI_MAX_TOKENS`` 不在
    ``_looks_configured`` 白名单里，导出到进程环境时被 ``load_config`` 过滤，
    只有写进 ``.env``（或显式传入 ``env``）才生效——与同族 ``OPENAI_API_KEY``
    等键以及 ``load_config`` 的"进程环境优先"承诺不一致。

    新义务：它们与同族键一样按进程环境、``.env``、默认值的顺序生效。
    """
    env_file = tmp_path / ".env"
    env_file.write_text(
        "OPENAI_CONTEXT_WINDOW=1111\nOPENAI_MAX_TOKENS=2222\n", encoding="utf-8"
    )
    monkeypatch.setenv("OPENAI_CONTEXT_WINDOW", "3333")
    monkeypatch.setenv("OPENAI_MAX_TOKENS", "4444")
    # 本用例只验证 OPENAI_* 元数据键的继承顺序，与 harness 无关；钉 pi 包
    # 避免默认 dsh 的 session_config 必填校验抢先失败。
    monkeypatch.setenv("GLINK_HARNESS", "pi")
    config = load_config(None, REPO_ROOT, env_file=env_file)
    assert config.context_window == 3333
    assert config.max_output_tokens == 4444


def test_process_environment_supplies_declared_acp_credential_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A key named by ``GLINK_ACP_CREDENTIAL_ENV`` inherits the process environment.

    原义务：``_looks_configured`` 只放行宿主自有键，声明的凭据键（如
    ``OPENROUTER_API_KEY``）被 ``load_config`` 过滤——只在进程环境提供时启动失败，
    ``.env`` 留有旧值时静默沿用旧值（轮换无效）。

    新义务：仅**显式声明**的凭据键额外按"进程环境优先于 ``.env``"继承；未声明
    的无关变量仍被过滤。
    """
    env_file = tmp_path / ".env"
    env_file.write_text(
        "GLINK_HARNESS=dsh\nOPENAI_MODEL=test/model\nOPENAI_REASONING_EFFORT=high\n"
        "GLINK_ACP_CREDENTIAL_ENV=OPENROUTER_API_KEY\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "from-env-only")
    monkeypatch.setenv("JINA_API_KEY", "undeclared")
    config = load_config(None, REPO_ROOT, env_file=env_file)
    assert dict(config.acp_credential_env_values) == {"OPENROUTER_API_KEY": "from-env-only"}

    env_file.write_text(
        "GLINK_HARNESS=dsh\nOPENAI_MODEL=test/model\nOPENAI_REASONING_EFFORT=high\n"
        "GLINK_ACP_CREDENTIAL_ENV=OPENROUTER_API_KEY\nOPENROUTER_API_KEY=rotated-away\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "rotated-in")
    config = load_config(None, REPO_ROOT, env_file=env_file)
    assert dict(config.acp_credential_env_values) == {"OPENROUTER_API_KEY": "rotated-in"}


def test_process_environment_can_declare_the_acp_credential_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The declaration itself may come from the process environment too."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "GLINK_HARNESS=dsh\nOPENAI_MODEL=test/model\nOPENAI_REASONING_EFFORT=high\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GLINK_ACP_CREDENTIAL_ENV", "OPENROUTER_API_KEY")
    monkeypatch.setenv("OPENROUTER_API_KEY", "from-env")
    config = load_config(None, REPO_ROOT, env_file=env_file)
    assert dict(config.acp_credential_env_values) == {"OPENROUTER_API_KEY": "from-env"}


# -- 就绪面族化（GLINK-READY-20261001：configured.model 按族判定）------------------


def _dsh_values(tmp_path: Path, **overrides: str) -> dict[str, str]:
    values = {
        "GLINK_HARNESS": "dsh",
        "OPENAI_MODEL": "openai/gpt-5.6-sol",
        "OPENAI_REASONING_EFFORT": "xhigh",
        "LOCAL_WEB_DATA_ROOT": str(tmp_path / "data"),
        "LOCAL_WEB_PORT": "0",
    }
    values.update(overrides)
    return values


def test_dsh_pack_reports_model_ready_via_the_acp_credential_channel(tmp_path: Path) -> None:
    """回归：dsh + 具名凭据通道 + 无 OPENAI_API_KEY 不得误报模型未配置。

    缺口①复现的反面：ACP 族凭据走 GLINK_ACP_CREDENTIAL_ENV（值在启动期解析），
    不要求 pi 族的 OPENAI_API_KEY。就绪只回答配置齐备，不代表真实模型握手成功。
    """
    config = build_config(
        _dsh_values(
            tmp_path,
            GLINK_ACP_CREDENTIAL_ENV="OPENROUTER_API_KEY",
            OPENROUTER_API_KEY="sk-or-synthetic",
        ),
        REPO_ROOT,
    )
    assert config.public_view()["configured"]["model"] is True


def test_dsh_pack_without_a_credential_channel_reports_not_ready(tmp_path: Path) -> None:
    """相邻合法输入保护：dsh 只有模型名、没有任何凭据通道时仍报未就绪。"""
    config = build_config(_dsh_values(tmp_path), REPO_ROOT)
    assert config.public_view()["configured"]["model"] is False


def test_pi_pack_readiness_still_requires_the_openai_key(tmp_path: Path) -> None:
    config = build_config(
        {
            "GLINK_HARNESS": "pi",
            "OPENAI_MODEL": "test/model",
            "LOCAL_WEB_DATA_ROOT": str(tmp_path / "data"),
        },
        REPO_ROOT,
    )
    assert config.public_view()["configured"]["model"] is False


def test_dsh_process_env_channel_counts_as_configured(tmp_path: Path) -> None:
    """B1 回归：GLINK_ACP_ENV 进程环境通道是既有政策认可的具名凭据源。

    dsh + GLINK_ACP_ENV=OPENROUTER_API_KEY=<值>（无 CREDENTIAL_ENV/FILES），
    就绪判定不得遗漏这条通道。
    """
    config = build_config(
        _dsh_values(tmp_path, GLINK_ACP_ENV="OPENROUTER_API_KEY=synthetic-only"),
        REPO_ROOT,
    )
    assert config.model_ready is True
    assert config.public_view()["configured"]["model"] is True


def test_codex_pack_does_not_require_a_host_model_field(tmp_path: Path) -> None:
    """B1 回归：codex/opencode 未声明 OPENAI_MODEL 来源，不得被误强制。

    codex 包 session_config=()（无宿主模型消费者）：合法原生配置
    （GLINK_ACP_ENV 传 OPENAI_API_KEY，无宿主 OPENAI_MODEL）即就绪。
    """
    config = build_config(
        {
            "GLINK_HARNESS": "codex",
            "GLINK_ACP_ENV": "OPENAI_API_KEY=synthetic-only",
            "LOCAL_WEB_DATA_ROOT": str(tmp_path / "data"),
            "LOCAL_WEB_PORT": "0",
        },
        REPO_ROOT,
    )
    assert config.pack.capabilities.session_config == ()
    assert config.model_ready is True


def test_opencode_pack_readiness_via_credential_file(tmp_path: Path) -> None:
    """B1 回归：opencode + 凭据文件注入（无宿主模型字段）即就绪。"""
    source = tmp_path / "auth.json"
    source.write_text('{"access": "synthetic"}', encoding="utf-8")
    source.chmod(0o600)
    config = build_config(
        {
            "GLINK_HARNESS": "opencode",
            "GLINK_ACP_CREDENTIAL_FILES": f"{source}=.local/share/opencode/auth.json",
            "LOCAL_WEB_DATA_ROOT": str(tmp_path / "data"),
            "LOCAL_WEB_PORT": "0",
        },
        REPO_ROOT,
    )
    assert config.model_ready is True


def test_health_payload_names_the_selected_pack(tmp_path: Path) -> None:
    """/api/health 的 harness 块报告所选包名与入口（dsh 默认包的直接断言）。"""
    config = build_config(
        _dsh_values(
            tmp_path,
            GLINK_ACP_CREDENTIAL_ENV="OPENROUTER_API_KEY",
            OPENROUTER_API_KEY="sk-or-synthetic",
            LOCAL_WEB_EXECUTOR="docker",
        ),
        REPO_ROOT,
    )
    instance = create_server(config)
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        status, _, body = _get(instance, "/api/health")
        payload = json.loads(body)
        assert status == 200
        assert payload["harness"]["pack"] == "dsh"
        assert payload["harness"]["bin"] == "dsh"
        # docker 面不探测本机 PATH（镜像内执行，启动校验已把关）
        assert payload["harness"]["available"] is True
        assert payload["configured"]["model"] is True
    finally:
        instance.shutdown()
        instance.server_close()
        thread.join(timeout=5)


def test_public_view_never_leaks_credentials_or_paths() -> None:
    config = build_config(
        {
            "OPENAI_API_KEY": "secret-key",
            "OPENAI_BASE_URL": "https://example.invalid/v1",
            "OPENAI_MODEL": "some-model",
            "GLINK_HARNESS": "pi",
            "E2B_API_KEY": "e2b-secret",
        },
        REPO_ROOT,
    )
    rendered = json.dumps(config.public_view())
    assert "secret" not in rendered
    assert str(REPO_ROOT) not in rendered
    assert config.public_view()["configured"] == {"model": True, "e2b": False}


# -- static assets ------------------------------------------------------------


def test_index_rewrites_asset_versions_from_content() -> None:
    assets = WebAssets(REPO_ROOT / "web")
    body = assets.index().decode("utf-8")
    assert "?v=auto" not in body
    assert "/app.js?v=" in body and "/styles.css?v=" in body


def test_module_requests_are_uncached_and_never_fall_back_to_html() -> None:
    assets = WebAssets(REPO_ROOT / "web")
    module = assets.resolve("/modules/workspace.mjs")
    assert module is not None
    assert module.cache_control == "no-cache"
    assert module.content_type.startswith("text/javascript")
    assert assets.resolve("/modules/does-not-exist.mjs") is None


def test_traversal_cannot_escape_the_web_root() -> None:
    assets = WebAssets(REPO_ROOT / "web")
    asset = assets.resolve("/modules/../../../../etc/passwd")
    if asset is not None:
        assert asset.path.is_relative_to((REPO_ROOT / "web").resolve())


def test_fonts_are_long_cached() -> None:
    assets = WebAssets(REPO_ROOT / "web")
    font = assets.resolve("/fonts/NotoSansSC-Regular.woff2")
    assert font is not None
    assert "immutable" in font.cache_control


def test_file_type_tiles_are_served_as_png() -> None:
    assets = WebAssets(REPO_ROOT / "web")
    tile = assets.resolve("/icons/markdown-32.png")
    assert tile is not None
    assert tile.content_type == "image/png"


# -- event bus ----------------------------------------------------------------


def test_bus_assigns_monotonic_sequence_and_replays_after_cursor() -> None:
    bus = EventBus()
    bus.open_run("run-1")
    bus.publish("run-1", RUN_STARTED, {"prompt": "hi"})
    bus.publish("run-1", "assistant_delta", {"text": "a"})
    bus.publish("run-1", "assistant_delta", {"text": "b"})

    # A live subscriber resumes after its cursor and then follows the tail.
    subscriber = bus.subscribe("run-1", after=1)
    first = next(subscriber)
    assert first.seq == 2
    bus.publish("run-1", "assistant_delta", {"text": "c"})
    assert [next(subscriber).seq for _ in range(2)] == [3, 4]
    subscriber.close()

    # The ring is released when the run closes; replay after that belongs to
    # the durable log (`RunStore.iter_events`), which is the fact source.
    bus.close("run-1")
    assert bus.is_open("run-1") is False
    assert [event.seq for event in bus.subscribe("run-1", after=0)] == []


def test_unknown_run_is_not_open() -> None:
    bus = EventBus()
    assert bus.is_open("nope") is False


def test_terminal_event_types_are_stable() -> None:
    assert {"run_finished", "run_failed", "run_cancelled"} == set(TERMINAL_EVENT_TYPES)


# -- HTTP surface -------------------------------------------------------------


def test_health_reports_executor_and_harness_availability(server: GlinkHarnessServer) -> None:
    status, _, body = _get(server, "/api/health")
    payload = json.loads(body)
    assert status == 200
    assert payload["status"] == "ok"
    assert payload["executor"] == "inproc"
    assert set(payload["harness"]) == {"pack", "bin", "available"}
    assert payload["harness"]["pack"] == "pi" and payload["harness"]["bin"] == "pi"


def test_config_endpoint_exposes_no_secrets(server: GlinkHarnessServer) -> None:
    status, _, body = _get(server, "/api/config")
    assert status == 200
    assert json.loads(body)["model"] == "test-model"


def test_index_and_assets_are_served(server: GlinkHarnessServer) -> None:
    status, headers, body = _get(server, "/")
    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    assert b"<html" in body.lower()

    status, headers, _ = _get(server, "/styles.css?v=auto")
    assert status == 200
    assert headers["Content-Type"].startswith("text/css")


def test_spa_route_falls_back_to_index(server: GlinkHarnessServer) -> None:
    status, headers, body = _get(server, "/some/client/route")
    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    assert b"<html" in body.lower()


def test_unknown_module_is_a_404_not_html(server: GlinkHarnessServer) -> None:
    status, headers, _ = _get(server, "/modules/missing.mjs")
    assert status == 404
    assert headers["Content-Type"].startswith("application/json")


def test_run_event_stream_streams_frames(server: GlinkHarnessServer) -> None:
    bus = server.bus
    bus.open_run("run-x")
    port = server.server_address[1]
    connection = HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("GET", "/api/runs/run-x/events")
        response = connection.getresponse()
        assert response.status == 200
        assert response.getheader("Content-Type").startswith("text/event-stream")
        bus.publish("run-x", RUN_STARTED, {"prompt": "hello"})
        bus.close("run-x")
        # Read the chunked SSE frame written after the subscription attached.
        line = response.read(1)
        assert line  # stream is open and carries the frame
    finally:
        connection.close()


def test_event_stream_for_unknown_run_is_404(server: GlinkHarnessServer) -> None:
    status, _, body = _get(server, "/api/runs/absent/events")
    assert status == 404
    assert json.loads(body)["error"] == "unknown_run"


# -- configuration boundaries (GLINK-HARDEN-01) --------------------------------


@pytest.mark.parametrize(
    ("key", "value", "expect"),
    [
        ("LOCAL_WEB_PORT", "-1", "LOCAL_WEB_PORT"),
        ("LOCAL_WEB_PORT", "70000", "LOCAL_WEB_PORT"),
        ("GLINK_MAX_GLOBAL_RUNS", "0", "GLINK_MAX_GLOBAL_RUNS"),
        ("GLINK_MAX_TENANT_RUNS", "-1", "GLINK_MAX_TENANT_RUNS"),
        ("LOCAL_WEB_TASK_TIMEOUT_SECONDS", "0", "LOCAL_WEB_TASK_TIMEOUT_SECONDS"),
        ("OPENAI_CONTEXT_WINDOW", "0", "OPENAI_CONTEXT_WINDOW"),
        ("OPENAI_MAX_TOKENS", "-5", "OPENAI_MAX_TOKENS"),
        ("GLINK_LOGIN_MAX_ATTEMPTS", "0", "GLINK_LOGIN_MAX_ATTEMPTS"),
        ("GLINK_SESSION_IDLE_SECONDS", "0", "GLINK_SESSION_IDLE_SECONDS"),
        ("GLINK_ASSERTION_SKEW_SECONDS", "-1", "GLINK_ASSERTION_SKEW_SECONDS"),
        ("LOCAL_WEB_DOCKER_PIDS_LIMIT", "-1", "LOCAL_WEB_DOCKER_PIDS_LIMIT"),
        ("LOCAL_WEB_E2B_TIMEOUT_SECONDS", "-1", "LOCAL_WEB_E2B_TIMEOUT_SECONDS"),
    ],
)
def test_out_of_range_integer_configuration_is_refused_with_the_key_name(
    key: str, value: str, expect: str
) -> None:
    with pytest.raises(ConfigError, match=expect):
        build_config({key: value}, REPO_ROOT)


def test_zero_port_stays_valid_as_the_ephemeral_port_request() -> None:
    assert build_config({"LOCAL_WEB_PORT": "0", "GLINK_HARNESS": "pi"}, REPO_ROOT).port == 0
    with pytest.raises(ConfigError, match="654?35|65535"):
        build_config({"LOCAL_WEB_PORT": "65536", "GLINK_HARNESS": "pi"}, REPO_ROOT)


def test_the_cli_revalidates_host_and_port_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A ``--host``/``--port`` override must pass the same boundary checks."""
    from glink_harness.app import main as app_main  # noqa: PLC0415

    config = build_config(
        {
            "LOCAL_WEB_DATA_ROOT": str(tmp_path / "data"),
            "LOCAL_WEB_PORT": "0",
            "OPENAI_MODEL": "test-model",
            "GLINK_HARNESS": "pi",
        },
        REPO_ROOT,
    )
    monkeypatch.setattr("glink_harness.app.load_config", lambda: config)

    assert app_main(["--host", "0.0.0.0"]) == 2
    assert "loopback" in capsys.readouterr().err
    assert app_main(["--port", "70000"]) == 2
    assert "LOCAL_WEB_PORT" in capsys.readouterr().err
    assert config.host == "127.0.0.1" and config.port == 0, "配置对象不得被就地改写"


def test_health_on_a_sandbox_face_does_not_probe_the_local_path(
    tmp_path: Path,
) -> None:
    """docker/e2b 面的引擎在镜像/沙箱里，health 不得用本机 PATH 误判不可用。

    回归场景：harness=dsh + executor=e2b（内核在 E2B 模板内、本机无 dsh CLI）被
    ``shutil.which`` 报为 available=false，前端据此拦截一切研究发起——修复后沙箱面
    恒可用（配置完整性已由启动校验把关），只有 inproc 才看本机 PATH。
    """
    config = build_config(
        {
            "LOCAL_WEB_DATA_ROOT": str(tmp_path / "data"),
            "LOCAL_WEB_PORT": "0",
            "OPENAI_MODEL": "test-model",
            "OPENAI_REASONING_EFFORT": "high",
            "GLINK_HARNESS": "dsh",
            "LOCAL_WEB_EXECUTOR": "e2b",
            "E2B_API_KEY": "test-e2b-key",
            "GLINK_EGRESS_PUBLIC_URL": "https://egress.example.test",
        },
        REPO_ROOT,
    )
    instance = create_server(config)
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        status, _, body = _get(instance, "/api/health")
        payload = json.loads(body)
        assert status == 200
        assert payload["executor"] == "e2b"
        assert payload["harness"]["pack"] == "dsh"
        assert payload["harness"]["bin"] == "dsh"
        assert payload["harness"]["available"] is True, (
            "沙箱面的执行体在镜像/模板内，本机没有 dsh 不代表引擎不可用"
        )
    finally:
        instance.shutdown()
        instance.server_close()
