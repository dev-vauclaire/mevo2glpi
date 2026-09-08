"""Persistent runtime heartbeat reporting for the publisher worker."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

from glpi_ticketing_database import (
    AdvisoryLock,
    WorkerRuntimeState,
    WorkerRuntimeStatusRepository,
    WorkerServiceName,
)

from ..publisher import (
    BlockedOutcome,
    CompletedOutcome,
    DiscoveryRetryOutcome,
    IdleOutcome,
    PublisherRunOutcome,
    RejectedOutcome,
    RetryScheduledOutcome,
    SystemBlockedOutcome,
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True)
class PublisherRuntimeHeartbeatReporter:
    """Persist liveness and sticky degraded/blocked dependency state."""

    advisory_lock: AdvisoryLock
    heartbeat_interval_seconds: float = 10.0
    instance_id: UUID = field(default_factory=uuid4)
    now: Callable[[], datetime] = _utc_now
    monotonic: Callable[[], float] = time.monotonic
    _state: WorkerRuntimeState = field(
        init=False,
        default=WorkerRuntimeState.STARTING,
    )
    _last_error: tuple[str, str] | None = field(init=False, default=None)
    _current_ingestion_uuid: UUID | None = field(init=False, default=None)
    _last_recorded_monotonic: float | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        self.heartbeat_interval_seconds = _positive_seconds(
            self.heartbeat_interval_seconds,
            "heartbeat_interval_seconds",
        )
        if not callable(self.now):
            raise TypeError("now must be callable")
        if not callable(self.monotonic):
            raise TypeError("monotonic must be callable")

    def record_state(self, state: WorkerRuntimeState) -> None:
        """Record an explicit process-lifecycle state."""
        if not isinstance(state, WorkerRuntimeState):
            raise TypeError("state must be a WorkerRuntimeState")
        self._state = state
        if state in {
            WorkerRuntimeState.STARTING,
            WorkerRuntimeState.RUNNING,
            WorkerRuntimeState.STOPPED,
        }:
            self._last_error = None
            self._current_ingestion_uuid = None
        self._record()

    def start_ingestion(self, ingestion_uuid: UUID) -> None:
        """Expose the ingestion acquired after its claiming transaction."""
        if not isinstance(ingestion_uuid, UUID):
            raise TypeError("ingestion_uuid must be a UUID")
        self._current_ingestion_uuid = ingestion_uuid
        self._record()

    def report_outcome(self, outcome: PublisherRunOutcome) -> None:
        """Translate one sanitized publisher boundary to a heartbeat."""
        if isinstance(outcome, IdleOutcome):
            self._current_ingestion_uuid = None
            self.pulse_if_due()
            return
        if isinstance(outcome, DiscoveryRetryOutcome | RetryScheduledOutcome):
            self._state = WorkerRuntimeState.DEGRADED
            self._last_error = (
                outcome.failure.category,
                outcome.failure.technical_code,
            )
            self._current_ingestion_uuid = None
        elif isinstance(outcome, SystemBlockedOutcome):
            self._state = WorkerRuntimeState.BLOCKED
            self._last_error = (
                outcome.failure.category,
                outcome.failure.technical_code,
            )
            self._current_ingestion_uuid = outcome.ingestion_uuid
        elif isinstance(outcome, CompletedOutcome | RejectedOutcome | BlockedOutcome):
            self._state = WorkerRuntimeState.RUNNING
            self._last_error = None
            self._current_ingestion_uuid = None
        else:  # pragma: no cover - exhaustively typed
            raise AssertionError("unsupported publisher outcome")
        self._record()

    def pulse_if_due(self) -> bool:
        """Refresh the current state after the monotonic cadence elapsed."""
        current = self._read_monotonic()
        if self._last_recorded_monotonic is not None:
            elapsed = current - self._last_recorded_monotonic
            if elapsed < 0:
                raise ValueError("monotonic must not move backwards")
            if elapsed < self.heartbeat_interval_seconds:
                return False
        self._record(recorded_monotonic=current)
        return True

    def _record(self, *, recorded_monotonic: float | None = None) -> None:
        if recorded_monotonic is None:
            recorded_monotonic = self._read_monotonic()
        session = self.advisory_lock.open_session()
        try:
            with session.begin():
                WorkerRuntimeStatusRepository(session).record_heartbeat(
                    service_name=WorkerServiceName.PUBLISHER,
                    instance_id=self.instance_id,
                    state=self._state,
                    now=self.now(),
                    current_ingestion_uuid=self._current_ingestion_uuid,
                    error_category=(
                        self._last_error[0] if self._last_error is not None else None
                    ),
                    error_code=(
                        self._last_error[1] if self._last_error is not None else None
                    ),
                )
        finally:
            session.close()
        self._last_recorded_monotonic = recorded_monotonic

    def _read_monotonic(self) -> float:
        value = self.monotonic()
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise TypeError("monotonic must return a number")
        normalized = float(value)
        if not math.isfinite(normalized):
            raise ValueError("monotonic must return a finite number")
        return normalized


def _positive_seconds(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{field_name} must be a number")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized <= 0:
        raise ValueError(f"{field_name} must be finite and greater than zero")
    return normalized


__all__ = ["PublisherRuntimeHeartbeatReporter"]
