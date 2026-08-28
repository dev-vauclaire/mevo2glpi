"""Repository dedicated to the ``WorkerRuntimeStatus`` model."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy.orm import Session

from ..models import (
    WorkerRuntimeState,
    WorkerRuntimeStatus,
    WorkerServiceName,
)
from ._validation import (
    validate_aware_datetime,
    validate_optional_error,
    validate_uuid,
)


class StaleWorkerInstanceError(RuntimeError):
    """Reject a heartbeat emitted by a superseded worker instance."""


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkerRuntimeSnapshot:
    """Immutable view of one singleton worker heartbeat."""

    service_name: WorkerServiceName
    instance_id: UUID
    state: WorkerRuntimeState
    last_heartbeat_at: datetime
    current_job_uuid: UUID | None
    last_error_category: str | None
    last_error_code: str | None
    updated_at: datetime


class WorkerRuntimeStatusRepository:
    """Persist singleton worker heartbeats in a caller-owned transaction."""

    def __init__(self, session: Session) -> None:
        if not isinstance(session, Session):
            raise TypeError("session must be a SQLAlchemy Session")
        self._session = session

    def record_heartbeat(
        self,
        *,
        service_name: WorkerServiceName,
        instance_id: UUID,
        state: WorkerRuntimeState,
        now: datetime,
        current_job_uuid: UUID | None = None,
        error_category: str | None = None,
        error_code: str | None = None,
    ) -> WorkerRuntimeSnapshot:
        """Insert or replace the latest sanitized heartbeat for one service."""
        if not isinstance(service_name, WorkerServiceName):
            raise TypeError("service_name must be a WorkerServiceName")
        validate_uuid(instance_id, "instance_id")
        if not isinstance(state, WorkerRuntimeState):
            raise TypeError("state must be a WorkerRuntimeState")
        now = validate_aware_datetime(now, "now")
        if current_job_uuid is not None:
            validate_uuid(current_job_uuid, "current_job_uuid")
        error_category, error_code = validate_optional_error(
            error_category,
            error_code,
        )
        if state in {WorkerRuntimeState.DEGRADED, WorkerRuntimeState.BLOCKED}:
            if error_category is None:
                raise ValueError("degraded and blocked heartbeats require an error")
        elif error_category is not None:
            raise ValueError(
                "only degraded and blocked heartbeats may contain an error"
            )

        runtime = self._session.get(
            WorkerRuntimeStatus,
            service_name.value,
            with_for_update=True,
        )
        if runtime is None:
            runtime = WorkerRuntimeStatus(
                service_name=service_name.value,
                instance_id=instance_id,
                state=state.value,
                last_heartbeat_at=now,
                current_job_uuid=current_job_uuid,
                last_error_category=error_category,
                last_error_code=error_code,
                updated_at=now,
            )
            self._session.add(runtime)
        else:
            if (
                runtime.instance_id != instance_id
                and state is not WorkerRuntimeState.STARTING
            ):
                raise StaleWorkerInstanceError(
                    "worker heartbeat belongs to a superseded instance"
                )
            runtime.instance_id = instance_id
            runtime.state = state.value
            runtime.last_heartbeat_at = now
            runtime.current_job_uuid = current_job_uuid
            runtime.last_error_category = error_category
            runtime.last_error_code = error_code
            runtime.updated_at = now

        self._session.flush()
        return _to_snapshot(runtime)


def _to_snapshot(runtime: WorkerRuntimeStatus) -> WorkerRuntimeSnapshot:
    return WorkerRuntimeSnapshot(
        service_name=WorkerServiceName(runtime.service_name),
        instance_id=runtime.instance_id,
        state=WorkerRuntimeState(runtime.state),
        last_heartbeat_at=runtime.last_heartbeat_at,
        current_job_uuid=runtime.current_job_uuid,
        last_error_category=runtime.last_error_category,
        last_error_code=runtime.last_error_code,
        updated_at=runtime.updated_at,
    )


__all__ = [
    "StaleWorkerInstanceError",
    "WorkerRuntimeSnapshot",
    "WorkerRuntimeStatusRepository",
]
