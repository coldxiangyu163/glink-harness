"""The external-resource ledger: restart reconciliation, adjudication, sweep.

This collaborator owns the parts of the run lifecycle that answer one
question — *which runs still hold external execution resources* — and the
three ways such a hold is resolved: a confirmed cleanup at finalize time
(recorded by the runner), an online sweep that proves the persisted identity
is gone, or an operator's offline adjudication. It also owns restart
reconciliation, because closing out a previous process's ``running`` records
is the same ledger's job: the interrupted commit carries the resource state
``released: null`` so the hold survives honestly.

The runner keeps the admission and execution threads; the ledger keeps the
store-derived facts. Two seams connect them, both injected at construction so
the ledger never reaches back into the manager:

* ``admission_lock`` is the manager's own lock. Snapshots of the in-flight
  run table (and the ``RunBusy`` refusal in adjudication) happen under the
  same lock as admission, exactly as when this code lived on the manager.
* ``execution_ids`` / ``executing_ids`` snapshot that table: every run id
  this process owns, and the subset still executing (phase before the
  committed one).

The identity probes (Docker CLI, E2B SDK) are assembled here from the frozen
host configuration — the E2B probe authenticates with the frozen
``e2b_api_key``, the same source ``Sandbox.create`` gets, instead of the
process environment — so the runner never sees face-specific probe details.
Tests inject fakes through the same constructor arguments.
"""

from __future__ import annotations

import logging
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .. import event_schema, events
from ..config import HostConfig
from ..contracts import conversation_guard
from ..kernel.conversation_lease import (
    ConversationLease,
    LeaseCorruptError,
    read_lease,
    remove_lease,
    write_lease,
)
from ..ownership import DataRootOwnership, OwnershipRequired
from ..redaction import SecretRedactor
from ..store import FAILED, RunAlreadyTerminal, RunRecord, RunStore
from ..kernel.docker_env import confirm_absent as docker_confirm_absent
from ..kernel.e2b_env import confirm_sandbox_absent as e2b_confirm_sandbox_absent

logger = logging.getLogger("glink_harness.runs.resource_ledger")


class RunBusy(RuntimeError):
    """Raised when a run is requested while another one is executing."""

    def __init__(self, active_run_id: str) -> None:
        super().__init__(f"run {active_run_id} is still active")
        self.active_run_id = active_run_id


