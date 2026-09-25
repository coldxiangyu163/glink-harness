"""Repository session blueprints: harness-agnostic session templates.

A blueprint is a reviewed repository file (``blueprints/<name>.md``) that
declares the *outcome contract* of one research session: a name, a description,
an ordered list of stages (goal, deliverable contract, checkpoint declaration)
and a body template with the methodology prose. It constrains what a session
must deliver, not how a harness executes it, so the same document serves the pi
face and the ACP family — each through its own system-prompt channel
(``docs/blueprint-templates.md``).

The host owns no blueprint content of its own: the accepted names are derived
from the directory on every admission (never hardcoded), and every read is
fail-closed — an unknown, unreadable or malformed file is refused instead of
being partially rendered. The frontmatter is TOML between ``+++`` fences, so the
accepted subset is exactly what :mod:`tomllib` reads and no YAML parser enters
the host dependency set.

This module renders a document; it never decides a run's fate. Admission turns a
:class:`BlueprintError` into the HTTP refusal and the run record carries the
stage metadata (see :mod:`pi_glink.kernel.runner`).
"""

from __future__ import annotations

import logging
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("pi_glink.blueprints")

#: The one frontmatter format this host accepts. A file must declare it, so an
#: unrelated Markdown file that happens to sit in the directory is not treated
#: as a session contract.
FORMAT = "pi-glink-blueprint/v1"

#: TOML frontmatter fence (the established TOML front-matter spelling, as
#: opposed to YAML's ``---``): the parser — not an editor heuristic — decides
#: what is valid, and the fence makes the format visible in the file itself.
FRONTMATTER_FENCE = "+++"

#: Bounded source document. The rendered text is injected into a system prompt
#: that travels through argv (pi) or a sandbox command string (E2B), so an
#: unbounded file is refused instead of being passed on.
MAX_BLUEPRINT_BYTES = 32 * 1024

#: A blueprint name (the file stem, which is also the API value and the run
#: record's identifier) is a lowercase slug. Derivation and validation share
#: the pattern, so a file whose name cannot be used never appears as accepted.
NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

#: Top-level keys the format defines. Unknown keys are refused: a typo in a
#: stage contract must fail admission, not silently drop that contract.
_FRONTMATTER_KEYS = frozenset({"format", "name", "description", "stages"})
_STAGE_KEYS = frozenset({"id", "title", "goal", "deliverable", "checkpoint"})


