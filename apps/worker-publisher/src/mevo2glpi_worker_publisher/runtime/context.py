"""Runtime ownership for the singleton publisher process."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Protocol

import httpx2
from sqlalchemy.engine import Engine

from mevo2glpi_common.cooperative_wait import wait_with_periodic_checkpoint
from mevo2glpi_database import (
    AdvisoryLock,
    WorkerRuntimeState,
    WorkerServiceName,
    create_database_engine,
    try_acquire_advisory_lock,
)

from ..composition import build_publisher_worker
from ..publisher import PublisherRunOutcome, PublisherWorkerProtocol
from ..settings import PublisherSettings
from .heartbeat import PublisherRuntimeHeartbeatReporter

LOGGER = logging.getLogger(__name__)


def _never_stop() -> bool:
    return False


class PublisherAlreadyRunningError(RuntimeError):
    """Report that another PostgreSQL session owns the publisher lock."""


class RuntimePublisherWorkerProtocol(PublisherWorkerProtocol, Protocol):
    """Publisher handle exposing its cooperative runtime hooks."""

    def checkpoint(self) -> None: ...

    def wait(self, seconds: float) -> None: ...


@dataclass(slots=True)
class RuntimeReportingPublisherWorker:
    """Fence each iteration and persist its sanitized outcome."""

    worker: PublisherWorkerProtocol
    heartbeat: PublisherRuntimeHeartbeatReporter
    ownership_guard: Callable[[], None]
    checkpoint_callback: Callable[[], None]
    wait_callback: Callable[[float], None]

    def checkpoint(self) -> None:
        self.checkpoint_callback()

    def wait(self, seconds: float) -> None:
        self.wait_callback(seconds)

    def run_once(self) -> PublisherRunOutcome:
        self.checkpoint()
        outcome = self.worker.run_once()
        self.ownership_guard()
        self.heartbeat.report_outcome(outcome)
        return outcome


@dataclass(slots=True)
class PublisherRuntimeContext(AbstractContextManager[RuntimePublisherWorkerProtocol]):
    """Own the engine, singleton lock, HTTP client and heartbeat."""

    settings: PublisherSettings
    http_client_factory: Callable[[], httpx2.Client] = httpx2.Client
    wait_once: Callable[[float], object] = time.sleep
    should_stop: Callable[[], bool] = _never_stop
    _engine: Engine | None = field(init=False, default=None)
    _advisory_lock: AdvisoryLock | None = field(init=False, default=None)
    _http_client: httpx2.Client | None = field(init=False, default=None)
    _heartbeat: PublisherRuntimeHeartbeatReporter | None = field(
        init=False,
        default=None,
    )
    _closed: bool = field(init=False, default=False)

    def __enter__(self) -> RuntimePublisherWorkerProtocol:
        if self._engine is not None:
            raise RuntimeError("publisher runtime context cannot be entered twice")
        self._engine = create_database_engine(
            self.settings.database_url.get_secret_value(),
            connect_timeout_seconds=self.settings.database_connect_timeout_seconds,
        )
        try:
            self._advisory_lock = try_acquire_advisory_lock(
                self._engine,
                WorkerServiceName.PUBLISHER,
            )
            if self._advisory_lock is None:
                raise PublisherAlreadyRunningError(
                    "another publisher worker owns the singleton lock"
                )
            self._heartbeat = PublisherRuntimeHeartbeatReporter(
                self._advisory_lock,
                heartbeat_interval_seconds=(
                    self.settings.publisher_heartbeat_interval_seconds
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
                        self.settings.publisher_heartbeat_interval_seconds
                    ),
                    wait_once=self.wait_once,
                    should_stop=self.should_stop,
                    checkpoint=runtime_checkpoint,
                )

            worker = build_publisher_worker(
                self.settings,
                http_client=self._http_client,
                session_factory=self._advisory_lock.open_session,
                ownership_guard=runtime_checkpoint,
                on_ingestion_started=self._heartbeat.start_ingestion,
            )
            self._heartbeat.record_state(WorkerRuntimeState.RUNNING)
            return RuntimeReportingPublisherWorker(
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
        """Release owned resources once and in reverse dependency order."""
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
                    "publisher stopped heartbeat failed error_type=%s",
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


def publisher_runtime(
    settings: PublisherSettings,
    *,
    http_client_factory: Callable[[], httpx2.Client] = httpx2.Client,
    wait_once: Callable[[float], object] = time.sleep,
    should_stop: Callable[[], bool] = _never_stop,
) -> PublisherRuntimeContext:
    """Create the resource context for one composed publisher."""
    return PublisherRuntimeContext(
        settings=settings,
        http_client_factory=http_client_factory,
        wait_once=wait_once,
        should_stop=should_stop,
    )


__all__ = [
    "PublisherAlreadyRunningError",
    "PublisherRuntimeContext",
    "RuntimePublisherWorkerProtocol",
    "RuntimeReportingPublisherWorker",
    "publisher_runtime",
]
