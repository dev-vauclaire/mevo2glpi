"""Runtime resource ownership for one retention worker process."""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field

import httpx
from sqlalchemy.engine import Engine

from glpi_ticketing_database import (
    AdvisoryLock,
    WorkerRuntimeState,
    WorkerServiceName,
    create_database_engine,
    try_acquire_advisory_lock,
)

from ..composition import build_retention_worker
from ..retention import RetentionRunOutcome
from ..retention.ports import RetentionWorkerProtocol
from ..settings import RetentionWorkerSettings
from .heartbeat import RetentionRuntimeHeartbeatReporter

LOGGER = logging.getLogger(__name__)


class RetentionWorkerAlreadyRunningError(RuntimeError):
    """Report that another PostgreSQL session owns the singleton lock."""


@dataclass(slots=True)
class RuntimeReportingRetentionWorker:
    """Guard each iteration with the lock and publish its outcome."""

    worker: RetentionWorkerProtocol
    advisory_lock: AdvisoryLock
    heartbeat: RetentionRuntimeHeartbeatReporter

    def run_once(self) -> RetentionRunOutcome:
        self.advisory_lock.ensure_held()
        outcome = self.worker.run_once()
        self.advisory_lock.ensure_held()
        self.heartbeat.report_outcome(outcome)
        return outcome


@dataclass(slots=True)
class RetentionWorkerRuntimeContext(AbstractContextManager[RetentionWorkerProtocol]):
    """Own the engine, singleton lock, HTTP client and heartbeat lifecycle."""

    settings: RetentionWorkerSettings
    http_client_factory: Callable[[], httpx.Client] = httpx.Client
    _engine: Engine | None = field(init=False, default=None)
    _advisory_lock: AdvisoryLock | None = field(init=False, default=None)
    _http_client: httpx.Client | None = field(init=False, default=None)
    _heartbeat: RetentionRuntimeHeartbeatReporter | None = field(
        init=False,
        default=None,
    )
    _closed: bool = field(init=False, default=False)

    def __enter__(self) -> RetentionWorkerProtocol:
        if self._engine is not None:
            raise RuntimeError("retention runtime context cannot be entered twice")

        self._engine = create_database_engine(
            self.settings.database_url.get_secret_value(),
            connect_timeout_seconds=(self.settings.database_connect_timeout_seconds),
        )
        try:
            self._advisory_lock = try_acquire_advisory_lock(
                self._engine,
                WorkerServiceName.RETENTION,
            )
            if self._advisory_lock is None:
                raise RetentionWorkerAlreadyRunningError(
                    "another retention worker owns the singleton lock"
                )

            self._heartbeat = RetentionRuntimeHeartbeatReporter(self._advisory_lock)
            self._heartbeat.record_state(WorkerRuntimeState.STARTING)
            self._http_client = self.http_client_factory()
            worker = build_retention_worker(
                self.settings,
                http_client=self._http_client,
                session_factory=self._advisory_lock.open_session,
                ownership_guard=self._advisory_lock.ensure_held,
            )
            self._heartbeat.record_state(WorkerRuntimeState.RUNNING)
            return RuntimeReportingRetentionWorker(
                worker=worker,
                advisory_lock=self._advisory_lock,
                heartbeat=self._heartbeat,
            )
        except BaseException:
            self.close()
            raise

    def __exit__(
        self,
        _error_type: type[BaseException] | None,
        _error: BaseException | None,
        _traceback: object,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Release every acquired resource once, in dependency order."""
        if self._closed:
            return
        self._closed = True

        advisory_lock = self._advisory_lock
        heartbeat = self._heartbeat
        if (
            heartbeat is not None
            and advisory_lock is not None
            and not advisory_lock.is_released
        ):
            try:
                heartbeat.record_state(WorkerRuntimeState.STOPPED)
            except Exception as error:  # pragma: no cover - defensive cleanup
                LOGGER.warning(
                    "retention worker stopped heartbeat failed error_type=%s",
                    type(error).__name__,
                )

        try:
            if self._http_client is not None:
                self._http_client.close()
        finally:
            try:
                if advisory_lock is not None and not advisory_lock.is_released:
                    advisory_lock.release()
            finally:
                if self._engine is not None:
                    self._engine.dispose()


def retention_worker_runtime(
    settings: RetentionWorkerSettings,
    *,
    http_client_factory: Callable[[], httpx.Client] = httpx.Client,
) -> RetentionWorkerRuntimeContext:
    """Create the context owning one fully composed retention worker."""
    return RetentionWorkerRuntimeContext(
        settings=settings,
        http_client_factory=http_client_factory,
    )


__all__ = [
    "RetentionWorkerAlreadyRunningError",
    "RetentionWorkerRuntimeContext",
    "RuntimeReportingRetentionWorker",
    "retention_worker_runtime",
]
