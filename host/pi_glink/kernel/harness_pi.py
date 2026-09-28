"""The pi harness family.

pi 族的策略面：宿主模型凭据准入、会话文件种子、
``AgentRuntime`` 渲染与 ``Projector``、pi 进程的 :class:`Launch` 描述与
RPC 驱动（``PiExecutionAdapter``，与 sandbox 正交）、
pi stop 词汇归一化，以及 pi-subagents 的子代理增强与进度配对。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Mapping, TYPE_CHECKING

from ..artifacts import ArtifactError, ArtifactTree
from ..config import ConfigError
from ..store import (
    CHILDREN_DIRNAME,
    FAILED,
    FINISHED,
    SUBAGENTS_DIRNAME,
    RunRecord,
)
from .agent_env import (
    IMAGE_PACKAGE_GUARD,
    AgentRuntime,
    prepare_agent_dir,
)
from .docker_env import worker_image
from .execution import ExecutionAdapter, ExecutionSpec, PiExecutionAdapter
from .harness import (
    FollowupUnavailable,
    HarnessFamily,
    PreparedRuntime,
    Verdict,
)
from .projection import Projector
from .sandbox import Launch, Mount, Sandbox
from .subagent_artifacts import (
    associate_children,
    parse_artifact_filename,
    read_meta_enrichment,
)

if TYPE_CHECKING:  # pragma: no cover - annotation only
    from ..redaction import SecretRedactor
    from .agent_env import RunCredentials

logger = logging.getLogger("pi_glink.kernel.harness_pi")

#: per-run pi 配置目录名，以及宿主读回产物（含会话种子）的统一上限。
RUNTIME_DIRNAME = "pi-agent"
REPORT_MAX_BYTES = 32 * 1024 * 1024

#: Bounds for the children enrichment copy (pi-subagents artifacts →
#: ``children/<n>/``): the sources are already bounded at recovery, the copy
#: gets its own ceiling so a pathological layout stays cheap.
_ENRICH_VISIT_LIMIT = 256
_ENRICH_FILE_MAX_BYTES = 8 * 1024 * 1024
_ENRICH_TOTAL_MAX_BYTES = 64 * 1024 * 1024
_USAGE_FIELDS = ("input", "output", "cacheRead", "cacheWrite", "totalTokens")


class PiHarnessFamily(HarnessFamily):
    """pi 族：宿主渲染配置、宿主凭据准入、会话文件续接。"""

    name = "pi"

    # -- (a) 准入 --------------------------------------------------------------

    def validate_credentials(self, credentials: "RunCredentials") -> None:
        # The ACP family does not use the host's model credential: the agent
        # brings its own model configuration (injection points are M2 work).
        if not credentials.model_api_key:
            raise ConfigError(
                "OPENAI_API_KEY is not configured; set it in .env before starting a run"
            )

    # -- (b) 追问门禁与续接种子 -------------------------------------------------

    def resume_seed(
        self, parent: RunRecord, *, redactor: "SecretRedactor"
    ) -> bytes | None:
        # The recorded path is not a capability. It must be a direct file in
        # that run's own session directory; ArtifactTree refuses links.
        session_dir = Path(self._config.session_dir) / parent.run_id
        path = Path(parent.session_file or "")
        if path.parent != session_dir or path.suffix != ".jsonl":
            raise FollowupUnavailable("session_unavailable")
        try:
            with ArtifactTree(session_dir) as tree:
                seed = tree.read(path.name, max_bytes=REPORT_MAX_BYTES)
        except (ArtifactError, OSError) as exc:
            raise FollowupUnavailable("session_unavailable") from exc
        if not seed.strip() or not parent.session_id:
            raise FollowupUnavailable("session_unavailable")
        try:
            # Historical files may predate the latest redaction rules. Never
            # hand an unchecked copy to the next kernel.
            seed = redactor.redact(seed.decode("utf-8")).encode("utf-8")
        except UnicodeError as exc:
            raise FollowupUnavailable("session_unavailable") from exc
        return seed

    def resume_value(self, parent: RunRecord | None) -> str | None:
        # pi resumes from the sanitized session file copied into the run's
        # session directory.
        return "continued.jsonl" if parent else None

    def verifies_continuation_before_turn(self) -> bool:
        # 恒 True：每个 sandbox 同规则，都走 RPC 通道，``get_state`` 在
        # ``session_ready`` 即知会话身份（pi 以 ``--session continued.jsonl``
        # 启动，续接会话原地打开、保持原 session id；docs/adr/0004）。
        return True

    # -- (c) runtime 渲染与 projector 工厂 --------------------------------------

    def prepare_runtime(
        self,
        run_id: str,
        run_dir: Path,
        *,
        parent: RunRecord | None,
        session_seed: bytes | None,
        credentials: "RunCredentials | None",
        sandbox: Sandbox,
    ) -> PreparedRuntime:
        runtime = prepare_agent_dir(
            self._config,
            runtime_dir=run_dir / RUNTIME_DIRNAME,
            session_dir=Path(self._config.session_dir) / run_id,
            credentials=credentials,
            # An isolated sandbox runs pi from its image: settings.json names
            # the image-local prebaked packages (the guard verifies them).
            image_packages=sandbox.isolated,
        )
        seed_consumed = False
        if session_seed is not None:
            with ArtifactTree(runtime.session_dir) as tree:
                tree.replace(
                    "continued.jsonl", session_seed, max_bytes=REPORT_MAX_BYTES
                )
            seed_consumed = True
        # This run's own redaction boundary: the host's secrets plus the
        # credentials this run was actually given. The rendered search
        # credential only exists in the file, so it is learned here (never
        # logged, never returned). Adding it to the run's boundary — not to the
        # host-level one — keeps two tenants' secrets from mixing.
        return PreparedRuntime(
            runtime=runtime,
            secret_files=(runtime.directory / "web-search.json",),
            seed_consumed=seed_consumed,
        )

    def make_projector(self, redactor: "SecretRedactor") -> Projector:
        return Projector(redactor)

    # -- (d) launch 描述与协议驱动 ---------------------------------------------

    def launch(self, spec: ExecutionSpec, sandbox: Sandbox) -> Launch:
        """pi in RPC mode, with its config and session directories placed.

        ``argv[0]`` is the host's ``config.pi_bin`` in-process and the image's
        ``pi`` on an isolated sandbox (a host path means nothing there), where
        the image's package guard runs first. ``spec.extra_argv`` already holds
        ``--name pi-glink-<run_id>`` and the optional ``--append-system-prompt``
        (the runner injects them identically on every sandbox). The
        continuation is ``--session <sessions>/continued.jsonl`` in the
        sandbox's own view — the upload/mount location of the seeded copy. The
        prompt itself travels as an RPC frame on stdin, never in argv.

        The environment is credential-free: the model credential is in the
        rendered ``auth.json`` (pi's own store), so ``env`` inside the kernel
        cannot print it.
        """
        runtime: AgentRuntime = spec.runtime
        config_dir = sandbox.place("config", runtime.directory)
        sessions_dir = sandbox.place("sessions", runtime.session_dir)
        argv = [
            "pi" if sandbox.isolated else spec.config.pi_bin,
            "--mode", "rpc",
            "--provider", runtime.provider_id,
            "--model", runtime.model_id,
            *spec.extra_argv,
        ]
        if spec.resume_session:
            argv.extend(["--session", f"{sessions_dir}/{spec.resume_session}"])
        env = {
            "PI_CODING_AGENT_DIR": config_dir,
            "PI_CODING_AGENT_SESSION_DIR": sessions_dir,
            "PI_SKIP_VERSION_CHECK": "1",
            "PI_TELEMETRY": "0",
        }
        return Launch(
            argv=tuple(argv),
            env=env,
            mounts=(
                Mount(runtime.directory, config_dir),
                # The run's own session directory: a shared mount would let two
                # runs write into each other's session files.
                Mount(runtime.session_dir, sessions_dir),
            ),
            guard=IMAGE_PACKAGE_GUARD if sandbox.isolated else None,
            image=worker_image(spec.config),
            template=spec.config.e2b_template,
            # A remote sandbox is created with the same credential-free
            # environment its kernel command gets.
            create_env=env,
        )

    def driver(
        self,
        spec: ExecutionSpec,
        *,
        sandbox: Sandbox,
        launch: Launch,
        on_frame: Callable[[Mapping[str, Any]], None],
        client_factory: Callable[..., Any] | None,
    ) -> ExecutionAdapter:
        return PiExecutionAdapter(
            spec,
            sandbox=sandbox,
            launch=launch,
            client_factory=client_factory,
            on_frame=on_frame,
        )

    # -- (e) 终态词汇归一化 ------------------------------------------------------

    def model_stop_verdict(
        self, stop: str | None, *, model_error: str | None
    ) -> Verdict | None:
        if stop == "error":
            return Verdict(FAILED, "model_error", model_error or "模型返回了错误结果")
        if stop == "aborted":
            return Verdict(FAILED, "aborted", model_error or "模型输出被中断")
        if stop == "length":
            # The execution succeeded, but the answer was cut off at the output
            # limit. That is a delivery fact the record must carry.
            return Verdict(FINISHED, "length", None)
        return None

    # -- (f) 子代理增强与运行中事实 ----------------------------------------------

    def enrich_children(
        self,
        run_id: str,
        *,
        runtime: AgentRuntime | None,
        artifact_failures: list[str],
        sweep: Callable[[Path], bool],
        warn: Callable[[str], None],
    ) -> bool:
        """Associate pi-subagents artifacts with the child documents (R2, pi 族).

        The candidates are the E2B mirror (``runs/<id>/subagents/artifacts``)
        and, on the host-side faces, the session dir's ``subagent-artifacts/``.
        Matched file groups are copied into ``children/<n>/`` (normalized
        ``<kind>.<ext>`` names), ``_meta.json`` contributes its whitelisted
        counters, and the document records the outcome honestly: ``meta`` when
        paired, ``unmatched`` when the run has artifacts but the ordinal
        pairing failed, ``none`` when there is nothing to pair. Copies pass
        the run's redactor sweep like every other artifact; the sources stay
        untouched (they are the recovery substrate, not the product surface).
        """
        page = self._store.read_children(run_id)
        if not page.documents:
            return False
        candidates: list[tuple[str, Any, str]] = []  # (artifact relpath, source tree, kind dir label)
        mirror = self._store.run_dir(run_id) / SUBAGENTS_DIRNAME / "artifacts"
        session_artifacts = (
            runtime.session_dir / "subagent-artifacts"
            if runtime is not None
            else None
        )
        for root in (mirror, session_artifacts):
            if root is None or not root.is_dir():
                continue
            try:
                with ArtifactTree(root) as tree:
                    for relative in tree.files(max_files=_ENRICH_VISIT_LIMIT):
                        candidates.append((relative, root, relative))
            except ArtifactError as exc:
                logger.warning("子代理产物目录不可读 run=%s %s: %s", run_id, root, exc)
        named = [
            (parsed, root, relative)
            for relative, root, _label in candidates
            if (parsed := parse_artifact_filename(relative)) is not None
        ]
        if not named:
            return False
        association = associate_children(
            [dict(document) for document in page.documents],
            [parsed for parsed, _root, _rel in named],
        )
        roots = {id(parsed): (root, relative) for parsed, root, relative in named}
        copied_total = 0
        for n, artifacts in association.by_n.items():
            document = dict(self._store.read_child(run_id, n) or {})
            if not document:
                continue
            listing: list[dict[str, Any]] = []
            enrichment: dict[str, Any] = {}
            for artifact in artifacts:
                root, relative = roots[id(artifact)]
                stored = f"{artifact.kind}.{artifact.path.rsplit('.', 1)[-1]}"
                try:
                    with ArtifactTree(root) as source:
                        data = source.read(relative, max_bytes=_ENRICH_FILE_MAX_BYTES)
                    copied_total += len(data)
                    if copied_total > _ENRICH_TOTAL_MAX_BYTES:
                        artifact_failures.append("子代理增强复制超过总量上限")
                        break
                    with ArtifactTree(self._store.run_dir(run_id)) as run_tree:
                        run_tree.replace(
                            f"{CHILDREN_DIRNAME}/{n}/{stored}",
                            data,
                            max_bytes=_ENRICH_FILE_MAX_BYTES,
                            create_parents=True,
                        )
                except (ArtifactError, OSError) as exc:
                    artifact_failures.append(f"子代理产物关联复制失败: {relative}: {exc}")
                    continue
                listing.append({"name": stored, "bytes": len(data), "source": artifact.path})
                if artifact.kind == "meta":
                    meta = read_meta_enrichment(data)
                    if meta:
                        enrichment.update(meta)
            document.update(enrichment)
            document["enrichment"] = "meta"
            if listing:
                document["artifacts"] = listing
            try:
                self._store.write_child(run_id, document)
            except (ArtifactError, OSError, ValueError) as exc:
                warn(f"子代理增强落文档失败 n={n}: {exc}")
        for n in association.unmatched_children:
            document = self._store.read_child(run_id, n)
            if document and document.get("enrichment") in (None, "none"):
                try:
                    self._store.write_child(run_id, {**document, "enrichment": "unmatched"})
                except (ArtifactError, OSError, ValueError) as exc:
                    warn(f"子代理关联标记失败 n={n}: {exc}")
        if association.unmatched_files:
            logger.info(
                "未关联的子代理产物保留在回收基质 run=%s: %s",
                run_id,
                ", ".join(a.path for a in association.unmatched_files[:8]),
            )
        children_dir = self._store.run_dir(run_id) / CHILDREN_DIRNAME
        return children_dir.is_dir() and sweep(children_dir)

    def match_progress_n(
        self, fact: Any, progress_calls: Mapping[str, list[dict[str, Any]]]
    ) -> int | None:
        # pi 族：前台守卫保证同一时刻至多一个在飞派发 call；transcript 的
        # ``index`` 是该 call 内的 task 位置，且 ``agent`` 同名才配对。
        agent = getattr(fact, "agent", None)
        index = getattr(fact, "index", None)
        if (
            isinstance(agent, str)
            and isinstance(index, int)
            and not isinstance(index, bool)
        ):
            for entries in progress_calls.values():
                if 0 <= index < len(entries) and entries[index]["agent"] == agent:
                    return entries[index]["n"]
        return None

    def accumulate_usage(
        self,
        frame: Mapping[str, Any],
        payload: Mapping[str, Any],
        total: dict[str, Any],
        seen_keys: set[str],
    ) -> dict[str, Any] | None:
        """Turn one response's usage snapshot into a run cumulative total.

        pi reports the usage of *one assistant response* (its own fields are
        already cumulative for that request). The run total is the sum across
        responses; a frame carrying the same message identity is not counted
        twice. No price is invented: ``cost`` is summed only when pi reported it.
        """
        snapshot = payload.get("usage")
        if not isinstance(snapshot, Mapping):
            return None
        key = self._usage_key(frame)
        if key is not None:
            if key in seen_keys:
                return None
            seen_keys.add(key)
        for name in _USAGE_FIELDS:
            value = snapshot.get(name)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                total[name] = total.get(name, 0) + value
        cost = snapshot.get("cost")
        if isinstance(cost, Mapping):
            cost_total = total.setdefault("cost", {})
            for name, value in cost.items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    cost_total[name] = cost_total.get(name, 0) + value
        total["requests"] = total.get("requests", 0) + 1
        return {"usage": dict(total), "requestUsage": dict(snapshot)}

    @staticmethod
    def _usage_key(frame: Mapping[str, Any]) -> str | None:
        message = frame.get("message")
        if not isinstance(message, Mapping):
            return None
        for name in ("timestamp", "id"):
            value = message.get(name)
            if isinstance(value, str) and value:
                return f"{name}:{value}"
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return f"{name}:{value}"
        return None