class ResourceLedger:
    """Track unconfirmed external resources and reconcile them by identity."""

    def __init__(
        self,
        *,
        store: RunStore,
        config: HostConfig,
        ownership: DataRootOwnership | None,
        redactor: SecretRedactor,
        redactor_for_record: Callable[[RunRecord], SecretRedactor],
        admission_lock: threading.RLock,
        execution_ids: Callable[[], set[str]],
        executing_ids: Callable[[], set[str]],
        docker_probe: Callable[[str], tuple[bool, str]] | None = None,
        sandbox_probe: Callable[[str], tuple[bool, str]] | None = None,
    ) -> None:
        self.store = store
        self.config = config
        self._ownership = ownership
        self._redactor = redactor
        self._redactor_for_record = redactor_for_record
        self._admission_lock = admission_lock
        self._execution_ids = execution_ids
        self._executing_ids = executing_ids
        #: Identity probes used by the online resource ledger sweep. Tests inject
        #: fakes here; production addresses only the persisted container name or
        #: sandbox id (never a name pattern, never a listing). The E2B probe
        #: authenticates with the frozen host configuration — the same source
        #: ``Sandbox.create`` gets — instead of the process environment.
        self._docker_probe = docker_probe or docker_confirm_absent
        self._sandbox_probe = sandbox_probe or (
            lambda sandbox_id: e2b_confirm_sandbox_absent(
                sandbox_id, api_key=config.e2b_api_key
            )
        )
        self._resource_watch: threading.Thread | None = None
        self._resource_watch_stop: threading.Event | None = None
        self._resource_sweep_lock = threading.Lock()

    # -- restart reconciliation and resource adjudication ---------------------

    def reconcile(self) -> list[str]:
        """Close out runs a previous host process left ``running``.

        Requires proof that this process owns the data root: without it, a
        run of a live host would be rewritten. A run row that is still
        ``running`` has, by the store's transaction, no terminal frame; it is
        interrupted: a ``run_failed(interrupted)`` frame is committed with the resource state
        ``released: null`` (unknown), so it keeps holding capacity until an
        operator adjudicates it. External resources are never touched.
        Returns the ids that were interrupted.
        """
        self._require_ownership()
        with self._admission_lock:
            executing = self._execution_ids()
        interrupted: list[str] = []
        for run_id in self.store.running_ids():
            if run_id in executing:
                continue
            if self._commit_interrupted(run_id):
                interrupted.append(run_id)
        if interrupted:
            logger.warning(
                "识别到 %d 个上次进程遗留的 running run，已按中断收口: %s",
                len(interrupted),
                ", ".join(interrupted),
            )
        return interrupted

    def _commit_interrupted(self, run_id: str) -> bool:
        record = self.store.get(run_id)
        external = {
            key: value
            for key, value in (
                ("container", record.container_name if record else None),
                ("sandbox", record.sandbox_id if record else None),
            )
            if value
        }
        # Children follow the run here too: a leftover run's still-running
        # child documents are closed as interrupted (the same sweep the run's
        # own verdict applies, shared via the store), so the reconciled
        # terminal frame and the documents never diverge.
        failures = self.store.settle_children(run_id, "interrupted")
        if failures != 0:
            logger.warning(
                "遗留 run 的子代理文档收口不完整 run=%s failures=%s", run_id, failures
            )
        payload = {
            "status": FAILED,
            "stopReason": "interrupted",
            "error": "宿主进程在该 run 结束前退出，已按中断收口",
            "reportPath": None,
            "reportStatus": "none",
            "children": self.store.children_summary(run_id),
            "external": external,
            "cleanup": {
                "released": None,
                "failures": ["宿主进程在该 run 结束前退出，执行资源状态未知，未自动回收"],
                "external": external,
            },
        }
        seq = self.store.last_seq(run_id) + 1
        # This envelope bypasses the bus (the run has no live stream any
        # more), so it is validated and versioned here like a bus frame.
        event_schema.check(events.RUN_FAILED, payload, source=f"resource ledger run={run_id}")
        try:
            self.store.commit_terminal(
                run_id,
                {
                    "type": events.RUN_FAILED,
                    "v": event_schema.EVENT_VERSIONS[events.RUN_FAILED],
                    "runId": run_id,
                    "seq": seq,
                    "ts": time.time(),
                    "payload": payload,
                },
            )
        except (OSError, RunAlreadyTerminal) as exc:
            logger.error("遗留 run 的中断终帧写入失败 run=%s: %s", run_id, exc)
            return False
        return True

    def unreleased_resources(self) -> list[dict[str, Any]]:
        """Runs whose resources are not confirmed released, for operator review.

        Every hold carries its persisted identity and cleanup facts.
        """
        holds = set(self.resource_holds())
        entries = [
            {
                "runId": record.run_id,
                "status": record.status,
                "stopReason": record.stop_reason,
                "container": record.container_name,
                "sandbox": record.sandbox_id,
                "cleanup": record.cleanup,
            }
            for record in self.store.holding_records()
            if record.run_id in holds
        ]
        return entries

    def adjudicate_release(self, run_id: str, note: str) -> RunRecord:
        """Record an operator's decision that a run's resources are released.

        This is the only way besides a confirmed cleanup to return capacity. It
        requires data-root ownership (the host is not running) and a note
        describing how the release was verified; nothing is cleaned up here.
        """
        self._require_ownership()
        reason = (note or "").strip()
        if not reason:
            raise ValueError("裁决必须附带核对说明")
        with self._admission_lock:
            if run_id in self._execution_ids():
                raise RunBusy(run_id)
            record = self.store.get(run_id)
            if record is None:
                raise KeyError(run_id)
            if not record.is_terminal:
                raise ValueError(f"run {run_id} 尚未终态，不能裁决资源")
            if not record.holds_resources:
                raise ValueError(f"run {run_id} 没有待确认的执行资源")
            return self.store.update(
                run_id,
                resource_adjudication={
                    "decision": "released",
                    "source": "operator",
                    "note": self._redactor_for_record(record).redact(reason),
                    "at": time.time(),
                },
            )

    # -- online resource reconciliation ---------------------------------------

    def reconcile_resources(self) -> list[str]:
        """Confirm persisted resources are gone and release the verified holds.

        Only the identity stored in the run's own record is addressed —
        a container name or a sandbox id — and the probe is run against the
        face that created it. No name pattern is guessed, no other resource is
        listed, created or touched. A confirmation records a *separate* fact
        from the operator's: ``resource_adjudication`` with
        ``source: "auto-verified"``, the method and the time; the original
        ``cleanup`` facts and the committed terminal frame stay untouched.

        Fail-closed in every other direction: a missing identity, a probe error,
        a timeout or an unclear answer leaves the hold exactly where it was.

        Never called while the admission lock is held (the ticket bound is per
        probe call) and never overlapping itself: a tick that finds the previous
        sweep running is skipped. Returns the run ids it released.
        """
        if self.config.executor not in ("docker", "e2b"):
            # ``inproc`` acquires no external resource and this host therefore
            # never probes one. (A record left behind by an earlier Docker/E2B
            # deployment still counts; it just needs the offline operator path
            # on a host that is not configured for an external face.)
            return []
        if not self._resource_sweep_lock.acquire(blocking=False):
            return []
        try:
            with self._admission_lock:
                in_flight = self._execution_ids()
            released: list[str] = []
            for record in self.store.holding_records():
                if record.run_id in in_flight or not record.is_terminal:
                    continue
                try:
                    confirmed, method, detail = self._probe_resource(record)
                except Exception as exc:  # noqa: BLE001 - uncertainty keeps the hold
                    logger.warning(
                        "资源探测异常，保持持有 run=%s: %s: %s",
                        record.run_id,
                        type(exc).__name__,
                        exc,
                    )
                    continue
                if not confirmed:
                    logger.info(
                        "执行资源未确认释放，保持持有 run=%s: %s", record.run_id, detail
                    )
                    continue
                try:
                    self.store.update(
                        record.run_id,
                        resource_adjudication={
                            "decision": "released",
                            "source": "auto-verified",
                            "method": method,
                            "note": self._redactor_for_record(record).redact(detail),
                            "at": time.time(),
                        },
                    )
                except Exception:  # noqa: BLE001 - the hold simply stays
                    logger.exception(
                        "自动对账确认后写入裁决失败，保持持有 run=%s", record.run_id
                    )
                    continue
                released.append(record.run_id)
                logger.info("执行资源已按持久化身份确认释放 run=%s method=%s", record.run_id, method)
            return released
        finally:
            self._resource_sweep_lock.release()

    def _probe_resource(self, record: RunRecord) -> tuple[bool, str, str]:
        """``(confirmed, method, detail)`` for one unconfirmed hold.

        The face is chosen from the *record's persisted identity*, not from the
        host's current configuration: an operator may have switched
        ``LOCAL_WEB_EXECUTOR`` since the run, and a leftover container must still
        be probed as a container. A record with no identity at all cannot be
        confirmed online. A record carrying both identities is released only
        when every one of them is confirmed gone.
        """
        checks: list[tuple[str, str]] = []
        if record.container_name:
            checks.append(("docker-inspect", record.container_name))
        if record.sandbox_id:
            checks.append(("e2b-kill", record.sandbox_id))
        if not checks:
            return False, "none", "缺少持久化的执行身份，不猜测资源"
        confirmed = True
        methods: list[str] = []
        details: list[str] = []
        for method, target in checks:
            if method == "docker-inspect":
                probe = self._docker_probe
            else:
                probe = self._sandbox_probe
            ok, detail = probe(target)
            methods.append(method)
            details.append(f"{target}: {detail}")
            if not ok:
                confirmed = False
        return confirmed, "+".join(methods), "; ".join(details)

    # -- conversation leases (docs/adr/0020) ---------------------------------

    def reconcile_leases(self) -> list[str]:
        """Destroy expired conversation leases' sandboxes (fail-closed).

        The lease sweep runs beside the resource sweep on the same watch: an
        idle lease past its TTL is destroyed **by its persisted sandbox id**
        (the same kill-based confirm contract as the resource probe), and the
        record is deleted only once every sandbox it names is confirmed gone —
        a destroy that cannot be confirmed keeps the lease on the books for the
        next tick, never silently forgotten. An unexpired lease only retries
        its ``retiredSandboxIds`` (sandboxes a later lease replaced but could
        not destroy). Requires the frozen E2B key: without a way to destroy,
        the sweep leaves every lease exactly where it is.
        """
        if not self.config.has_e2b_key:
            return []
        if not self._resource_sweep_lock.acquire(blocking=False):
            return []
        try:
            destroyed: list[str] = []
            for conversation_id in self.store.conversation_ids():
                conversation_dir = self.store.conversations_root() / conversation_id
                with conversation_guard(conversation_dir):
                    if conversation_id in self.store.holding_conversation_roots():
                        continue
                    try:
                        lease = read_lease(conversation_dir)
                    except LeaseCorruptError as exc:
                        # 不可读租约不销毁也不跳过文件：显式告警保留，沙箱归属交
                        # 操作员裁决（fail-closed，不把不可读当作无租约静默遗忘）。
                        logger.warning(
                            "对话租约不可读，本轮保留待人工处置 conversation=%s: %s",
                            conversation_id,
                            exc,
                        )
                        continue
                    if lease is None:
                        continue
                    now = time.time()
                    if not lease.is_expired(now):
                        self._retry_retired(conversation_dir, lease)
                        continue
                    targets = [lease.sandbox_id, *lease.retired_sandbox_ids]
                    confirmed = [
                        self._destroy_lease_sandbox(sandbox_id) for sandbox_id in targets
                    ]
                    if all(confirmed):
                        remove_lease(conversation_dir)
                        destroyed.append(conversation_id)
                        logger.info(
                            "对话租约到期已销毁 conversation=%s sandbox=%s",
                            conversation_id,
                            lease.sandbox_id,
                        )
                    else:
                        logger.warning(
                            "对话租约到期但沙箱未能确认销毁，保留待下轮 conversation=%s",
                            conversation_id,
                        )
            return destroyed
        finally:
            self._resource_sweep_lock.release()

    def _destroy_lease_sandbox(self, sandbox_id: str) -> bool:
        """Kill one lease-named sandbox; only the SDK contract confirms."""
        try:
            confirmed, _detail = self._sandbox_probe(sandbox_id)
        except Exception as exc:  # noqa: BLE001 - uncertainty keeps the lease
            logger.warning(
                "对话租约沙箱销毁探测异常，保留 sandbox=%s: %s: %s",
                sandbox_id,
                type(exc).__name__,
                exc,
            )
            return False
        if not confirmed:
            logger.warning("对话租约沙箱未确认销毁，保留 sandbox=%s", sandbox_id)
        return confirmed

    def _retry_retired(self, conversation_dir: Path, lease: ConversationLease) -> None:
        """Retry a live lease's retired sandboxes; drop the ones confirmed gone."""
        if not lease.retired_sandbox_ids:
            return
        remaining = [
            sandbox_id
            for sandbox_id in lease.retired_sandbox_ids
            if not self._destroy_lease_sandbox(sandbox_id)
        ]
        if len(remaining) == len(lease.retired_sandbox_ids):
            return
        updated = ConversationLease(
            conversation_run=lease.conversation_run,
            sandbox_id=lease.sandbox_id,
            generation=lease.generation,
            template=lease.template,
            pack=lease.pack,
            run_id=lease.run_id,
            paused_at=lease.paused_at,
            expires_at=lease.expires_at,
            retired_sandbox_ids=tuple(remaining),
        )
        try:
            write_lease(conversation_dir, updated)
        except Exception:  # noqa: BLE001 - the next tick retries the retired ids
            logger.warning(
                "对话租约 retired 重写失败，下轮重试 conversation=%s",
                lease.conversation_run,
                exc_info=True,
            )

    def delete_conversation(self, root_run_id: str) -> tuple[bool, list[str]]:
        """Delete a conversation's host state: destroy the lease, remove the dir.

        The deletion link's minimal loop (docs/adr/0020): destroying the
        conversation means destroying its paused sandbox and cleaning the
        host-side authoritative state objects (``conversations/<root>/`` —
        the published state and the lease). Fail-closed exactly like the
        sweep: a sandbox the kill cannot confirm keeps the directory and the
        lease record; the run records and event logs are a separate product
        surface this does not touch.
        """
        with conversation_guard(self.store.conversations_root() / root_run_id):
            conversation_dir = self.store.conversations_root() / root_run_id
            if not conversation_dir.is_dir():
                return True, []
            if root_run_id in self.store.holding_conversation_roots():
                # 对话内仍有 run 持有资源（可能正走在热路径上）：删除会从脚下抽走
                # 它的沙箱/状态（cc 终审 S3）；等终态或取消后再删。
                return False, ["对话内仍有运行中的 run，拒绝删除；请先等待或取消"]
            try:
                lease = read_lease(conversation_dir)
            except LeaseCorruptError as exc:
                # 不可读租约下删除目录会把沙箱名字一并抹掉（永久孤儿）：拒绝删除，
                # 由操作员先处置租约（修复或确认沙箱已销毁）再删对话。
                return False, [f"conversation lease unreadable, refusing delete: {exc}"]
            failures: list[str] = []
            if lease is not None:
                for sandbox_id in (lease.sandbox_id, *lease.retired_sandbox_ids):
                    confirmed, detail = self._sandbox_probe(sandbox_id)
                    if not confirmed:
                        failures.append(f"sandbox {sandbox_id}: {detail}")
            if failures:
                return False, failures
            shutil.rmtree(conversation_dir, ignore_errors=False)
            return True, []

    def start_resource_watch(self) -> threading.Thread | None:
        """Start the periodic ledger sweep (once at startup, then every interval).

        The sweep runs in a background daemon thread: it never executes under
        the admission lock and a sweep that is still probing is skipped, never
        queued. It is enabled for the external faces (Docker/E2B) only, because
        only they acquire resources the host can confirm by identity;
        ``GLINK_RESOURCE_RECONCILE_SECONDS=0`` disables it. The offline
        operator path stays available either way.
        """
        interval = self.config.resource_reconcile_seconds
        if (
            interval <= 0
            or not (
                self.config.executor in ("docker", "e2b") or self.config.has_e2b_key
            )
        ):
            # The resource sweep needs an external face; the lease sweep
            # (docs/adr/0020) needs only the frozen E2B key — a host switched
            # away from the e2b face still destroys the leases its data root
            # carries. Neither runs without the face or the key.
            return None
        with self._admission_lock:
            if self._resource_watch is not None:
                return self._resource_watch
            stop = threading.Event()
            thread = threading.Thread(
                target=self._resource_watch_loop,
                args=(stop, interval),
                name="resource-reconcile",
                daemon=True,
            )
            self._resource_watch = thread
            self._resource_watch_stop = stop
        thread.start()
        return thread

    def _resource_watch_loop(self, stop: threading.Event, interval: int) -> None:
        while not stop.is_set():
            try:
                self.reconcile_resources()
                self.reconcile_leases()
            except Exception:  # noqa: BLE001 - a sweep must never kill the host
                logger.exception("资源自动对账失败，保留现有持有")
            stop.wait(interval)

    def stop_resource_watch(self, timeout: float = 5.0) -> None:
        """Stop the periodic sweep without touching the holds it did not verify."""
        with self._admission_lock:
            thread = self._resource_watch
            stop = self._resource_watch_stop
            self._resource_watch = None
            self._resource_watch_stop = None
        if stop is not None:
            stop.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def resource_holds(self) -> list[str]:
        """Runs whose resources are not confirmed released (global fail-closed).

        A run still in flight in this process occupies a concurrency slot
        instead; only a run past its terminal commit can hold resources beyond
        that slot. The distinction matters during the window between the commit
        and the execution leaving the table: the committed run must already
        block new admissions when its cleanup did not confirm release, otherwise
        a race would hand its unreleased resources to the next run.
        """
        with self._admission_lock:
            in_flight = self._executing_ids()
        return [
            record.run_id
            for record in self.store.holding_records()
            if record.run_id not in in_flight
        ]

    def _require_ownership(self) -> None:
        if self._ownership is None:
            raise OwnershipRequired("恢复与资源裁决需要先取得数据目录的执行所有权")
        self._ownership.require(self.config.data_root)
