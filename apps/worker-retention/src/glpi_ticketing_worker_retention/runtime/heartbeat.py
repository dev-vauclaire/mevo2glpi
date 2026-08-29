"""Persistent runtime heartbeat reporting for the retention worker."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

from glpi_ticketing_database import (
    AdvisoryLock,
    DeletionReason,
    WorkerRuntimeState,
    WorkerRuntimeStatusRepository,
    WorkerServiceName,
)

from ..retention import (
    AudioDeletedOutcome,
    DependencyCondition,
    DependencyWaitingOutcome,
    GlpiCheckRescheduledOutcome,
    RetentionBlockedOutcome,
    RetentionOperation,
    RetentionRunOutcome,
    RetryScheduledOutcome,
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
    instance_id: UUID = field(default_factory=uuid4)
    now: Callable[[], datetime] = _utc_now
    _dependencies: dict[RetentionOperation, _DependencyFailure] = field(
        init=False,
        default_factory=dict,
    )

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
        self._record(state)

    def report_outcome(self, outcome: RetentionRunOutcome) -> None:
        """Translate one sanitized worker outcome into a heartbeat."""
        current_job_uuid = getattr(outcome, "job_uuid", None)
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

        state, failure = self._effective_state()
        self._record(
            state,
            current_job_uuid=current_job_uuid,
            failure=failure,
        )

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
        current_job_uuid: UUID | None = None,
        failure: _DependencyFailure | None = None,
    ) -> None:
        session = self.advisory_lock.open_session()
        try:
            with session.begin():
                WorkerRuntimeStatusRepository(session).record_heartbeat(
                    service_name=WorkerServiceName.RETENTION,
                    instance_id=self.instance_id,
                    state=state,
                    now=self.now(),
                    current_job_uuid=current_job_uuid,
                    error_category=failure.category if failure is not None else None,
                    error_code=(
                        failure.technical_code if failure is not None else None
                    ),
                )
        finally:
            session.close()


__all__ = ["RetentionRuntimeHeartbeatReporter"]
