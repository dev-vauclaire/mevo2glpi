"""Runtime resource ownership for one automation worker process."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Protocol

import httpx
from sqlalchemy.engine import Engine

from mevo2glpi_common.cooperative_wait import wait_with_periodic_checkpoint
from mevo2glpi_database import (
    AdvisoryLock,
    WorkerRuntimeState,
    WorkerServiceName,
    create_database_engine,
    try_acquire_advisory_lock,
)

from ..composition import build_automation_worker
from ..settings import WorkerSettings
from ..worker import AutomationWorkerProtocol, RunOutcome
from .heartbeat import RuntimeHeartbeatReporter

LOGGER = logging.getLogger(__name__)


def _never_stop() -> bool:
    return False


class WorkerAlreadyRunningError(RuntimeError):
    """Report that another PostgreSQL session owns the singleton lock."""


class RuntimeAutomationWorkerProtocol(AutomationWorkerProtocol, Protocol):
    """Worker process handle exposing its fenced cooperative runtime hooks."""

    def checkpoint(self) -> None: ...

    def wait(self, seconds: float) -> None: ...


@dataclass(slots=True)
class RuntimeReportingWorker:
    """Guard every iteration with the lock and publish its outcome."""

    worker: AutomationWorkerProtocol
    heartbeat: RuntimeHeartbeatReporter
    ownership_guard: Callable[[], None]
    checkpoint_callback: Callable[[], None]
    wait_callback: Callable[[float], None]

    def checkpoint(self) -> None:
        """Prove singleton ownership before refreshing a due heartbeat."""
        self.checkpoint_callback()

    def wait(self, seconds: float) -> None:
        """Wait cooperatively while keeping ownership and liveness observable."""
        self.wait_callback(seconds)

    def run_once(self) -> RunOutcome:
        self.checkpoint()
        outcome = self.worker.run_once()
        # Do not emit the previous snapshot immediately before the outcome.
        # The outcome write owns the next heartbeat transaction; this guard
        # solely proves that the singleton lock still belongs to this process.
        self.ownership_guard()
        self.heartbeat.report_outcome(outcome)
        return outcome


@dataclass(slots=True)
class WorkerRuntimeContext(AbstractContextManager[RuntimeAutomationWorkerProtocol]):
    """Own the engine, singleton lock, HTTP client and heartbeat lifecycle."""

    settings: WorkerSettings
    http_client_factory: Callable[[], httpx.Client] = httpx.Client
    wait_once: Callable[[float], object] = time.sleep
    should_stop: Callable[[], bool] = _never_stop
    _engine: Engine | None = field(init=False, default=None)
    _advisory_lock: AdvisoryLock | None = field(init=False, default=None)
    _http_client: httpx.Client | None = field(init=False, default=None)
    _heartbeat: RuntimeHeartbeatReporter | None = field(init=False, default=None)
    _closed: bool = field(init=False, default=False)

    @property
    def engine(self) -> Engine | None:
        """Expose the owned engine for diagnostics without transferring ownership."""
        return self._engine

    @property
    def advisory_lock(self) -> AdvisoryLock | None:
        """Expose the singleton lock for diagnostics."""
        return self._advisory_lock

    @property
    def http_client(self) -> httpx.Client | None:
        """Expose the owned HTTP client for diagnostics."""
        return self._http_client

    def __enter__(self) -> RuntimeAutomationWorkerProtocol:
        if self._engine is not None:
            raise RuntimeError("worker runtime context cannot be entered twice")

        connect_timeout = self.settings.database_connect_timeout_seconds
        self._engine = create_database_engine(
            self.settings.database_url.get_secret_value(),
            connect_timeout_seconds=connect_timeout,
        )
        try:
            self._advisory_lock = try_acquire_advisory_lock(
                self._engine,
                WorkerServiceName.AUTOMATION,
            )
            if self._advisory_lock is None:
                raise WorkerAlreadyRunningError(
                    "another automation worker already owns the singleton lock"
                )

            self._heartbeat = RuntimeHeartbeatReporter(
                self._advisory_lock,
                heartbeat_interval_seconds=(
                    self.settings.worker_heartbeat_interval_seconds
                ),
            )
            self._heartbeat.record_state(WorkerRuntimeState.STARTING)
            self._http_client = self.http_client_factory()

            def runtime_checkpoint() -> None:
                assert self._advisory_lock is not None
                assert self._heartbeat is not None
                self._advisory_lock.ensure_held()
                self._heartbeat.pulse_if_due()

            def cooperative_wait(seconds: float) -> None:
                wait_with_periodic_checkpoint(
                    seconds,
                    checkpoint_interval_seconds=(
                        self.settings.worker_heartbeat_interval_seconds
                    ),
                    wait_once=self.wait_once,
                    should_stop=self.should_stop,
                    checkpoint=runtime_checkpoint,
                )

            worker = build_automation_worker(
                self.settings,
                http_client=self._http_client,
                session_factory=self._advisory_lock.open_session,
                ownership_guard=runtime_checkpoint,
                on_job_started=self._heartbeat.start_job,
            )
            self._heartbeat.record_state(WorkerRuntimeState.RUNNING)
            return RuntimeReportingWorker(
                worker=worker,
                heartbeat=self._heartbeat,
                ownership_guard=self._advisory_lock.ensure_held,
                checkpoint_callback=runtime_checkpoint,
                wait_callback=cooperative_wait,
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
                    "worker stopped heartbeat failed error_type=%s",
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


def worker_runtime(
    settings: WorkerSettings,
    *,
    http_client_factory: Callable[[], httpx.Client] = httpx.Client,
    wait_once: Callable[[float], object] = time.sleep,
    should_stop: Callable[[], bool] = _never_stop,
) -> WorkerRuntimeContext:
    """Create the context owning one fully composed automation worker."""
    return WorkerRuntimeContext(
        settings=settings,
        http_client_factory=http_client_factory,
        wait_once=wait_once,
        should_stop=should_stop,
    )


__all__ = [
    "RuntimeReportingWorker",
    "RuntimeAutomationWorkerProtocol",
    "WorkerAlreadyRunningError",
    "WorkerRuntimeContext",
    "worker_runtime",
]
