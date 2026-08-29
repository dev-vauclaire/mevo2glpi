"""Persistent runtime heartbeat reporting for the automation worker."""

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

from ..worker import (
    BlockedOutcome,
    CompletedOutcome,
    FailedOutcome,
    IdleOutcome,
    InterruptedOutcome,
    RetryOutcome,
    RunOutcome,
    SystemBlockedOutcome,
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True)
class RuntimeHeartbeatReporter:
    """Persist process health while retaining dependency failures across idle runs.

    An idle iteration does not prove that an unavailable dependency recovered.
    Consequently, ``DEGRADED`` and ``BLOCKED`` remain sticky until the worker
    reaches a real job boundary.
    """

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
    _current_job_uuid: UUID | None = field(init=False, default=None)
    _last_recorded_monotonic: float | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        self.heartbeat_interval_seconds = _positive_finite_seconds(
            self.heartbeat_interval_seconds,
            "heartbeat_interval_seconds",
        )
        if not callable(self.now):
            raise TypeError("now must be callable")
        if not callable(self.monotonic):
            raise TypeError("monotonic must be callable")

    @property
    def state(self) -> WorkerRuntimeState:
        """Return the last state selected from an observable boundary."""
        return self._state

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
            self._current_job_uuid = None
        self._record(state)

    def start_job(self, audio_uuid: UUID) -> None:
        """Publish the job acquired after its claiming transaction completed."""
        if not isinstance(audio_uuid, UUID):
            raise TypeError("audio_uuid must be a UUID")
        self._current_job_uuid = audio_uuid
        self._record(self._state)

    def pulse_if_due(self) -> bool:
        """Refresh the current snapshot when the monotonic interval elapsed."""
        current_monotonic = self._read_monotonic()
        previous_monotonic = self._last_recorded_monotonic
        if previous_monotonic is not None:
            elapsed = current_monotonic - previous_monotonic
            if elapsed < 0:
                raise ValueError("monotonic must not move backwards")
            if elapsed < self.heartbeat_interval_seconds:
                return False

        self._record(self._state, recorded_monotonic=current_monotonic)
        return True

    def report_outcome(self, outcome: RunOutcome) -> None:
        """Translate a sanitized worker outcome into one runtime heartbeat."""
        current_job_uuid: UUID | None = None
        if isinstance(outcome, RetryOutcome):
            self._state = WorkerRuntimeState.DEGRADED
            self._last_error = (
                outcome.failure.category,
                outcome.failure.technical_code,
            )
        elif isinstance(outcome, SystemBlockedOutcome):
            self._state = WorkerRuntimeState.BLOCKED
            current_job_uuid = outcome.audio_uuid
            self._last_error = (
                outcome.failure.category,
                outcome.failure.technical_code,
            )
        elif isinstance(outcome, IdleOutcome):
            # Preserve the state and sanitized error until a job boundary
            # demonstrates recovery.
            self._current_job_uuid = None
            self.pulse_if_due()
            return
        elif isinstance(
            outcome,
            CompletedOutcome | FailedOutcome | BlockedOutcome | InterruptedOutcome,
        ):
            self._state = WorkerRuntimeState.RUNNING
            if isinstance(outcome, InterruptedOutcome):
                current_job_uuid = outcome.audio_uuid
            self._last_error = None
        else:  # pragma: no cover - RunOutcome is exhaustively typed
            raise AssertionError("unsupported worker outcome")

        self._current_job_uuid = current_job_uuid
        self._record(self._state)

    def _record(
        self,
        state: WorkerRuntimeState,
        *,
        recorded_monotonic: float | None = None,
    ) -> None:
        if recorded_monotonic is None:
            recorded_monotonic = self._read_monotonic()
        session = self.advisory_lock.open_session()
        try:
            with session.begin():
                WorkerRuntimeStatusRepository(session).record_heartbeat(
                    service_name=WorkerServiceName.AUTOMATION,
                    instance_id=self.instance_id,
                    state=state,
                    now=self.now(),
                    current_job_uuid=self._current_job_uuid,
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


def _positive_finite_seconds(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{field_name} must be a number")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized <= 0:
        raise ValueError(f"{field_name} must be finite and greater than zero")
    return normalized


__all__ = ["RuntimeHeartbeatReporter"]
