"""The execution core composed without any HTTP face (multi-harness-plan §6, R2).

``build_execution_service`` assembles everything a run needs — the data-root
ownership, the identity material, the event bus, the run store and the run
manager — and reconciles leftover ``running`` rows before it returns, so the
service can execute and replay runs with or without an HTTP face on top
(:func:`glink_harness.app.create_server` puts one there). This module must not
import the HTTP/UI layer; ``tests/test_repository_boundaries.py`` holds that
direction.

Ordering is part of the safety contract:

1. the data-root lock is acquired **first**, before any read or write under
   the data root — including account-database creation and migration;
2. identity material is loaded (a missing or over-permissive key file refuses
   to start instead of falling back to the unauthenticated local surface);
3. the run store is opened and reconciled before the service is returned.

Binding a port and starting the periodic resource sweep belong to the caller
that adds a face; an embedded service starts its own sweep with
``manager.start_resource_watch()``.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from .accounts import AccountsStore
from .auth import AccountsAuth
from .config import ConfigError, HostConfig
from .events import EventBus
from .runs import RunManager
from .ownership import DataRootOwnership
from .store import RunStore
from .tenancy import Tenancy, build_tenancy

logger = logging.getLogger(__name__)


class ExecutionService:
    """One host process's execution core and its single teardown path.

    Tests may hand-assemble the core from its parts (this constructor); the
    production path is :func:`build_execution_service`, which also owns the
    acquisition ordering and the rollback of a failed startup. ``close`` is
    the one teardown path whether or not an HTTP face was ever built on top.
    """

    def __init__(
        self,
        config: HostConfig,
        bus: EventBus,
        store: RunStore,
        manager: RunManager,
        *,
        ownership: DataRootOwnership | None = None,
        tenancy: Tenancy | None = None,
        auth: AccountsAuth | None = None,
    ) -> None:
        self.config = config
        self.bus = bus
        self.store = store
        self.manager = manager
        self.ownership = ownership
        #: Frozen multi-tenant trust material; ``None`` in local mode.
        self.tenancy = tenancy
        #: Identity backend for accounts mode; ``None`` in gateway and local mode.
        self.auth = auth
        #: Teardown-once guard; see :meth:`close`.
        self._closed = False

    @property
    def accounts(self) -> AccountsStore | None:
        """The account store, when accounts mode is active."""
        return self.auth.store if self.auth is not None else None

    def close(self, *, shutdown_timeout: float = 15.0) -> None:
        """Tear the core down in the reverse order of its assembly.

        The run shutdown is part of the contract: in-flight runs are
        cancelled and awaited to their terminal commit
        (:meth:`RunManager.shutdown`) **before** the data-root lock goes —
        releasing the lock while a run is still live would let a second
        host's reconciliation overwrite it (the ownership invariant). The
        ledger sweep stops before that so no probe runs against a closing
        host; the egress listener, the run store and the identity material
        follow. Every step runs even when an earlier one raised: the first
        exception is re-raised, later ones are logged, so a partial teardown
        never strands the data-root lock.

        Every component's close is idempotent, and the whole teardown runs at
        most once: a repeated call returns immediately. One is expected —
        the standard library's bind-failure path inside ``socketserver``
        calls ``server_close`` on a half-built server, and
        ``create_server``'s rollback then calls ``close`` again; running the
        steps twice would make the run shutdown read an already-closed
        store.
        """
        if self._closed:
            return
        self._closed = True
        steps: tuple[tuple[str, Callable[[], object]], ...] = (
            ("resource sweep", self.manager.stop_resource_watch),
            ("run shutdown", lambda: self.manager.shutdown(timeout=shutdown_timeout)),
            ("egress listener", self.manager.egress.close),
            ("run store", self.store.close),
            ("identity material", self._close_tenancy),
            ("data-root lock", self._release_ownership),
        )
        failure: BaseException | None = None
        for name, step in steps:
            try:
                step()
            except BaseException as exc:  # noqa: BLE001 - teardown must reach the lock
                if failure is None:
                    failure = exc
                else:
                    logger.error("拆除 %s 失败（继续其余步骤）", name, exc_info=exc)
        if failure is not None:
            raise failure

    def _close_tenancy(self) -> None:
        if self.tenancy is not None:
            self.tenancy.close()

    def _release_ownership(self) -> None:
        if self.ownership is not None:
            self.ownership.release()

    def __enter__(self) -> ExecutionService:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def build_execution_service(
    config: HostConfig,
    *,
    client_factory: Callable[..., Any] | None = None,
    docker_probe: Callable[[str], tuple[bool, str]] | None = None,
    sandbox_probe: Callable[[str], tuple[bool, str]] | None = None,
) -> ExecutionService:
    """Assemble the execution core: ownership → identity → store → reconcile.

    ``client_factory`` and the two identity probes exist for tests: production
    always talks to the real ``pi`` binary, the real Docker CLI and the real
    E2B SDK. The probes are what the online resource-ledger sweep calls; they
    never run inside the admission lock.
    """
    # Ownership comes first: only a process that holds the data root may
    # conclude that a ``running`` record has no live owner, or create/rewrite
    # the account database. A second host on the same data root (same or
    # another port) stops here, before touching any record.
    ownership = DataRootOwnership.acquire(config.data_root)
    tenancy: Tenancy | None = None
    store: RunStore | None = None
    try:
        # Accounts mode opens/creates/migrates the SQLite database here, under
        # the lock; gateway mode only reads files.
        tenancy = build_tenancy(config)
        if config.accounts_enabled and (tenancy is None or tenancy.accounts is None):
            # Defensive: never serve accounts mode without its store.
            raise ConfigError("GLINK_AUTH_MODE=accounts 账号库初始化失败")
        # Accounts mode gets its session backend here; gateway and local mode
        # have no in-app login at all.
        accounts_store = tenancy.accounts if tenancy is not None else None
        auth = None
        if config.accounts_enabled:
            assert accounts_store is not None  # 上面的守卫已挡住缺库的情形
            auth = AccountsAuth(config, accounts_store)
        bus = EventBus()
        store = RunStore(config.data_root / "runs")
        manager = RunManager(
            config,
            bus,
            store,
            client_factory=client_factory,
            ownership=ownership,
            credential_store=tenancy.credentials if tenancy is not None else None,
            docker_probe=docker_probe,
            sandbox_probe=sandbox_probe,
        )
        # Runs left ``running`` by a previous process cannot continue; close
        # them out honestly before the service executes or serves anything.
        # External leftovers are not touched here — their identity is recorded
        # for the operator, and the periodic sweep is the only thing that may
        # confirm one.
        manager.reconcile()
        return ExecutionService(
            config,
            bus,
            store,
            manager,
            ownership=ownership,
            tenancy=tenancy,
            auth=auth,
        )
    except BaseException:
        if store is not None:
            store.close()
        if tenancy is not None:
            tenancy.close()
        ownership.release()
        raise
