"""Boundary and gate regressions for the run read model.

:mod:`glink_harness.runs.read_model` is the one place the run detail, child
documents, child artifact download, report download and event stream take the
same four steps: owner gate → path resolution → read bound → redaction. These
tests pin both the coupling (every read endpoint goes through it and no route
re-opens the store gate or an ``ArtifactTree`` on its own) and the semantics
(unknown/foreign/ownerless are the same miss; redaction uses the record's own
credentials; only ``children/<n>/`` regular files are served).
"""

from __future__ import annotations

import ast
import time
from pathlib import Path

from glink_harness.redaction import SecretRedactor
from glink_harness.runs import read_model
from glink_harness.store import RunSpec, RunStore
from glink_harness.tenancy import TenantContext

from store_helpers import finish

ROOT = Path(__file__).resolve().parents[1]
SPEC = RunSpec(family="pi", pack="pi", executor="inproc")

#: Every run-scoped read face of the HTTP surface, with the read-model calls it
#: must make. A new read endpoint added here (or a route that stops going
#: through the model) fails the coupling test below.
READ_ENDPOINTS: dict[tuple[str, str], dict[str, set[str]]] = {
    ("host/glink_harness/web_routes.py", "RoutesMixin"): {
        "_send_run": {"visible_record"},
        "_send_children": {"visible_record", "read_child_documents"},
        "_send_child_file": {"visible_record", "read_child_file"},
        "_send_report": {"visible_record", "read_report"},
        "_report_available": {"report_available"},
    },
    ("host/glink_harness/web_sse.py", "SseMixin"): {
        "_stream_run_events_authorized": {"visible_record"},
    },
}

#: None of these belongs in a route: the artifact read, the owner gate and the
#: report-path rule all live in the read model.
_ROUTE_FORBIDDEN_TOKENS = frozenset(
    {"ArtifactTree", "get_for", "report_location", "read_children"}
)


def _module_tree(relative: str) -> ast.Module:
    return ast.parse((ROOT / relative).read_text(encoding="utf-8"), filename=relative)


def _methods(tree: ast.Module, class_name: str) -> dict[str, ast.FunctionDef]:
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return {child.name: child for child in node.body if isinstance(child, ast.FunctionDef)}
    raise AssertionError(f"{class_name} 不在模块中")


def _read_model_calls(method: ast.FunctionDef) -> set[str]:
    calls = set()
    for node in ast.walk(method):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "read_model"
        ):
            calls.add(node.attr)
    return calls


def test_every_read_endpoint_goes_through_the_read_model() -> None:
    """读端点必须经 read_model，而不是各自内联门禁：新增端点漏步即变红。"""
    for (relative, class_name), endpoints in READ_ENDPOINTS.items():
        methods = _methods(_module_tree(relative), class_name)
        for method_name, required in endpoints.items():
            assert method_name in methods, f"{relative} 缺少 {class_name}.{method_name}"
            missing = required - _read_model_calls(methods[method_name])
            assert not missing, (
                f"{relative}::{class_name}.{method_name} 未经 read_model: {sorted(missing)}"
            )


def test_no_route_reopens_the_store_gate_or_an_artifact_tree() -> None:
    """反例保护：读路由不得绕过 read_model 直查 store 或直开 ArtifactTree。"""
    for relative in {rel for rel, _ in READ_ENDPOINTS}:
        tree = _module_tree(relative)
        for name in _ROUTE_FORBIDDEN_TOKENS:
            found = [node for node in ast.walk(tree) if _token_matches(node, name)]
            assert not found, f"{relative} 不应再出现 {name}（应由 read_model 统一）"


def _token_matches(node: ast.AST, name: str) -> bool:
    if isinstance(node, ast.Attribute):
        return node.attr == name
    if isinstance(node, ast.Name):
        return node.id == name
    return False


def _context(tenant_id: str, user_id: str = "user-a") -> TenantContext:
    return TenantContext(
        tenant_id=tenant_id,
        user_id=user_id,
        role="member",
        audience="glink-harness-test",
        expires_at=time.time() + 120,
    )


