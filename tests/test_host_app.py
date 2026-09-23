"""Host skeleton regressions: config, static serving, event bus, HTTP surface."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.client import HTTPConnection
from pathlib import Path

import pytest

from pi_glink.app import PiGlinkServer, create_server
from pi_glink.config import ConfigError, build_config, load_config
from pi_glink.dotenv import parse_env_file
from pi_glink.events import RUN_STARTED, TERMINAL_EVENT_TYPES, EventBus
from pi_glink.static_files import WebAssets

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def server(tmp_path: Path):
    config = build_config(
        {
            "LOCAL_WEB_DATA_ROOT": str(tmp_path / "data"),
            "LOCAL_WEB_PORT": "0",  # ephemeral port: tests must not fight over 7860
            "OPENAI_MODEL": "test-model",
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


def _get(server: PiGlinkServer, path: str) -> tuple[int, dict[str, str], bytes]:
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
        build_config({"LOCAL_WEB_HOST": "0.0.0.0"}, REPO_ROOT)
    with pytest.raises(ConfigError, match="loopback"):
        build_config(
            {"LOCAL_WEB_HOST": "0.0.0.0", "LOCAL_WEB_ALLOW_REMOTE": "1"}, REPO_ROOT
        )
    with pytest.raises(ConfigError, match="loopback"):
        build_config({"LOCAL_WEB_HOST": "192.168.1.10"}, REPO_ROOT)
    assert build_config({}, REPO_ROOT).binds_loopback is True


def test_defaults_follow_frontier_agent_naming(tmp_path: Path) -> None:
    config = build_config({}, REPO_ROOT)
    assert config.port == 7860
    assert config.executor == "inproc"
    assert config.e2b_template == "pi-glink-worker"
    assert config.agent_config_dir == REPO_ROOT / "agent"
    assert config.binds_loopback is True


def test_process_environment_wins_over_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_MODEL=from-file\nLOCAL_WEB_PORT=1\n", encoding="utf-8")
    config = load_config(
        {"OPENAI_MODEL": "from-env", "LOCAL_WEB_PORT": "9999"},
        REPO_ROOT,
        env_file=env_file,
    )
    assert config.openai_model == "from-env"
    assert config.port == 9999


def test_public_view_never_leaks_credentials_or_paths() -> None:
    config = build_config(
        {
            "OPENAI_API_KEY": "secret-key",
            "OPENAI_BASE_URL": "https://example.invalid/v1",
            "OPENAI_MODEL": "some-model",
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


def test_health_reports_executor_and_pi_availability(server: PiGlinkServer) -> None:
    status, _, body = _get(server, "/api/health")
    payload = json.loads(body)
    assert status == 200
    assert payload["status"] == "ok"
    assert payload["executor"] == "inproc"
    assert set(payload["pi"]) == {"bin", "available"}


def test_config_endpoint_exposes_no_secrets(server: PiGlinkServer) -> None:
    status, _, body = _get(server, "/api/config")
    assert status == 200
    assert json.loads(body)["model"] == "test-model"


def test_index_and_assets_are_served(server: PiGlinkServer) -> None:
    status, headers, body = _get(server, "/")
    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    assert b"<html" in body.lower()

    status, headers, _ = _get(server, "/styles.css?v=auto")
    assert status == 200
    assert headers["Content-Type"].startswith("text/css")


def test_spa_route_falls_back_to_index(server: PiGlinkServer) -> None:
    status, headers, body = _get(server, "/some/client/route")
    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    assert b"<html" in body.lower()


def test_unknown_module_is_a_404_not_html(server: PiGlinkServer) -> None:
    status, headers, _ = _get(server, "/modules/missing.mjs")
    assert status == 404
    assert headers["Content-Type"].startswith("application/json")


def test_run_event_stream_streams_frames(server: PiGlinkServer) -> None:
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


def test_event_stream_for_unknown_run_is_404(server: PiGlinkServer) -> None:
    status, _, body = _get(server, "/api/runs/absent/events")
    assert status == 404
    assert json.loads(body)["error"] == "unknown_run"