class BlueprintError(ValueError):
    """A blueprint file is not usable (unknown, unreadable or malformed).

    ``reason`` is a short machine-readable token — ``unknown`` for a name that
    is not in the directory, one of the validation failures otherwise. It never
    carries file content, so it stays safe to log; the HTTP layer reports only
    the derived name list (see
    :class:`~pi_glink.kernel.runner.BlueprintUnavailable`).
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"blueprint refused: {reason}")


@dataclass(frozen=True, slots=True)
class Stage:
    """One blueprint stage: its goal, its deliverable contract, its checkpoint."""

    id: str
    title: str
    goal: str
    deliverable: str
    checkpoint: str

    def as_record(self) -> dict[str, str]:
        """The stage metadata the run record carries (never the rendered text)."""
        return {
            "id": self.id,
            "title": self.title,
            "goal": self.goal,
            "deliverable": self.deliverable,
            "checkpoint": self.checkpoint,
        }


@dataclass(frozen=True, slots=True)
class Blueprint:
    """One parsed blueprint document."""

    name: str
    description: str
    stages: tuple[Stage, ...]
    body: str


def blueprint_names(directory: Path | str) -> tuple[str, ...]:
    """The blueprint names ``directory`` offers, derived from its files.

    Only regular ``*.md`` files with a usable slug stem count; symlinks are
    skipped for the same reason the agent template skips them — the accepted set
    must be repository content, not a path into the operator's filesystem. A
    missing directory yields no names (fail-closed), never an error at startup.
    """
    base = Path(directory)
    if not base.is_dir():
        return ()
    return tuple(
        sorted(
            path.stem
            for path in base.glob("*.md")
            if path.is_file()
            and not path.is_symlink()
            and NAME_PATTERN.fullmatch(path.stem)
        )
    )


def load_blueprint(directory: Path | str, name: str) -> Blueprint:
    """Parse the blueprint ``name`` out of ``directory``; never a partial read.

    The name must be one of :func:`blueprint_names` (an exact stem), so the file
    that is opened is always a reviewed document inside the directory: a caller
    cannot address another file with ``..`` or a path separator.
    """
    base = Path(directory)
    if not isinstance(name, str) or name not in blueprint_names(base):
        raise BlueprintError("unknown")
    try:
        data = (base / f"{name}.md").read_bytes()
    except OSError as exc:
        raise BlueprintError("unreadable") from exc
    if len(data) > MAX_BLUEPRINT_BYTES:
        raise BlueprintError("too_large")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BlueprintError("not_utf8") from exc
    return parse_blueprint(text, expected_name=name)


def parse_blueprint(text: str, *, expected_name: str) -> Blueprint:
    """Validate one document and return its parsed form.

    Every failure is a :class:`BlueprintError` with a specific reason; there is
    no "best effort" parse. ``expected_name`` is the file's stem: the declared
    ``name`` must equal it so the identifier written into a run record and the
    file it came from can never disagree.
    """
    frontmatter, body = _split_document(text)
    try:
        data = tomllib.loads(frontmatter)
    except tomllib.TOMLDecodeError as exc:
        raise BlueprintError("invalid_frontmatter") from exc
    if not isinstance(data, dict):
        raise BlueprintError("invalid_frontmatter")

    unknown = sorted(set(data) - _FRONTMATTER_KEYS)
    if unknown:
        raise BlueprintError(f"unknown_field:{unknown[0]}")
    declared_format = _text_field(data, "format")
    if declared_format != FORMAT:
        raise BlueprintError("unsupported_format")
    if _text_field(data, "name") != expected_name:
        raise BlueprintError("name_mismatch")
    description = _text_field(data, "description")

    raw_stages = data.get("stages")
    if not isinstance(raw_stages, list) or not raw_stages:
        raise BlueprintError("missing_field:stages")
    stages: list[Stage] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_stages):
        if not isinstance(item, dict):
            raise BlueprintError(f"invalid_stage:{index}")
        extra = sorted(set(item) - _STAGE_KEYS)
        if extra:
            raise BlueprintError(f"unknown_stage_field:{extra[0]}")
        stage = Stage(
            id=_text_field(item, "id", prefix=f"stage {index}"),
            title=_text_field(item, "title", prefix=f"stage {index}"),
            goal=_text_field(item, "goal", prefix=f"stage {index}"),
            deliverable=_text_field(item, "deliverable", prefix=f"stage {index}"),
            checkpoint=_text_field(item, "checkpoint", prefix=f"stage {index}"),
        )
        if not NAME_PATTERN.fullmatch(stage.id):
            raise BlueprintError(f"invalid_stage_id:{stage.id}")
        if stage.id in seen:
            raise BlueprintError(f"duplicate_stage_id:{stage.id}")
        seen.add(stage.id)
        stages.append(stage)

    if not body:
        raise BlueprintError("missing_body")
    return Blueprint(
        name=expected_name,
        description=description,
        stages=tuple(stages),
        body=body,
    )


def render_blueprint(blueprint: Blueprint) -> str:
    """The exact text a run's system-prompt channel carries for this blueprint.

    Deterministic by construction: the output is a pure function of the parsed
    document — no run id, timestamp, host path or credential is part of it — so
    the same blueprint always renders byte-identical text. The frame marks the
    text as the host's working contract, and the user's own request is never
    included here: it travels through its own channel and this text never
    replaces it.
    """
    lines = [
        f'<blueprint name="{blueprint.name}">',
        "以下是本次会话的蓝图（宿主注入的产出契约，不是用户请求）：它给出流程骨架与每阶段的"
        "产出契约，不限制你自行决定执行方法；宿主只声明检查点，不代替你推进阶段。",
        "",
        blueprint.description,
        "",
        "## 阶段与产出契约",
    ]
    for index, stage in enumerate(blueprint.stages, start=1):
        lines.extend(
            [
                "",
                f"### {index}. {stage.title}（id: {stage.id}）",
                f"- 目标：{stage.goal}",
                f"- 产出契约：{stage.deliverable}",
                f"- 检查点：{stage.checkpoint}",
            ]
        )
    lines.extend(["", "## 执行模板", "", blueprint.body, "", "</blueprint>", ""])
    return "\n".join(lines)


def _split_document(text: str) -> tuple[str, str]:
    """``(frontmatter, body)`` for a ``+++``-fenced document.

    The fence lines may carry trailing spaces; everything between the first two
    fence lines is the TOML frontmatter and everything after the closing fence
    is the body, stripped (a document is never rendered with a leading/trailing
    blank page).
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != FRONTMATTER_FENCE:
        raise BlueprintError("missing_frontmatter")
    closing = next(
        (index for index in range(1, len(lines)) if lines[index].strip() == FRONTMATTER_FENCE),
        None,
    )
    if closing is None:
        raise BlueprintError("missing_frontmatter")
    return "\n".join(lines[1:closing]), "\n".join(lines[closing + 1 :]).strip()


def _text_field(data: dict, key: str, *, prefix: str = "") -> str:
    """A required non-empty string field, or a refusal naming it."""
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        label = f"{prefix}:{key}" if prefix else key
        raise BlueprintError(f"missing_field:{label}")
    return value.strip()