def test_visible_record_is_the_same_miss_for_unknown_foreign_and_ownerless(
    tmp_path: Path,
) -> None:
    """异租户 / ownerless / 未知 run 都返回 ``None``，调用方因此答案是同一 404。"""
    store = RunStore(tmp_path / "runs")
    store.create(
        "alpha", run_id="alpha1", workspace=tmp_path / "ws-a",
        tenant_id="alpha", creator_user_id="user-a", spec=SPEC,
    )
    store.create("legacy", run_id="legacy1", workspace=tmp_path / "ws-l", spec=SPEC)

    alpha = _context("alpha")
    beta = _context("beta", "user-b")
    assert read_model.visible_record(store, "alpha1", alpha).run_id == "alpha1"
    assert read_model.visible_record(store, "alpha1", beta) is None
    assert read_model.visible_record(store, "legacy1", alpha) is None
    assert read_model.visible_record(store, "nosuchrun", alpha) is None
    # 本地单用户面直接读，ownerless 记录正是它的历史数据。
    assert read_model.visible_record(store, "legacy1", None).run_id == "legacy1"


def test_report_read_uses_the_records_own_redaction_boundary(tmp_path: Path) -> None:
    """报告只从 host 产物副本读，并且按记录自己的凭据脱敏。"""
    store = RunStore(tmp_path / "runs")
    secret = "sk-live-0123456789abcdef"
    store.create(
        "报告", run_id="report1", workspace=tmp_path / "ws",
        tenant_id="alpha", creator_user_id="user-a", spec=SPEC,
    )
    artifact = store.report_artifact_path("report1")
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(f"# 报告\n\nkey={secret}\n", encoding="utf-8")
    record = finish(store, "report1", "finished", report_path=str(artifact), finished_at=time.time())

    seen: list[str] = []

    def redactor_for(record) -> SecretRedactor:
        seen.append(record.run_id)
        return SecretRedactor([secret])

    assert read_model.report_available(store, record) is True
    served = read_model.read_report(store, record, redactor_for)
    assert served is not None
    assert secret.encode("utf-8") not in served.content
    assert b"[REDACTED]" in served.content
    assert served.redacted is True
    assert served.content_type.startswith("text/markdown")
    assert seen == ["report1"], "脱敏必须用记录自己的边界"

    # report_path 指向 run 目录之外时不是可下载能力：location 规则直接拒绝。
    outside = tmp_path / "outside.md"
    outside.write_text("# 外部\n", encoding="utf-8")
    store.create(
        "外链", run_id="external1", workspace=tmp_path / "ws-e",
        tenant_id="alpha", creator_user_id="user-a", spec=SPEC,
    )
    external = finish(store, "external1", "finished", report_path=str(outside), finished_at=time.time())
    assert read_model.report_available(store, external) is False
    assert read_model.read_report(store, external, redactor_for) is None


def test_child_file_serves_only_regular_files_inside_children(tmp_path: Path) -> None:
    """只有 ``children/<n>/`` 内的普通文件被服务；越界与缺失都是同一个 miss。"""
    store = RunStore(tmp_path / "runs")
    store.create("子代理", run_id="child1", workspace=tmp_path / "ws", spec=SPEC)
    record = store.get("child1")
    assert record is not None
    children = store.run_dir("child1") / "children"
    (children / "0").mkdir(parents=True, exist_ok=True)
    (children / "0" / "output.md").write_text("子代理正文", encoding="utf-8")
    (children / "meta.json").write_text("{}", encoding="utf-8")

    def redactor_for(_record) -> SecretRedactor:
        return SecretRedactor()

    served = read_model.read_child_file(store, record, "0", "output.md", redactor_for)
    assert served is not None
    assert served.content == "子代理正文".encode("utf-8")
    assert served.content_type.startswith("text/markdown")
    assert served.redacted is False
    # 路径解析不是能力：父目录、别的目录、缺文件都拒绝。
    assert read_model.read_child_file(store, record, "0", "../meta.json", redactor_for) is None
    assert read_model.read_child_file(store, record, "9", "output.md", redactor_for) is None
    assert read_model.read_child_file(store, record, "0", "absent.md", redactor_for) is None
