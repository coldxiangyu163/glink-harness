"""The run's secret boundary: redactor, egress token, sweep roots, credential copies.

One :class:`RunSecretBoundary` exists per admitted run. It is built at
admission from the host's known secrets plus this run's own credentials, so a
tenant's secret never redacts (or leaks into) another tenant's output.

It has two faces, used by different stages:

* **The redaction view** (:meth:`redact`, :meth:`redact_payload`,
  :meth:`learn`, :meth:`hold_grant`) — every frame, log line and record field
  derived from the run crosses it. Prepare adds the secrets it rendered (the
  egress token, ACP ``runtime.secrets``); Recover adds values it only learned
  from recovered material.
* **The seal** (:meth:`seal_trees`, :meth:`seal_sessions`, :meth:`sweep_tree`,
  :meth:`sanitize_session_file`, :meth:`absorb`,
  :meth:`remove_credential_copies`) — rewriting or deleting the files the run
  left behind. Only the Seal stage calls these, after the face is closed and
  before the report is published; the sweep roots (workspace, the run's
  subagent mirror, the runtime's declared session/state roots and their
  injected credential copies) are bound to the boundary as the run learns them.

The egress grant is memory-only: the pipeline releases it when the verdict
commit has been attempted, whatever its outcome.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from ..artifacts import ArtifactError, ArtifactTree
from ..kernel.agent_env import RunCredentials
from ..kernel.harness import HarnessRuntime
from ..kernel.sweep import SessionSweep
from ..redaction import SecretRedactor, credential_secrets_from_text
from . import limits
from .state import LOGGER_NAME

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..egress import EgressGrant, EgressProxy

logger = logging.getLogger(LOGGER_NAME)


@dataclass(frozen=True, slots=True)
class SealFacts:
    """What one seal step did: whether bytes changed, and what it could not do."""

    redacted: bool = False
    failures: tuple[str, ...] = ()


class RunSecretBoundary:
    """One run's secrets and the places they may have been written to."""

    def __init__(self, redactor: SecretRedactor, *, run_id: str = "") -> None:
        self.redactor = redactor
        self.run_id = run_id
        self._grant: EgressGrant | None = None
        self._trees: tuple[tuple[str, Path], ...] = ()
        self._sessions: tuple[SessionSweep, ...] = ()

    @classmethod
    def for_credentials(
        cls, host: SecretRedactor, credentials: RunCredentials, *, run_id: str = ""
    ) -> "RunSecretBoundary":
        """Host secrets + this run's credentials (nothing from another tenant)."""
        redactor = SecretRedactor(host.known_values)
        redactor.add(credentials.model_api_key)
        redactor.add(credentials.web_search_key)
        return cls(redactor, run_id=run_id)

    # -- the redaction view (every stage) --------------------------------------

    @property
    def known_values(self) -> Any:
        return self.redactor.known_values

    def learn(self, value: str | None) -> None:
        """Add one newly learned secret shape to this run's boundary only."""
        self.redactor.add(value)

    def redact(self, text: str) -> str:
        return self.redactor.redact(text)

    def redact_payload(self, payload: Mapping[str, Any]) -> Any:
        return self.redactor.redact_payload(dict(payload))

    @property
    def egress_grant(self) -> "EgressGrant | None":
        return self._grant

    def hold_grant(self, grant: "EgressGrant") -> None:
        """Keep the run's egress grant; its token joins the boundary at once."""
        self._grant = grant
        self.learn(grant.token)

    def release_grant(self, egress: "EgressProxy") -> None:
        """Revoke the grant (idempotent). The token stays known to the redactor."""
        grant, self._grant = self._grant, None
        if grant is not None:
            egress.revoke(grant.token)

    # -- sweep roots (bound as the run learns them) ----------------------------

    @property
    def has_trees(self) -> bool:
        return bool(self._trees)

    def bind_trees(self, *trees: tuple[str, Path]) -> None:
        """Bind the run's rewritable trees: ``(label, root)`` in sweep order."""
        self._trees = tuple(trees)

    def bind_runtime(self, runtime: HarnessRuntime | None) -> None:
        """Bind the session/state roots the rendered runtime declares."""
        self._sessions = tuple(runtime.session_sweeps()) if runtime is not None else ()

    # -- the seal (Seal stage only) --------------------------------------------

    def seal_trees(self) -> SealFacts:
        """Rewrite every bound tree (workspace, subagent mirror), each guarded."""
        changed = False
        for label, root in self._trees:
            try:
                if root.is_dir() and self.sweep_tree(root):
                    changed = True
            except Exception:  # noqa: BLE001 - sweep is defence in depth
                logger.exception("%s credential sweep failed run=%s", label, self.run_id)
        return SealFacts(redacted=changed)

    def seal_sessions(self, session_file: str | None) -> SealFacts:
        """Sweep every session/state root the runtime declared.

        哪些路径需要收尾扫描是运行时自己的事实（``session_sweeps()``）：pi 的
        会话日志在宿主的 session 目录里，可以重写；ACP 的状态根是 agent 自己
        的会话存储，只读扫描并把凭据形状值并入本 run 边界（不改写文件，见
        ``docs/acp-integration-notes.md`` §10.2/§14.1）。边界只消费契约，
        不询问具体 runtime 类型的属性。
        """
        changed = False
        failures: list[str] = []
        for target in self._sessions:
            if target.carries_session_file and session_file:
                if self.sanitize_session_file(session_file, root=target.root):
                    changed = True
            if self._scan_sweep_root(target):
                changed = True
            if target.credential_copies:
                # 删除必须在只读扫描之后：先让本 run 的边界记住这些值，再从
                # 盘上拿掉副本（否则删除后就没有值可学）。
                failures.extend(
                    self.remove_credential_copies(target.root, target.credential_copies)
                )
        return SealFacts(redacted=changed, failures=tuple(failures))

    def _scan_sweep_root(self, target: SessionSweep) -> bool:
        """Scan one declared root: rewrite it, or only learn its values."""
        if target.rewrite:
            return self.sweep_tree(target.root, max_bytes=limits.REPORT_MAX_BYTES)
        return self.absorb(target.root)

    def sanitize_session_file(self, session_file: str, *, root: Path) -> bool:
        """Redact one session file below ``root``; returns whether its bytes changed."""
        try:
            with ArtifactTree(root) as tree:
                original = tree.read(
                    session_file, max_bytes=limits.REPORT_MAX_BYTES
                ).decode("utf-8", errors="replace")
                redacted = self.redact(original)
                if redacted == original:
                    return False
                tree.replace(
                    session_file, redacted.encode("utf-8"), max_bytes=limits.REPORT_MAX_BYTES
                )
        except (ArtifactError, OSError) as exc:
            logger.warning("会话文件未通过安全校验或无法改写，已跳过: %s (%s)", session_file, exc)
            return False
        logger.warning("session file contained credentials and was redacted: %s", session_file)
        return True

    def absorb(
        self,
        root: Path,
        *,
        max_files: int | None = None,
        max_bytes: int | None = None,
    ) -> bool:
        """Learn credentials from a state tree the host must not rewrite.

        ACP 状态根是 agent 自己的会话存储：改写会破坏追问要续接的会话，所以
        这里只读（同一套目录句柄与大小/数量上限），把文本里发现的凭据形状值
        并入本 run 的脱敏边界——与回收/继承时同一规则——文件字节一个不动。
        返回是否学到了新值（从而本 run 的后续产品输出会被多遮蔽一层）。
        """
        max_files = limits.SWEEP_MAX_FILES if max_files is None else max_files
        max_bytes = limits.REPORT_MAX_BYTES if max_bytes is None else max_bytes
        try:
            tree = ArtifactTree(root)
        except ArtifactError:
            return False
        changed = False
        with tree:
            for relative in tree.files(max_files=max_files):
                try:
                    raw = tree.read(relative, max_bytes=max_bytes)
                except ArtifactError:
                    continue
                if b"\x00" in raw[:1024]:  # binary-ish
                    continue
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                known = self.redactor.known_values
                for value in credential_secrets_from_text(text):
                    self.learn(value)
                if self.redactor.known_values != known:
                    changed = True
        return changed

    def remove_credential_copies(
        self, root: Path, relatives: tuple[str, ...]
    ) -> tuple[str, ...]:
        """Delete the host-injected credential copies from one state root.

        run 收尾在只读扫描之后删除这些**已知相对路径**的副本（docs/adr/0003）
        ——追问由 ``prepare_acp_runtime`` 从源重新注入、
        继承已 skip ``credential_dests``，副本收尾后没有用途。只删列出的路径本身：
        不做模式匹配、不跟随链接（链接或越界路径只记失败，不碰目标）、不删目录。
        删除失败作为收尾事实返回并告警，**不改变 run 终态**。
        """
        try:
            tree = ArtifactTree(root)
        except ArtifactError:
            # 该状态根不存在（例如 inproc 面没有 recovered）：没有副本可删。
            return ()
        failures: list[str] = []
        with tree:
            for relative in relatives:
                try:
                    removed = tree.remove(relative)
                except (ArtifactError, OSError) as exc:
                    detail = (
                        f"注入凭据副本删除失败: {relative}: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    logger.warning("run=%s %s", self.run_id, detail)
                    failures.append(detail)
                    continue
                if removed:
                    logger.info(
                        "removed injected credential copy run=%s path=%s/%s",
                        self.run_id,
                        root,
                        relative,
                    )
        return tuple(failures)

    def sweep_tree(
        self,
        workspace: Path,
        *,
        max_files: int | None = None,
        max_bytes: int | None = None,
    ) -> bool:
        """Redact credentials in every text artifact below ``workspace``.

        The report is not the only file a tool echo can reach; a ``write`` call
        can leak the same value into any artifact the operator later opens. The
        sweep is bounded (file count, size cap, binary skip) and walks the tree
        through directory handles: links are never followed for reading, and
        every rewrite is published inside the verified directory.
        ``max_bytes`` is raised for the session directory, whose files are
        legitimately larger than workspace artifacts.
        """
        max_files = limits.SWEEP_MAX_FILES if max_files is None else max_files
        max_bytes = limits.SWEEP_MAX_BYTES if max_bytes is None else max_bytes
        try:
            tree = ArtifactTree(workspace)
        except ArtifactError:
            return False
        changed = False
        with tree:
            for relative in tree.files(max_files=max_files):
                try:
                    raw = tree.read(relative, max_bytes=max_bytes)
                except ArtifactError:
                    continue
                if b"\x00" in raw[:1024]:  # binary-ish
                    continue
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                redacted = self.redact(text)
                if redacted == text:
                    continue
                try:
                    tree.replace(relative, redacted.encode("utf-8"), max_bytes=max_bytes)
                except (ArtifactError, OSError) as exc:
                    logger.error("failed to redact artifact: %s (%s)", relative, exc)
                    continue
                changed = True
                logger.warning("artifact contained credentials and was redacted: %s", relative)
        return changed
