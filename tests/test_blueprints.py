"""Regressions for host-side session blueprints (GLINK-ACP-10, first increment).

The blueprint is a repository file (``blueprints/<name>.md``): a harness-agnostic
session template whose stage list declares the outcome contract. These tests
pin the increment's obligations: the accepted names are derived from the
directory (never hardcoded), admission fails closed on an unknown or malformed
template before any run exists, the render is deterministic and travels through
the system-prompt channel of both harness faces without replacing the user's
prompt, the run record carries the template's identity and stage metadata with
no new product event, and tenant ownership is unchanged.

Every execution face here is synthetic: the pi face is ``fake_pi.py``, the ACP
face is ``fake_acp.py``, the HTTP tests bind an OS-assigned loopback port and a
temporary data root. No model, container, sandbox or real configuration file is
involved.
"""

from __future__ import annotations

import json
import re
import shlex
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Mapping

import pytest

from pi_glink import events as events_module
from pi_glink.app import PiGlinkServer, create_server
from pi_glink.blueprints import (
    BlueprintError,
    blueprint_names,
    load_blueprint,
    parse_blueprint,
    render_blueprint,
)
from pi_glink.config import HostConfig, build_config

from acp_pack import apply_acp_pack
from pi_glink.events import EventBus
from pi_glink.runs.runner import BlueprintUnavailable, RunManager
from pi_glink.kernel.rpc import PiRpcClient
from pi_glink.store import RunStore
from pi_glink.tenancy import TenantContext, TenantCredentialStore

REPO_ROOT = Path(__file__).resolve().parents[1]
BLUEPRINTS = REPO_ROOT / "methodology" / "blueprints"
FAKE_PI = Path(__file__).resolve().parent / "fixtures" / "fake_pi.py"
FAKE_ACP = Path(__file__).resolve().parent / "fixtures" / "fake_acp.py"
CONTRACT_PATH = REPO_ROOT / "methodology" / "work-agreement.md"
CAPABILITIES_PATH = REPO_ROOT / "harnesses" / "pi" / "capabilities.md"


def agreement_text() -> str:
    return CONTRACT_PATH.read_text(encoding="utf-8").strip()


def pi_system_prompt(*parts: str) -> str:
    """The pi system prompt: work agreement, pi pack capabilities, then the rest."""
    capabilities = CAPABILITIES_PATH.read_text(encoding="utf-8").strip()
    return "\n\n".join(part for part in (agreement_text(), capabilities, *parts) if part)


def acp_system_prompt(*parts: str) -> str:
    """The ACP test pack carries no capabilities.md: agreement, then the rest."""
    return "\n\n".join(part for part in (agreement_text(), *parts) if part)
AUDIENCE = "pi-glink-test"


def synthetic_document(
    name: str,
    *,
    description: str = "",  # filled from the name when empty
    body: str = "",
) -> str:
    """A minimal well-formed blueprint with two stages, for admission tests."""
    return (
        "+++\n"
        'format = "pi-glink-blueprint/v1"\n'
        f'name = "{name}"\n'
        f'description = "{description or f"合成蓝图 {name}"}"\n'
        "\n"
        "[[stages]]\n"
        'id = "collect"\n'
        'title = "取证"\n'
        'goal = "按来源计划取证"\n'
        'deliverable = "report.md 与来源清单"\n'
        'checkpoint = "关键命题都有专属来源"\n'
        "\n"
        "[[stages]]\n"
        'id = "verify"\n'
        'title = "核查"\n'
        'goal = "把关键论断带回来源"\n'
        'deliverable = "「来源与核查」一节"\n'
        'checkpoint = "核查状态已判定"\n'
        "+++\n"
        "\n"
        + (body or f"正文模板：{name} 的产出契约说明。\n")
    )


