"""The pi harness family（实现体自 runner.py 对应段落逐字搬迁）。

pi 族的策略面：模板命令白名单、宿主模型凭据准入、会话文件种子、
``AgentRuntime`` 渲染与 ``Projector``、RPC/Docker/E2B 面的 adapter 组装、
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
    AgentRuntime,
    build_pi_argv,
    build_pi_env,
    prepare_agent_dir,
    prompt_template_names,
)
from .docker_env import (
    build_plan as build_docker_plan,
    is_available as docker_available,
    stop_argv as docker_stop_argv,
)
from .e2b_env import SANDBOX_SESSIONS
from .execution import ExecutionAdapter, ExecutionSpec
from .execution import build_adapter as compose_adapter
from .harness import (
    FollowupUnavailable,
    HarnessFamily,
    PreparedRuntime,
    Verdict,
)
from .projection import Projector
from .subagent_artifacts import (
    associate_children,
    parse_artifact_filename,
    read_meta_enrichment,
)

if TYPE_CHECKING:  # pragma: no cover - annotation only
    from ..redaction import SecretRedactor
    from .agent_env import RunCredentials

logger = logging.getLogger("pi_glink.kernel.harness_pi")

#: 与 runner 的同义常量同值搬迁（runner 重接线前两处并存，语义逐字一致）：
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

    def allowed_prompt_commands(self) -> tuple[str, ...]:
        # pi dispatches extension commands and expands prompt templates, so the
        # allowlist is the rendered prompt-template names.
        return prompt_template_names(self._config.agent_config_dir)

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

    def verifies_continuation_before_turn(self, executor: str) -> bool:
        # 恒 True：三面同规则。e2b 面自 GLINK-ARCH-3 收敛起走 RPC 通道，
        # ``get_state`` 在 ``session_ready`` 即知会话身份（pi 以
        # ``--session continued.jsonl`` 启动，续接会话原地打开、保持原
        # session id）——原义务（沙箱面流中才发现 session id，只能回合后由
        # 驱动线程比对 ``expected_session_id``）随旧 ``--mode json`` 适配器
        # 整体删除。``executor`` 参数保留以合 ABC 签名。
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
    ) -> PreparedRuntime:
        runtime = prepare_agent_dir(
            self._config,
            runtime_dir=run_dir / RUNTIME_DIRNAME,
            session_dir=Path(self._config.session_dir) / run_id,
            credentials=credentials,
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

    # -- (d) adapter 组装 --------------------------------------------------------

    def build_adapter(
        self,
        spec: ExecutionSpec,
        *,
        executor: str,
        on_frame: Callable[[Mapping[str, Any]], None],
        on_identity: Callable[[Mapping[str, str]], None],
        client_factory: Callable[..., Any],
        sandbox_factory: Callable[..., Any] | None,
    ) -> ExecutionAdapter:
        return compose_adapter(
            spec,
            harness=self.name,
            executor=executor,
            client_factory=client_factory,
            on_frame=on_frame,
            sandbox_factory=sandbox_factory,
            on_identity=on_identity,
            rpc_plan=self._rpc_plan,
            docker_plan=self._docker_plan,
            docker_stop=docker_stop_argv,
            e2b_rpc_plan=self._e2b_rpc_plan,
        )

    def _rpc_plan(
        self, spec: ExecutionSpec
    ) -> tuple[list[str], dict[str, str], Path | None]:
        return (
            build_pi_argv(spec.config, spec.runtime, extra=[
                *spec.extra_argv,
                *(["--session", str(spec.runtime.session_dir / spec.resume_session)] if spec.resume_session else []),
            ]),
            build_pi_env(spec.config, spec.runtime),
            spec.workspace,
        )

    def _docker_plan(
        self, spec: ExecutionSpec
    ) -> tuple[list[str], dict[str, str], str]:
        if not docker_available():
            raise ConfigError(
                "LOCAL_WEB_EXECUTOR=docker requires a reachable Docker daemon"
            )
        plan = build_docker_plan(
            spec.config,
            spec.runtime,
            run_id=spec.run_id,
            workspace=spec.workspace,
            session_dir=spec.runtime.session_dir,
            extra=[*spec.extra_argv, *(["--session", f"/sessions/{spec.resume_session}"] if spec.resume_session else [])],
        )
        return plan.argv, plan.env, plan.container_name

    def _e2b_rpc_plan(self, spec: ExecutionSpec) -> list[str]:
        """e2b 面沙箱内 pi 的 RPC argv（命令链由 ``E2BSandboxClient.spawn_rpc`` 组合）。

        ``argv[0]`` 是字面量 ``"pi"``（镜像 PATH 内，docker_env.py 先例）：
        宿主路径 ``config.pi_bin`` 在沙箱里没有意义。``spec.extra_argv`` 已含
        ``--name pi-glink-<run_id>`` 与可选 ``--append-system-prompt <text>``
        （runner 统一注入，三面不变），``shlex.join`` 在 spawn 时负责其引
        用。续接形式与另两面同形（``--session <路径>``），只是路径是沙箱内
        的 ``/home/user/sessions/continued.jsonl``——``upload_continuation``
        的上传位置与该路径一致。prompt 本体走 stdin 的 ``prompt`` 帧，不进
        argv。
        """
        argv = [
            "pi",
            "--mode", "rpc",
            "--provider", spec.runtime.provider_id,
            "--model", spec.runtime.model_id,
            *spec.extra_argv,
        ]
        if spec.resume_session:
            argv.extend(["--session", f"{SANDBOX_SESSIONS}/{spec.resume_session}"])
        return argv

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
