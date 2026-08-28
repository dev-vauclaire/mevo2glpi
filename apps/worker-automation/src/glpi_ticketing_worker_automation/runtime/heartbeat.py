"""Persistent runtime heartbeat reporting for the automation worker."""

from __future__ import annotations

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
    instance_id: UUID = field(default_factory=uuid4)
    now: Callable[[], datetime] = _utc_now
    _state: WorkerRuntimeState = field(
        init=False,
        default=WorkerRuntimeState.STARTING,
    )
    _last_error: tuple[str, str] | None = field(init=False, default=None)

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
        self._record(state)

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
            pass
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

        self._record(self._state, current_job_uuid=current_job_uuid)

    def _record(
        self,
        state: WorkerRuntimeState,
        *,
        current_job_uuid: UUID | None = None,
    ) -> None:
        session = self.advisory_lock.open_session()
        try:
            with session.begin():
                WorkerRuntimeStatusRepository(session).record_heartbeat(
                    service_name=WorkerServiceName.AUTOMATION,
                    instance_id=self.instance_id,
                    state=state,
                    now=self.now(),
                    current_job_uuid=current_job_uuid,
                    error_category=(
                        self._last_error[0] if self._last_error is not None else None
                    ),
                    error_code=(
                        self._last_error[1] if self._last_error is not None else None
                    ),
                )
        finally:
            session.close()


__all__ = ["RuntimeHeartbeatReporter"]
