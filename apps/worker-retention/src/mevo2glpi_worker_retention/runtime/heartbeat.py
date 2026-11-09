"""Persistent runtime heartbeat reporting for the retention worker."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import assert_never
from uuid import UUID, uuid4

from mevo2glpi_database import (
    AdvisoryLock,
    DeletionReason,
    WorkerRuntimeState,
    WorkerRuntimeStatusRepository,
    WorkerServiceName,
)

from ..retention import (
    AudioDeletedOutcome,
    CheckRecoveredOutcome,
    DependencyCondition,
    DependencyWaitingOutcome,
    GlpiCheckRescheduledOutcome,
    IdleOutcome,
    RejectedIngestionsPurgedOutcome,
    RetentionBlockedOutcome,
    RetentionOperation,
    RetentionRunOutcome,
    RetryScheduledOutcome,
    StaleCandidateOutcome,
    SystemBlockedOutcome,
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class _DependencyFailure:
    state: WorkerRuntimeState
    category: str
    technical_code: str


@dataclass(slots=True)
class RetentionRuntimeHeartbeatReporter:
    """Persist health while retaining independent dependency failures."""

    advisory_lock: AdvisoryLock
    heartbeat_interval_seconds: float = 10.0
    instance_id: UUID = field(default_factory=uuid4)
    now: Callable[[], datetime] = _utc_now
    monotonic: Callable[[], float] = time.monotonic
    _dependencies: dict[RetentionOperation, _DependencyFailure] = field(
        init=False,
        default_factory=dict,
    )
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

    def record_state(self, state: WorkerRuntimeState) -> None:
        """Record an explicit process-lifecycle state."""
        if not isinstance(state, WorkerRuntimeState):
            raise TypeError("state must be a WorkerRuntimeState")
        if state in {
            WorkerRuntimeState.STARTING,
            WorkerRuntimeState.RUNNING,
            WorkerRuntimeState.STOPPED,
        }:
            self._dependencies.clear()
            self._current_job_uuid = None
        self._record(state)

    def start_job(self, job_uuid: UUID) -> None:
        """Publish the retention candidate selected for the current iteration."""
        if not isinstance(job_uuid, UUID):
            raise TypeError("job_uuid must be a UUID")
        self._current_job_uuid = job_uuid
        state, failure = self._effective_state()
        self._record(state, failure=failure)

    def report_outcome(self, outcome: RetentionRunOutcome) -> None:
        """Translate one sanitized worker outcome into a heartbeat."""
        if isinstance(outcome, IdleOutcome):
            self._current_job_uuid = None
            self.pulse_if_due()
            return

        if isinstance(outcome, RetryScheduledOutcome):
            self._dependencies[outcome.operation] = _DependencyFailure(
                state=WorkerRuntimeState.DEGRADED,
                category=outcome.failure.category,
                technical_code=outcome.failure.technical_code,
            )
        elif isinstance(outcome, SystemBlockedOutcome):
            self._dependencies[outcome.operation] = _DependencyFailure(
                state=WorkerRuntimeState.BLOCKED,
                category=outcome.failure.category,
                technical_code=outcome.failure.technical_code,
            )
        elif isinstance(outcome, DependencyWaitingOutcome):
            self._dependencies[RetentionOperation.GLPI_CHECK] = _DependencyFailure(
                state=(
                    WorkerRuntimeState.DEGRADED
                    if outcome.condition is DependencyCondition.DEGRADED
                    else WorkerRuntimeState.BLOCKED
                ),
                category=outcome.failure.category,
                technical_code=outcome.failure.technical_code,
            )
        elif isinstance(outcome, GlpiCheckRescheduledOutcome):
            self._dependencies.pop(RetentionOperation.GLPI_CHECK, None)
        elif isinstance(outcome, RetentionBlockedOutcome):
            self._dependencies.pop(outcome.operation, None)
        elif isinstance(outcome, AudioDeletedOutcome):
            self._dependencies.pop(RetentionOperation.AUDIO_PURGE, None)
            if outcome.reason is DeletionReason.TICKET_CLOSED:
                self._dependencies.pop(RetentionOperation.GLPI_CHECK, None)
        elif isinstance(
            outcome,
            CheckRecoveredOutcome
            | RejectedIngestionsPurgedOutcome
            | StaleCandidateOutcome,
        ):
            pass
        else:
            assert_never(outcome)

        self._current_job_uuid = None
        state, failure = self._effective_state()
        self._record(
            state,
            failure=failure,
        )

    def pulse_if_due(self) -> bool:
        """Refresh the current sticky health state when its cadence is due."""
        current_monotonic = self._read_monotonic()
        if self._last_recorded_monotonic is not None:
            elapsed = current_monotonic - self._last_recorded_monotonic
            if elapsed < 0:
                raise ValueError("monotonic must not move backwards")
            if elapsed < self.heartbeat_interval_seconds:
                return False

        state, failure = self._effective_state()
        self._record(
            state,
            failure=failure,
            recorded_monotonic=current_monotonic,
        )
        return True

    def _effective_state(
        self,
    ) -> tuple[WorkerRuntimeState, _DependencyFailure | None]:
        for state in (WorkerRuntimeState.BLOCKED, WorkerRuntimeState.DEGRADED):
            for operation in (
                RetentionOperation.GLPI_CHECK,
                RetentionOperation.AUDIO_PURGE,
            ):
                failure = self._dependencies.get(operation)
                if failure is not None and failure.state is state:
                    return state, failure
        return WorkerRuntimeState.RUNNING, None

    def _record(
        self,
        state: WorkerRuntimeState,
        *,
        failure: _DependencyFailure | None = None,
        recorded_monotonic: float | None = None,
    ) -> None:
        if recorded_monotonic is None:
            recorded_monotonic = self._read_monotonic()
        session = self.advisory_lock.open_session()
        try:
            with session.begin():
                WorkerRuntimeStatusRepository(session).record_heartbeat(
                    service_name=WorkerServiceName.RETENTION,
                    instance_id=self.instance_id,
                    state=state,
                    now=self.now(),
                    current_job_uuid=self._current_job_uuid,
                    error_category=failure.category if failure is not None else None,
                    error_code=(
                        failure.technical_code if failure is not None else None
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


__all__ = ["RetentionRuntimeHeartbeatReporter"]