def write_blueprint(directory: Path, name: str, *, text: str | None = None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.md"
    path.write_text(text if text is not None else synthetic_document(name), encoding="utf-8")
    return path


def make_config(tmp_path: Path, **overrides: str) -> HostConfig:
    values = {
        "OPENAI_API_KEY": "sk-test",
        "OPENAI_BASE_URL": "https://example.invalid/v1",
        "OPENAI_MODEL": "test/model",
        "PI_CODING_AGENT_DIR": str(REPO_ROOT / "harnesses" / "pi"),
        "LOCAL_WEB_DATA_ROOT": str(tmp_path / "data"),
        "LOCAL_WEB_PORT": "0",
        "PI_GLINK_BLUEPRINTS_DIR": str(tmp_path / "blueprints"),
    }
    values.update(overrides)
    apply_acp_pack(values, tmp_path)
    return build_config(values, REPO_ROOT)


def pi_capture_factory(captured: dict[str, Any]):
    """The pi face with its argv and the RPC prompt recorded."""

    def factory(argv, *, cwd=None, env=None, on_event=None):
        captured["argv"] = list(argv)
        client = PiRpcClient(
            [sys.executable, str(FAKE_PI), *argv[1:]],
            cwd=cwd,
            env={**(env or {}), "FAKE_PI_WRITE_REPORT": "1"},
            on_event=on_event,
        )
        original = client.request

        def request(command, **kwargs):
            if command.get("type") == "prompt":
                captured.setdefault("messages", []).append(str(command.get("message", "")))
            return original(command, **kwargs)

        client.request = request  # type: ignore[method-assign]
        return client

    return factory


def appended_system_prompt(argv: list[str]) -> str:
    assert "--append-system-prompt" in argv, argv
    return argv[argv.index("--append-system-prompt") + 1]


def acp_command(tmp_path: Path, settings: Mapping[str, Any]) -> str:
    path = tmp_path / "acp-settings.json"
    path.write_text(json.dumps(dict(settings), ensure_ascii=False), encoding="utf-8")
    return (
        f"{shlex.quote(sys.executable)} {shlex.quote(str(FAKE_ACP))}"
        f" --config {shlex.quote(str(path))}"
    )


def read_log(path: Path) -> list[Mapping[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def run_dirs(config: HostConfig) -> list[str]:
    runs = config.data_root / "runs"
    return sorted(path.name for path in runs.glob("*")) if runs.is_dir() else []


# -- the repository blueprint and the format contract -------------------------


def test_repository_blueprint_is_well_formed() -> None:
    """The built-in example satisfies every part of the frontmatter contract."""
    names = blueprint_names(BLUEPRINTS)
    assert names == ("deepresearch",), names
    blueprint = load_blueprint(BLUEPRINTS, "deepresearch")
    assert blueprint.name == "deepresearch"
    assert blueprint.description
    assert len(blueprint.stages) >= 3
    assert [stage.id for stage in blueprint.stages] == [
        "frame",
        "evidence",
        "coverage",
        "verification",
    ]
    for stage in blueprint.stages:
        assert stage.title and stage.goal and stage.deliverable and stage.checkpoint
        assert set(stage.as_record()) == {"id", "title", "goal", "deliverable", "checkpoint"}
    assert "report.md" in blueprint.body
    # The file on disk is exactly what makes it a blueprint: the stem, the
    # declared name and the API value are one identity.
    assert (BLUEPRINTS / "deepresearch.md").is_file()


def test_blueprint_keeps_the_existing_deep_research_methodology() -> None:
    """The example reuses the reviewed methodology; it invents no new claims.

    Same obligation set as ``tests/test_agent_prompts.py`` pins for
    The deleted pi prompt template: the blueprint is the structured,
    harness-agnostic spelling of that same contract.
    """
    text = (BLUEPRINTS / "deepresearch.md").read_text(encoding="utf-8")
    for anchor in (
        "report.md",
        "来源分层",
        "一个命题对一个来源",
        "区分三类内容",
        "覆盖声明",
        "未涉及",
        "自我核查",
        "来源与核查",
        "PASS",
        "BLOCKED",
        '"最优"',
        '"最稳妥"',
        "`grep` / `diff`",
    ):
        assert anchor in text, anchor
    for forbidden in ("alpha_search", "memory_remember", "Task", "verifier", "reviewer"):
        assert forbidden not in text, forbidden
    for phrase in ("Proceed with this", 'Reply "yes"', "请确认计划", "确认后再继续", "等待用户确认后"):
        assert phrase not in text, phrase
    assert re.search(r"\b(?:find|ls|glob)\b", text) is None


def test_names_are_derived_from_the_blueprint_directory(tmp_path: Path) -> None:
    """Only regular ``*.md`` files with a usable slug stem are offered."""
    directory = tmp_path / "blueprints"
    write_blueprint(directory, "zeta")
    write_blueprint(directory, "alpha")
    (directory / "notes.txt").write_text("not a blueprint", encoding="utf-8")
    (directory / "Upper.md").write_text(synthetic_document("Upper"), encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text(synthetic_document("linked"), encoding="utf-8")
    (directory / "linked.md").symlink_to(outside)
    nested = directory / "nested"
    nested.mkdir()
    write_blueprint(nested, "inner")

    assert blueprint_names(directory) == ("alpha", "zeta")
    assert blueprint_names(tmp_path / "missing") == ()


def test_a_name_outside_the_directory_is_refused(tmp_path: Path) -> None:
    """The API value is an exact stem of a derived name — never a path."""
    directory = tmp_path / "blueprints"
    write_blueprint(directory, "real")
    outside = tmp_path / "secret.md"
    outside.write_text("SECRET-MARKER-CONTENT", encoding="utf-8")
    for name in ("../secret", "secret", "/etc/passwd", "", " real", "real.md", "REAL", "nested/inner"):
        with pytest.raises(BlueprintError) as excinfo:
            load_blueprint(directory, name)
        assert excinfo.value.reason == "unknown", name


@pytest.mark.parametrize(
    ("document", "reason"),
    [
        ("no frontmatter at all\n", "missing_frontmatter"),
        ("+++\nname = \"x\"\n", "missing_frontmatter"),
        ("+++\n::: not toml :::\n+++\n\nbody\n", "invalid_frontmatter"),
        (
            '+++\nname = "x"\ndescription = "d"\nstages = [{id = "a", title = "t", goal = "g",'
            ' deliverable = "d", checkpoint = "c"}]\n+++\n\nbody\n',
            "missing_field:format",
        ),
        (
            '+++\nformat = "pi-glink-blueprint/v2"\nname = "x"\ndescription = "d"\n'
            '[[stages]]\nid = "a"\ntitle = "t"\ngoal = "g"\ndeliverable = "d"\ncheckpoint = "c"\n'
            "+++\n\nbody\n",
            "unsupported_format",
        ),
        (
            '+++\nformat = "pi-glink-blueprint/v1"\nname = "other"\ndescription = "d"\n'
            '[[stages]]\nid = "a"\ntitle = "t"\ngoal = "g"\ndeliverable = "d"\ncheckpoint = "c"\n'
            "+++\n\nbody\n",
            "name_mismatch",
        ),
        (
            '+++\nformat = "pi-glink-blueprint/v1"\nname = "x"\ndescription = "d"\n'
            "stages = []\n+++\n\nbody\n",
            "missing_field:stages",
        ),
        (
            '+++\nformat = "pi-glink-blueprint/v1"\nname = "x"\ndescription = "d"\n'
            '[[stages]]\nid = "a"\ntitle = "t"\ngoal = "g"\ncheckpoint = "c"\n'
            "+++\n\nbody\n",
            "missing_field:stage 0:deliverable",
        ),
        (
            '+++\nformat = "pi-glink-blueprint/v1"\nname = "x"\ndescription = "d"\n'
            '[[stages]]\nid = "a"\ntitle = "t"\ngoal = "g"\ndeliverable = "d"\ncheckpoint = "c"\n'
            'outcome = "typo"\n+++\n\nbody\n',
            "unknown_stage_field:outcome",
        ),
        (
            '+++\nformat = "pi-glink-blueprint/v1"\nname = "x"\ndescription = "d"\n'
            'owner = "typo"\n[[stages]]\nid = "a"\ntitle = "t"\ngoal = "g"\ndeliverable = "d"\n'
            'checkpoint = "c"\n+++\n\nbody\n',
            "unknown_field:owner",
        ),
        (
            '+++\nformat = "pi-glink-blueprint/v1"\nname = "x"\ndescription = "d"\n'
            '[[stages]]\nid = "a"\ntitle = "t"\ngoal = "g"\ndeliverable = "d"\ncheckpoint = "c"\n'
            '[[stages]]\nid = "a"\ntitle = "t2"\ngoal = "g"\ndeliverable = "d"\ncheckpoint = "c"\n'
            "+++\n\nbody\n",
            "duplicate_stage_id:a",
        ),
        (
            '+++\nformat = "pi-glink-blueprint/v1"\nname = "x"\ndescription = "d"\n'
            '[[stages]]\nid = "Bad Id"\ntitle = "t"\ngoal = "g"\ndeliverable = "d"\n'
            'checkpoint = "c"\n+++\n\nbody\n',
            "invalid_stage_id:Bad Id",
        ),
        (
            '+++\nformat = "pi-glink-blueprint/v1"\nname = "x"\ndescription = "d"\n'
            '[[stages]]\nid = "a"\ntitle = "t"\ngoal = "g"\ndeliverable = "d"\ncheckpoint = "c"\n'
            "+++\n",
            "missing_body",
        ),
    ],
)
def test_malformed_documents_are_refused(document: str, reason: str) -> None:
    """Every contract violation is a refusal, never a best-effort parse."""
    with pytest.raises(BlueprintError) as excinfo:
        parse_blueprint(document, expected_name="x")
    assert excinfo.value.reason == reason


def test_a_name_that_is_not_a_slug_is_refused() -> None:
    """The name becomes the rendered frame's attribute; it must be a slug."""
    with pytest.raises(BlueprintError) as excinfo:
        parse_blueprint(synthetic_document("x"), expected_name="Bad Name")
    assert excinfo.value.reason == "invalid_name"


def test_oversized_and_non_utf8_files_are_refused(tmp_path: Path) -> None:
    directory = tmp_path / "blueprints"
    directory.mkdir()
    big = synthetic_document("big", body="x" * (40 * 1024))
    (directory / "big.md").write_text(big, encoding="utf-8")
    (directory / "binary.md").write_bytes(b"\xff\xfe\x00\x01")
    with pytest.raises(BlueprintError) as excinfo:
        load_blueprint(directory, "big")
    assert excinfo.value.reason == "too_large"
    with pytest.raises(BlueprintError) as excinfo:
        load_blueprint(directory, "binary")
    assert excinfo.value.reason == "not_utf8"


def test_render_is_deterministic_and_carries_the_contract(tmp_path: Path) -> None:
    directory = tmp_path / "blueprints"
    write_blueprint(directory, "synthetic")
    blueprint = load_blueprint(directory, "synthetic")
    first = render_blueprint(blueprint)
    second = render_blueprint(load_blueprint(directory, "synthetic"))
    assert first == second, "同蓝图同输入必须渲染同文本"
    assert '<blueprint name="synthetic">' in first
    assert first.rstrip().endswith("</blueprint>")
    assert "合成蓝图 synthetic" in first
    for stage in blueprint.stages:
        assert stage.goal in first and stage.deliverable in first and stage.checkpoint in first
    assert blueprint.body in first
    # Purely the document: no host path, no run id, no timestamp.
    assert str(tmp_path) not in first
    assert "SENTINEL-USER-PROMPT" not in first


# -- the pi face --------------------------------------------------------------


def test_pi_blueprint_run_delivers_the_render_and_records_the_metadata(tmp_path: Path) -> None:
    write_blueprint(tmp_path / "blueprints", "synthetic")
    config = make_config(tmp_path)
    store = RunStore(config.data_root / "runs")
    captured: dict[str, Any] = {}
    manager = RunManager(config, EventBus(), store, client_factory=pi_capture_factory(captured))

    record = manager.start("普通的用户题目", blueprint="synthetic")
    final = manager.wait(record.run_id, timeout=30)

    assert final is not None and final.status == "finished"
    # The user's prompt travels byte for byte on its own channel.
    assert captured["messages"] == ["普通的用户题目"]
    rendered = render_blueprint(load_blueprint(config.blueprints_dir, "synthetic"))
    assert appended_system_prompt(captured["argv"]) == pi_system_prompt(rendered)
    assert "普通的用户题目" not in appended_system_prompt(captured["argv"])
    # The record carries the template's identity and its stage metadata, in order.
    stored = store.get(record.run_id)
    assert stored is not None
    assert stored.spec.blueprint == "synthetic"
    assert [dict(stage) for stage in stored.blueprint_stages] == [
        stage.as_record() for stage in load_blueprint(config.blueprints_dir, "synthetic").stages
    ]
    assert stored.blueprint_stages[0]["checkpoint"] == "关键命题都有专属来源"


def test_pi_blueprint_render_is_identical_across_runs(tmp_path: Path) -> None:
    write_blueprint(tmp_path / "blueprints", "synthetic")
    config = make_config(tmp_path)
    store = RunStore(config.data_root / "runs")
    first: dict[str, Any] = {}
    second: dict[str, Any] = {}
    manager_one = RunManager(config, EventBus(), store, client_factory=pi_capture_factory(first))
    manager_two = RunManager(config, EventBus(), store, client_factory=pi_capture_factory(second))
    record_one = manager_one.start("题目一", blueprint="synthetic")
    assert manager_one.wait(record_one.run_id, timeout=30).status == "finished"
    record_two = manager_two.start("题目二", blueprint="synthetic")
    assert manager_two.wait(record_two.run_id, timeout=30).status == "finished"
    assert appended_system_prompt(first["argv"]) == appended_system_prompt(second["argv"])


def test_pi_run_without_a_blueprint_is_unchanged(tmp_path: Path) -> None:
    """Zero change for a run that names no blueprint."""
    write_blueprint(tmp_path / "blueprints", "synthetic")
    config = make_config(tmp_path)
    store = RunStore(config.data_root / "runs")
    captured: dict[str, Any] = {}
    manager = RunManager(config, EventBus(), store, client_factory=pi_capture_factory(captured))

    record = manager.start("不带蓝图的题目")
    assert manager.wait(record.run_id, timeout=30).status == "finished"

    assert appended_system_prompt(captured["argv"]) == pi_system_prompt()
    assert "<blueprint" not in captured["argv"][captured["argv"].index("--append-system-prompt") + 1]
    stored = store.get(record.run_id)
    assert stored is not None and stored.spec.blueprint is None and stored.blueprint_stages == ()


def test_a_blueprint_event_stream_adds_no_product_event(tmp_path: Path) -> None:
    """A blueprint run records facts; it never becomes a new event type."""
    write_blueprint(tmp_path / "blueprints", "synthetic")
    config = make_config(tmp_path)
    store = RunStore(config.data_root / "runs")
    plain: dict[str, Any] = {}
    templated: dict[str, Any] = {}
    manager_one = RunManager(config, EventBus(), store, client_factory=pi_capture_factory(plain))
    manager_two = RunManager(config, EventBus(), store, client_factory=pi_capture_factory(templated))

    record_one = manager_one.start("不带蓝图")
    assert manager_one.wait(record_one.run_id, timeout=30).status == "finished"
    record_two = manager_two.start("带蓝图", blueprint="synthetic")
    assert manager_two.wait(record_two.run_id, timeout=30).status == "finished"

    baseline = [event["type"] for event in store.iter_events(record_one.run_id)]
    templated_types = [event["type"] for event in store.iter_events(record_two.run_id)]
    assert set(templated_types) <= set(baseline)
    vocabulary = {
        value
        for name, value in vars(events_module).items()
        if name.isupper() and isinstance(value, str)
    }
    assert set(templated_types) <= vocabulary
    offenders = [
        event["type"]
        for event in store.iter_events(record_two.run_id)
        if isinstance(event.get("payload"), Mapping)
        and ({"blueprint", "blueprintStages"} & set(event["payload"]))
    ]
    assert offenders == [], offenders


@pytest.mark.parametrize("harness", ["pi", "acp"])
def test_unknown_and_malformed_blueprints_are_refused_before_any_run(
    tmp_path: Path, harness: str
) -> None:
    """The same fail-closed rule admits runs on both harness faces."""
    write_blueprint(tmp_path / "blueprints", "synthetic")
    (tmp_path / "blueprints" / "broken.md").write_text("+++\nnot toml\n", encoding="utf-8")
    overrides: dict[str, str] = {}
    if harness == "acp":
        overrides = {
            "PI_GLINK_HARNESS": "acp",
            "PI_GLINK_ACP_COMMAND": acp_command(tmp_path, {}),
        }
    config = make_config(tmp_path, **overrides)
    store = RunStore(config.data_root / "runs")
    manager = RunManager(config, EventBus(), store, client_factory=pi_capture_factory({}))

    with pytest.raises(BlueprintUnavailable) as excinfo:
        manager.start("题目", blueprint="does-not-exist")
    assert excinfo.value.code == "unknown_blueprint"
    assert excinfo.value.available == ("broken", "synthetic")
    assert run_dirs(config) == [], "拒绝必须早于 run 目录创建"

    with pytest.raises(BlueprintUnavailable) as excinfo:
        manager.start("题目", blueprint="broken")
    assert excinfo.value.code == "invalid_blueprint"
    assert run_dirs(config) == []

    # Guard against a slug-shaped path: the name is matched, never resolved.
    with pytest.raises(BlueprintUnavailable) as excinfo:
        manager.start("题目", blueprint="../synthetic")
    assert excinfo.value.code == "unknown_blueprint"
    assert run_dirs(config) == []


def test_accepted_blueprint_names_follow_the_directory(tmp_path: Path) -> None:
    """No hardcoded list: adding or removing a file changes admission at once."""
    directory = tmp_path / "blueprints"
    config = make_config(tmp_path)
    store = RunStore(config.data_root / "runs")
    manager = RunManager(config, EventBus(), store, client_factory=pi_capture_factory({}))

    with pytest.raises(BlueprintUnavailable):
        manager.start("题目", blueprint="fresh")
    write_blueprint(directory, "fresh")
    record = manager.start("题目", blueprint="fresh")
    assert manager.wait(record.run_id, timeout=30).status == "finished"
    directory.joinpath("fresh.md").unlink()
    with pytest.raises(BlueprintUnavailable):
        manager.start("题目", blueprint="fresh")


# -- the ACP face -------------------------------------------------------------


def test_acp_blueprint_run_delivers_the_render_in_the_leading_block(tmp_path: Path) -> None:
    log = tmp_path / "agent.jsonl"
    write_blueprint(tmp_path / "blueprints", "synthetic")
    config = make_config(
        tmp_path,
        PI_GLINK_HARNESS="acp",
        PI_GLINK_ACP_COMMAND=acp_command(
            tmp_path, {"FAKE_ACP_LOG": str(log), "FAKE_ACP_WRITE_REPORT": True}
        ),
    )
    store = RunStore(config.data_root / "runs")
    manager = RunManager(config, EventBus(), store)
    record = manager.start("合成 ACP 题目", blueprint="synthetic")
    final = manager.wait(record.run_id, timeout=30)

    assert final is not None and final.status == "finished"
    rendered = render_blueprint(load_blueprint(config.blueprints_dir, "synthetic"))
    prompt = next(entry for entry in read_log(log) if entry["event"] == "prompt")
    assert prompt["text"].startswith("以下是宿主工作台注入的 system prompt")
    expected = f"<system-prompt>\n{acp_system_prompt(rendered.strip())}\n</system-prompt>"
    assert expected in prompt["text"]
    assert prompt["text"].endswith("--- 用户请求 ---\n\n合成 ACP 题目")
    stored = store.get(record.run_id)
    assert stored is not None and stored.spec.blueprint == "synthetic"
    assert len(stored.blueprint_stages) == 2


def test_acp_run_without_a_blueprint_delivers_only_the_working_agreement(tmp_path: Path) -> None:
    """ACP零变化：未指定蓝图时首块内容与旧行为逐字节一致。"""
    log = tmp_path / "agent.jsonl"
    write_blueprint(tmp_path / "blueprints", "synthetic")
    config = make_config(
        tmp_path,
        PI_GLINK_HARNESS="acp",
        PI_GLINK_ACP_COMMAND=acp_command(tmp_path, {"FAKE_ACP_LOG": str(log)}),
    )
    store = RunStore(config.data_root / "runs")
    manager = RunManager(config, EventBus(), store)
    record = manager.start("没有蓝图的 ACP 题目")
    assert manager.wait(record.run_id, timeout=30).status == "finished"

    prompt = next(entry for entry in read_log(log) if entry["event"] == "prompt")
    block = re.search(r"<system-prompt>\n(.*?)\n</system-prompt>", prompt["text"], re.DOTALL)
    assert block is not None and block.group(1) == acp_system_prompt()
    assert "<blueprint" not in prompt["text"]


# -- the HTTP API -------------------------------------------------------------


@pytest.fixture()
def server(tmp_path: Path):
    # The built-in blueprint directory: the product API is exercised against the
    # same repository content a deployment would offer.
    instance = create_server(
        make_config(tmp_path, PI_GLINK_BLUEPRINTS_DIR=str(BLUEPRINTS)),
        client_factory=pi_capture_factory({}),
    )
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        yield instance
    finally:
        instance.shutdown()
        instance.server_close()
        thread.join(timeout=5)


def request(
    server: PiGlinkServer, path: str, *, method: str = "GET", body: dict | None = None
) -> tuple[int, bytes]:
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{server.server_address[1]}{path}", data=payload, method=method
    )
    if payload is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def wait_for_terminal(server: PiGlinkServer, run_id: str, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    latest: dict = {}
    while time.monotonic() < deadline:
        status, raw = request(server, f"/api/runs/{run_id}")
        latest = json.loads(raw)["run"]
        if latest["status"] != "running":
            return latest
        time.sleep(0.05)
    return latest


def test_run_api_accepts_a_blueprint_and_records_it(server: PiGlinkServer) -> None:
    status, raw = request(
        server, "/api/runs", method="POST", body={"prompt": "带蓝图的产品题目", "blueprint": "deepresearch"}
    )
    assert status == 201
    run = json.loads(raw)["run"]
    assert run["blueprint"] == "deepresearch"
    assert [stage["id"] for stage in run["blueprintStages"]] == [
        "frame",
        "evidence",
        "coverage",
        "verification",
    ]
    assert run["prompt"] == "带蓝图的产品题目"

    # The detail view and the list view carry the same facts as the record.
    _status, raw = request(server, f"/api/runs/{run['runId']}")
    detail = json.loads(raw)["run"]
    assert detail["blueprint"] == "deepresearch"
    _status, raw = request(server, "/api/runs")
    listed = next(item for item in json.loads(raw)["runs"] if item["runId"] == run["runId"])
    assert listed["blueprint"] == "deepresearch"
    assert listed["blueprintStages"] == run["blueprintStages"]

    final = wait_for_terminal(server, run["runId"])
    assert final["status"] == "finished"
    assert final["blueprint"] == "deepresearch"
    assert final["blueprintStages"][0]["checkpoint"]


def test_run_api_without_a_blueprint_keeps_the_previous_shape(server: PiGlinkServer) -> None:
    status, raw = request(server, "/api/runs", method="POST", body={"prompt": "普通题目"})
    assert status == 201
    run = json.loads(raw)["run"]
    assert run["blueprint"] is None
    assert run["blueprintStages"] == []


def test_run_api_refuses_unknown_malformed_and_typed_blueprints(tmp_path: Path) -> None:
    directory = tmp_path / "blueprints"
    write_blueprint(directory, "synthetic")
    sentinel = "SENTINEL-FILE-CONTENT"
    (directory / "broken.md").write_text(
        synthetic_document("broken").replace("正文模板", f"正文模板 {sentinel}").replace(
            'format = "pi-glink-blueprint/v1"', 'format = "wrong/v1"'
        ),
        encoding="utf-8",
    )
    instance = create_server(make_config(tmp_path), client_factory=pi_capture_factory({}))
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        status, raw = request(
            instance, "/api/runs", method="POST", body={"prompt": "题目", "blueprint": "nope"}
        )
        assert status == 400
        body = json.loads(raw)
        assert body["error"] == "unknown_blueprint"
        assert body["available"] == ["broken", "synthetic"]
        assert "nope" not in raw.decode("utf-8"), "未知值不被回显"

        status, raw = request(
            instance, "/api/runs", method="POST", body={"prompt": "题目", "blueprint": "broken"}
        )
        assert status == 400
        assert json.loads(raw)["error"] == "invalid_blueprint"
        assert sentinel not in raw.decode("utf-8"), "模板内容不得回显"

        status, raw = request(
            instance, "/api/runs", method="POST", body={"prompt": "题目", "blueprint": 7}
        )
        assert status == 400
        assert json.loads(raw)["error"] == "invalid_request"

        assert run_dirs(instance.config) == [], "拒绝必须早于 run 目录创建"
    finally:
        instance.shutdown()
        instance.server_close()
        thread.join(timeout=5)


# -- the read-only blueprint catalogue ----------------------------------------


def test_blueprints_api_lists_usable_templates_and_matches_admission(
    server: PiGlinkServer,
) -> None:
    """The catalogue carries what admission resolved, from the same directory."""
    status, raw = request(server, "/api/blueprints")
    assert status == 200
    payload = json.loads(raw)
    names = [item["name"] for item in payload["blueprints"]]
    assert "deepresearch" in names
    assert names == sorted(names) and len(names) == len(set(names))

    listed = next(item for item in payload["blueprints"] if item["name"] == "deepresearch")
    parsed = load_blueprint(BLUEPRINTS, "deepresearch")
    assert listed["description"] == parsed.description
    assert listed["stages"] == [stage.as_record() for stage in parsed.stages]
    assert set(listed["stages"][0]) == {"id", "title", "goal", "deliverable", "checkpoint"}
    # 列表只说契约：渲染后的 system prompt 正文不下发。
    assert "<blueprint" not in raw.decode("utf-8")

    # 与准入同源：同一目录推导的 run 记录逐字段（含顺序）相同。
    status, raw = request(
        server, "/api/runs", method="POST", body={"prompt": "对照题目", "blueprint": "deepresearch"}
    )
    assert status == 201
    run = json.loads(raw)["run"]
    assert run["blueprintStages"] == listed["stages"]
    wait_for_terminal(server, run["runId"])


def test_blueprints_api_derives_from_the_directory_and_omits_unusable_candidates(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "blueprints"
    write_blueprint(directory, "alpha")
    write_blueprint(directory, "beta")
    sentinel = "SENTINEL-BLUEPRINT-BODY"
    (directory / "broken.md").write_text(
        synthetic_document("broken")
        .replace('format = "pi-glink-blueprint/v1"', 'format = "wrong/v1"')
        .replace("正文模板", f"正文模板 {sentinel}"),
        encoding="utf-8",
    )
    instance = create_server(
        make_config(tmp_path, PI_GLINK_BLUEPRINTS_DIR=str(directory)),
        client_factory=pi_capture_factory({}),
    )
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        status, raw = request(instance, "/api/blueprints")
        assert status == 200
        assert [item["name"] for item in json.loads(raw)["blueprints"]] == ["alpha", "beta"]
        assert sentinel not in raw.decode("utf-8"), "不可用候选不出现，也不回显文件内容"

        # 名字来自目录本身：文件增删立即生效，没有硬编码名单。
        write_blueprint(directory, "gamma")
        (directory / "alpha.md").unlink()
        _status, raw = request(instance, "/api/blueprints")
        assert [item["name"] for item in json.loads(raw)["blueprints"]] == ["beta", "gamma"]
    finally:
        instance.shutdown()
        instance.server_close()
        thread.join(timeout=5)


def test_blueprints_api_answers_an_empty_list_without_a_directory(tmp_path: Path) -> None:
    """没有目录是合法部署状态：200 空列表，不回显宿主路径。"""
    instance = create_server(make_config(tmp_path), client_factory=pi_capture_factory({}))
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        status, raw = request(instance, "/api/blueprints")
        assert status == 200
        assert json.loads(raw) == {"blueprints": []}
        assert str(instance.config.blueprints_dir) not in raw.decode("utf-8")
    finally:
        instance.shutdown()
        instance.server_close()
        thread.join(timeout=5)


def test_blueprints_api_has_no_write_surface(server: PiGlinkServer) -> None:
    """只读：上传/编辑没有实现，POST 落到统一的 unknown_endpoint。"""
    status, raw = request(server, "/api/blueprints", method="POST", body={"name": "x"})
    assert status == 404
    assert json.loads(raw)["error"] == "unknown_endpoint"


# -- tenant ownership ---------------------------------------------------------


def test_blueprint_metadata_stays_inside_its_tenant(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A blueprint run keeps the owner rules: one record, one tenant.

    The blueprint adds fields to the same record; it never adds a second read
    path, a second owner or an unnamed resource. A refusal creates no record for
    either tenant.
    """
    from pi_glink.kernel.execution import (  # noqa: PLC0415
        CleanupReport,
        ExecutionAdapter,
        ExecutionOutcome,
        SessionInfo,
    )

    class FakeAdapter(ExecutionAdapter):
        def __init__(self, spec, *, on_frame):
            super().__init__(spec, on_frame=on_frame)

        def start(self) -> None:
            self.spec.workspace.mkdir(parents=True, exist_ok=True)
            (self.spec.workspace / "report.md").write_text("# 合成报告\n", encoding="utf-8")

        def deliver_input(self) -> None:
            pass

        def session_info(self):
            return SessionInfo(payload={"sessionId": "fake"}, session_id="fake", session_file=None)

        def identity(self):
            return {}

        def wait(self, *, cancel, deadline):
            return ExecutionOutcome(settled=True)

        def cancel(self) -> None:
            pass

        def recover_artifacts(self, destination):
            from pi_glink.kernel.execution import ArtifactRecovery  # noqa: PLC0415

            return ArtifactRecovery()

        def close(self):
            return CleanupReport(released=True)

    monkeypatch.setattr("pi_glink.kernel.harness_pi.PiExecutionAdapter", lambda spec, **kw: FakeAdapter(spec, on_frame=kw["on_frame"]))

    secret_file = tmp_path / "gateway.key"
    secret_file.write_bytes(b"0123456789abcdef0123456789abcdef")
    secret_file.chmod(0o600)
    tenants_file = tmp_path / "tenants.json"
    tenants_file.write_text(
        json.dumps(
            {
                "tenants": {
                    "tenant-alpha": {"modelApiKey": "sk-alpha-0000000000000000"},
                    "tenant-beta": {"modelApiKey": "sk-beta-0000000000000000"},
                }
            }
        ),
        encoding="utf-8",
    )
    tenants_file.chmod(0o600)
    write_blueprint(tmp_path / "blueprints", "synthetic")
    config = make_config(
        tmp_path,
        LOCAL_WEB_EXECUTOR="docker",
        PI_GLINK_TENANCY="multitenant",
        PI_GLINK_GATEWAY_SECRET_FILE=str(secret_file),
        PI_GLINK_TENANT_CONFIG_FILE=str(tenants_file),
        PI_GLINK_TENANCY_AUDIENCE=AUDIENCE,
    )
    store = RunStore(config.data_root / "runs")
    credentials = TenantCredentialStore.from_file(tenants_file, repo_root=REPO_ROOT)
    manager = RunManager(config, EventBus(), store, credential_store=credentials)
    alpha = TenantContext(
        tenant_id="tenant-alpha",
        user_id="user-a",
        role="member",
        audience=AUDIENCE,
        expires_at=time.time() + 60,
    )
    beta = TenantContext(
        tenant_id="tenant-beta",
        user_id="user-b",
        role="member",
        audience=AUDIENCE,
        expires_at=time.time() + 60,
    )

    record = manager.start("alpha 的蓝图题目", alpha, blueprint="synthetic")
    assert manager.wait(record.run_id, timeout=30) is not None
    assert record.tenant_id == "tenant-alpha" and record.spec.blueprint == "synthetic"
    assert store.get_for(record.run_id, "tenant-alpha") is not None
    assert store.get_for(record.run_id, "tenant-beta") is None
    assert [item.run_id for item in store.list_for("tenant-beta")] == []
    assert [item.run_id for item in store.list_for("tenant-alpha")] == [record.run_id]

    before = len(store.list(limit=None))
    with pytest.raises(BlueprintUnavailable):
        manager.start("beta 的未知蓝图", beta, blueprint="nope")
    assert len(store.list(limit=None)) == before
