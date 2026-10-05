"""Admit: turn a start request into an :class:`~glink_harness.runs.state.AdmittedRun`.

Admission is split around the manager's admission lock:

* :meth:`RunAdmission.prepare` runs **outside** the lock. It parses the ``/``
  command gate, resolves and renders the blueprint, resolves the frozen
  :class:`RunSpec`, runs the follow-up's fail-fast gates, selects the run's
  credentials, builds the run's :class:`RunSecretBoundary` and takes the
  follow-up's session snapshot (bounded file IO) — so one tenant's follow-up
  never serializes another tenant's admission behind its disk IO. It returns
  a frozen :class:`AdmissionRequest`.
* :meth:`RunAdmission.admit` runs **inside** the lock (after the manager's
  concurrency gate). It rechecks the parent authoritatively and creates the
  record; its answer is the frozen :class:`AdmittedRun`.

Nothing here starts a thread or touches a face. The harness-family cache lives
here too: every "the pi family does X, the ACP family does Y" decision goes
through the family the *run's own* spec names, never a process-wide one.
"""

from __future__ import annotations

import logging
import re
import threading
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Sequence

from ..blueprints import (
    Blueprint,
    BlueprintError,
    blueprint_names,
    load_blueprint,
    render_blueprint,
)
from ..config import HostConfig
from ..contracts import FollowupUnavailable
from ..kernel.agent_env import RunCredentials
from ..kernel.harness import HarnessFamily, OwnerRef, select_family
from ..redaction import SecretRedactor
from ..store import AdmissionCheck, IdempotencyClaim, RunRecord, RunSpec, RunStore
from ..tenancy import TenantContext, TenantCredentialStore, TenantNotConfigured
from .errors import BlueprintUnavailable, UnsupportedCommand
from .secrets import RunSecretBoundary
from .state import LOGGER_NAME, AdmittedRun

logger = logging.getLogger(LOGGER_NAME)


def owner_ref_of(context: TenantContext | None) -> OwnerRef:
    """``TenantContext`` → ``OwnerRef`` 的唯一映射点（R1-4，设计 §2.3 末段）。

    kernel 层（族接口与协议面）只引用抽象
    :class:`~glink_harness.kernel.harness.OwnerRef`，不导入 tenancy 或
    产品账号类型；产品侧的已验证租户身份在此收敛为 owner 标识。本地模式
    （无租户身份）映射为空 OwnerRef。映射本身不做校验——断言/会话的验证
    发生在 web 入口，owner 匹配与异租户拒绝的语义由下方门禁保持原样。
    """
    return OwnerRef(owner_id=context.tenant_id) if context is not None else OwnerRef()


def parse_blueprint_command(text: str, allowed: Sequence[str]) -> tuple[str | None, str]:
    """Split a ``/<blueprint> <topic>`` prompt; refuse anything else.

    Returns ``(blueprint, prompt)``. Text that does not open with ``/`` is an
    ordinary prompt and comes back unchanged. The accepted form is exactly a
    known blueprint name followed by whitespace and a non-empty topic that does
    not itself start with ``/`` (the rewritten prompt must not reach pi's
    extension dispatch).
    """
    stripped = text.lstrip()
    if not stripped.startswith("/"):
        return None, text
    match = re.match(r"^/(\S+)(?:\s+([\s\S]*))?$", stripped)
    name = match.group(1) if match is not None else ""
    topic = (match.group(2) or "").strip() if match is not None else ""
    if name not in allowed or topic.startswith("/"):
        raise UnsupportedCommand(allowed)
    if not topic:
        raise ValueError("prompt must not be empty after a /<blueprint> command")
    return name, topic


@dataclass(frozen=True, slots=True)
class AdmissionRequest:
    """Everything decided before the admission lock is entered."""

    text: str
    context: TenantContext | None
    #: ``context`` 的 kernel 侧抽象身份（R1-4）：准入冻结时经
    #: :func:`owner_ref_of` 映射，写记录与 kernel 边界的身份事实只引用它。
    owner: OwnerRef
    parent_run_id: str | None
    blueprint: Blueprint | None
    blueprint_prompt: str
    spec: RunSpec
    credentials: RunCredentials
    secrets: RunSecretBoundary
    session_seed: bytes | None
    continuation_source: Path | None


class RunAdmission:
    """Admission rules and the per-name harness-family cache."""

    def __init__(
        self,
        *,
        config: HostConfig,
        store: RunStore,
        credential_store: TenantCredentialStore | None,
        host_redactor: SecretRedactor,
    ) -> None:
        self.config = config
        self.store = store
        self.credential_store = credential_store
        self.host_redactor = host_redactor
        #: Harness families by name (``RunSpec.family``): admission resolves the
        #: spec from the frozen config (:meth:`spec`), and from then on the
        #: record is the only source (:meth:`family_of`).
        self.families: dict[str, HarnessFamily] = {}
        self._families_lock = threading.Lock()

    # -- run spec and families ------------------------------------------------

    def spec(self, blueprint: Blueprint | None) -> RunSpec:
        """The :class:`RunSpec` a new run is admitted with, from the frozen config.

        This is the only place the host's configured harness family, pack and
        sandbox face turn into run facts; the credential origin is filled in
        once the run's credentials are selected. No request field selects them
        (yet): a per-request choice would only change this resolution.
        """
        return RunSpec(
            family=self.config.harness,
            pack=self.config.harness_pack,
            executor=self.config.executor,
            blueprint=blueprint.name if blueprint is not None else None,
        )

    def family_for(self, name: str) -> HarnessFamily:
        """The (cached) harness family object named by a ``RunSpec.family``."""
        with self._families_lock:
            family = self.families.get(name)
            if family is None:
                family = select_family(name, config=self.config, store=self.store)
                self.families[name] = family
            return family

    def family_of(self, record: RunRecord) -> HarnessFamily:
        """The family of one admitted run: its record's ``spec.family``."""
        return self.family_for(record.spec.family)

    # -- blueprints and credentials -------------------------------------------

    def resolve_blueprint(self, blueprint: str) -> Blueprint:
        """Resolve one session template name against the configured directory.

        The accepted set is derived from the directory on every admission (a
        blueprint added or removed on disk is in force immediately), and the
        name is matched exactly — a client cannot address a file outside it. A
        malformed document is refused too: a partially rendered contract would
        be worse than no contract. The refusal carries only the derived name
        list, never the submitted value.
        """
        try:
            return load_blueprint(self.config.blueprints_dir, blueprint)
        except BlueprintError as exc:
            available = blueprint_names(self.config.blueprints_dir)
            code = "unknown_blueprint" if exc.reason == "unknown" else "invalid_blueprint"
            # ``repr`` escapes control characters, so a hostile name cannot
            # inject lines into the host log; the value is truncated, and the
            # refusals the client sees carry none of it.
            logger.warning(
                "blueprint refused name=%r reason=%s", (blueprint or "")[:64], exc.reason
            )
            raise BlueprintUnavailable(code, available) from exc

    def credentials_for(self, context: TenantContext | None) -> RunCredentials:
        """Select the credentials one run may render (caller holds no lock).

        Multi-tenant mode requires a verified context. Under the default
        ``tenant`` policy the run needs the tenant's own model key; a missing
        entry is a refusal, never a fallback to the host's global ``.env`` key
        or to another tenant's credential file. Under the explicit ``shared``
        policy every tenant renders the one host-frozen credential instead.
        """
        if self.config.multi_tenant:
            if context is None:
                raise TenantNotConfigured("多租户模式需要已验证的租户身份")
            if self.config.shared_model_credentials:
                shared = RunCredentials.from_shared_host(self.config)
                if not shared.model_api_key:
                    # Defensive: the configuration refuses this at startup.
                    raise TenantNotConfigured("宿主未冻结共享模型凭据")
                return shared
            if self.credential_store is None:
                raise TenantNotConfigured("宿主未加载租户凭据配置")
            tenant = self.credential_store.require(context.tenant_id)
            return RunCredentials(
                model_api_key=tenant.model_api_key,
                web_search_key=tenant.web_search_key,
                origin="tenant",
            )
        return RunCredentials.from_host(self.config)

    def redactor_for_record(self, record: RunRecord) -> SecretRedactor:
        """The redaction boundary appropriate for an already-persisted run.

        Used by the report download: a multi-tenant report is masked against
        the record's own tenant credentials, never another tenant's. Under the
        explicit ``shared`` policy the credential is the host-level one, which
        is already part of the host redactor (and is never a tenant secret), so
        no tenant entry is consulted — the shared key must mask even when the
        tenant entry has no key at all.
        """
        if (
            not self.config.multi_tenant
            or self.config.shared_model_credentials
            or self.credential_store is None
        ):
            return self.host_redactor
        tenant = self.credential_store.get(record.tenant_id)
        if tenant is None:
            return self.host_redactor
        redactor = SecretRedactor(self.host_redactor.known_values)
        redactor.add(tenant.model_api_key)
        redactor.add(tenant.web_search_key)
        return redactor

    # -- follow-up parents ----------------------------------------------------

    def authorized_parent(
        self, context: TenantContext | None, parent_run_id: str
    ) -> RunRecord | None:
        """The parent this run may resume, or ``None`` (same as an unknown run).

        The tenant gate runs before any other field of the parent is read. In
        multi-tenant mode an unknown, foreign or ownerless (single-user) parent is
        therefore invisible: the caller cannot learn its status, session,
        report, children or existence. Local mode has no tenant identity and
        keeps the original single-user behaviour.
        """
        parent = self.store.get(parent_run_id)
        if parent is None:
            return None
        owner = owner_ref_of(context)
        if self.config.multi_tenant and (
            owner.owner_id is None or parent.tenant_id != owner.owner_id
        ):
            return None
        return parent

    def visible_records(self, context: TenantContext | None) -> list[RunRecord]:
        """Every run the caller may see (all runs in local mode)."""
        if not self.config.multi_tenant:
            return self.store.list(limit=None)
        owner = owner_ref_of(context)
        assert owner.owner_id is not None  # 多租户下必须带租户上下文
        return self.store.list_for(owner.owner_id, limit=None)

    def resumable_parent(
        self, context: TenantContext | None, parent_run_id: str, spec: RunSpec
    ) -> RunRecord:
        """The parent a follow-up may resume *now*, or the refusal it has earned.

        The owner gate comes first (``unknown_run``): an unknown, foreign or
        ownerless parent is refused before its status, session, report or
        children are read. The state refusals then follow in their fixed order:
        a parent that is not terminal is ``run_busy``, one that already has a
        follow-up turn is ``stale_run``.

        Between the owner gate and the state checks the parent must have run
        under the child's family and pack: the record's ``spec.family`` and
        ``spec.pack`` (frozen at admission) must equal the child's ``spec``,
        otherwise the follow-up is ``session_unavailable`` whatever the
        parent's status — a foreign parent is still ``unknown_run`` first, so
        existence is not disclosed across tenants. The family-specific facts
        (the pack's declared ``capabilities.resume``, then the ACP negotiated
        capability matrix and state root) follow in
        :meth:`HarnessFamily.check_continuation`.

        One admission calls this twice: outside the lock, to fail fast before
        the parent's session file is read, and inside it, where the decision is
        authoritative — between the two calls the parent may have gained a
        follow-up turn.
        """
        parent = self.authorized_parent(context, parent_run_id)
        if parent is None:
            raise FollowupUnavailable("unknown_run")
        if parent.spec.family != spec.family:
            raise FollowupUnavailable("session_unavailable")
        if parent.spec.pack != spec.pack:
            # 同族不同包（例如 dsh → opencode）的会话不能续接：agent 自己的状态根
            # 与续接能力都是按包声明的，fail-closed 胜过猜一个映射。
            raise FollowupUnavailable("session_unavailable")
        if not self.config.pack.capabilities.resume:
            # 包清单的能力矩阵声明本包会话不可续接（capabilities.resume=false）：
            # 在读取父会话之前就拒绝，不交给族代码去猜。
            raise FollowupUnavailable("session_unavailable")
        if not parent.is_terminal:
            raise FollowupUnavailable("run_busy")
        if any(
            item.parent_run_id == parent_run_id
            for item in self.visible_records(context)
        ):
            raise FollowupUnavailable("stale_run")
        return parent

    def resume_seed(
        self,
        context: TenantContext | None,
        parent_run_id: str | None,
        redactor: SecretRedactor,
        spec: RunSpec,
    ) -> bytes | None:
        """Snapshot the sanitized session of an authorized parent run.

        Caller holds no lock. This is the follow-up's file IO — up to
        ``REPORT_MAX_BYTES`` read, decoded and redacted — which therefore never
        blocks another admission. The owner gate and the state refusals still
        run first (:meth:`resumable_parent`) and are rechecked under the
        admission lock before the run is created, so the seed handed to the
        kernel comes from a parent that was authorized when it was read *and*
        was still resumable when the child was admitted.

        The seed itself is the family's: the pi family reads the parent's
        session file (bounded, re-redacted); the ACP family has no host
        session file and answers ``None`` — its continuation lives in the
        parent's agent state root, which ``prepare_runtime`` inherits after
        admission.
        """
        if parent_run_id is None:
            return None
        parent = self.resumable_parent(context, parent_run_id, spec)
        return self.family_for(spec.family).resume_seed(parent, redactor=redactor)

    # -- the two halves of one admission --------------------------------------

    def prepare(
        self,
        prompt: str,
        context: TenantContext | None,
        *,
        parent_run_id: str | None,
        blueprint: str | None,
    ) -> AdmissionRequest:
        """Everything before the admission lock (see the module docstring)."""
        text = (prompt or "").strip()
        if not text:
            raise ValueError("prompt must not be empty")
        # 宿主级 ``/`` 命令门禁（harness 无关，GLINK-ARCH-6）：以 ``/`` 开头的输入只会
        # 被解释为 ``/<蓝图名> <题目>``；其余（包括重写后仍以 ``/`` 开头的题目）一律拒绝，
        # 因为 pi 对以 ``/`` 开头的消息先派发扩展命令，而 pi-subagents 注册的
        # ``/run --bg`` / ``/subagents-detach`` 不随本 run 的生命周期收口。拒绝发生在凭据
        # 解析、父会话读取、run 目录与执行面创建之前，同一条判定服务所有族、追问与三面。
        allowed_commands = blueprint_names(self.config.blueprints_dir)
        command_blueprint, text = parse_blueprint_command(text, allowed_commands)
        if command_blueprint is not None:
            if blueprint is not None:
                # 两个入口同时给出时不猜优先级（斜杠形式就是 blueprint 字段的糖）。
                raise ValueError(
                    "blueprint 不能同时由 /<蓝图名> 命令与 blueprint 字段给出"
                )
            blueprint = command_blueprint
        # The session template is resolved and rendered before any credential is
        # read, any parent is inspected or a run directory exists: an unknown or
        # malformed blueprint refuses the whole admission, on every harness.
        blueprint_template = (
            self.resolve_blueprint(blueprint) if blueprint is not None else None
        )
        blueprint_prompt = (
            render_blueprint(blueprint_template) if blueprint_template is not None else ""
        )
        # The run's spec as far as it is known before credentials are read
        # (family/pack/executor from the frozen config, the resolved
        # blueprint); the credential origin completes it below.
        target = self.spec(blueprint_template)
        family = self.family_for(target.family)
        continuation_source = None
        if parent_run_id is not None:
            # 锁外追问门禁：共享门禁（owner→harness 字段→terminal→stale）先行，
            # 族的追加事实（ACP 族的能力矩阵、session id、状态根；pi 族无追加）
            # 随后。fail-closed 发生在凭据解析与 run 目录创建之前；子 adapter
            # 在任何 resume 请求前还会自查它自己协商到的能力。
            continuation_source = family.check_continuation(
                self.resumable_parent(context, parent_run_id, target)
            )
        credentials = self.credentials_for(context)
        family.validate_credentials(credentials)
        spec = replace(target, credential_origin=credentials.origin)
        # Build the run's secret boundary before anything derived from the
        # prompt can be stored: a credential pasted into the prompt is masked
        # with the same knowledge the run's own output will be.
        secrets = RunSecretBoundary.for_credentials(self.host_redactor, credentials)
        # Caller holds no lock: the parent gate, the refusal checks and the
        # session snapshot's file IO happen before admission. The refusals are
        # re-evaluated inside the lock (:meth:`admit`), where the seed is
        # consumed, so a state that changed while the file was being read is
        # still enforced.
        seed = self.resume_seed(context, parent_run_id, secrets.redactor, spec)
        return AdmissionRequest(
            text=text,
            context=context,
            owner=owner_ref_of(context),
            parent_run_id=parent_run_id,
            blueprint=blueprint_template,
            blueprint_prompt=blueprint_prompt,
            spec=spec,
            credentials=credentials,
            secrets=secrets,
            session_seed=seed,
            continuation_source=continuation_source,
        )

    def continuation_guard(self, request: AdmissionRequest) -> AbstractContextManager[None]:
        if request.parent_run_id is None:
            return nullcontext()
        parent = self.authorized_parent(request.context, request.parent_run_id)
        if parent is None:
            raise FollowupUnavailable("unknown_run")
        return self.family_for(request.spec.family).continuation_guard(parent)

    def admit(
        self,
        request: AdmissionRequest,
        admission_check: AdmissionCheck | None = None,
        idempotency: IdempotencyClaim | None = None,
    ) -> AdmittedRun:
        """Create the record (caller holds the admission lock).

        ``admission_check`` is the concurrency/ledger gate: the store runs it
        inside the same database transaction that inserts the record, so the
        holds it counts and the insert cannot interleave with another writer.
        ``idempotency`` (R3) is the owner's key claim, reserved in that same
        transaction (see :meth:`RunStore.create`); the lookup half runs in the
        manager before ``prepare``, so a replay never reaches this method.
        """
        context = request.context
        parent_run_id = request.parent_run_id
        continuation_workspace: Path | None = None
        continuation_source = request.continuation_source
        if parent_run_id is not None:
            # Authoritative recheck (the snapshot may have raced a concurrent
            # follow-up of the same parent): a refusal here discards the seed,
            # nothing has been written yet. 共享门禁与族追加门禁在同一次调用内
            # 保持原次序（先共享后族）。
            family = self.family_for(request.spec.family)
            parent = self.resumable_parent(context, parent_run_id, request.spec)
            continuation_source = family.check_continuation(parent)
            # The family decides whether the conversation's workspace is reused
            # (ACP keeps it: the session's ``cwd`` is recorded in the agent's
            # own state) or fresh (pi).
            continuation_workspace = family.continuation_workspace(parent)
        blueprint = request.blueprint
        # The store allocates the id and creates the run directory exclusively
        # (a generated collision is retried there): a new run can never adopt or
        # overwrite an existing record. An ACP continuation records the
        # conversation's workspace; every other run gets its own freshly
        # allocated one.
        record = self.store.create(
            request.secrets.redact(request.text),
            workspace=continuation_workspace,
            workspace_root=(
                None if continuation_workspace is not None else self.config.workspace_root
            ),
            tenant_id=request.owner.owner_id,
            creator_user_id=context.user_id if context is not None else None,
            parent_run_id=parent_run_id,
            blueprint_stages=(
                tuple(stage.as_record() for stage in blueprint.stages)
                if blueprint is not None
                else ()
            ),
            # 准入时从冻结配置解析的 RunSpec，与 run id/owner 同一次原子写入：
            # 此后族、执行面、追问门禁与状态根都读它，不用当前配置反推历史 run
            # （docs/adr/0002）。
            spec=request.spec,
            admission_check=admission_check,
            idempotency=idempotency,
        )
        request.secrets.run_id = record.run_id
        return AdmittedRun(
            record=record,
            workspace=Path(record.workspace),
            kernel_prompt=request.text,
            context=context,
            credentials=request.credentials,
            secrets=request.secrets,
            session_seed=request.session_seed,
            parent_run_id=parent_run_id,
            blueprint=blueprint,
            blueprint_prompt=request.blueprint_prompt,
            continuation_source=continuation_source,
        )
