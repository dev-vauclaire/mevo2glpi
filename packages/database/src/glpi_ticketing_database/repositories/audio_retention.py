"""Repository dedicated to the ``AudioRetention`` model."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy.orm import Session

from ..models import (
    AudioRetention,
    DeletionReason,
    RetentionPolicy,
    RetentionStatus,
)
from ._validation import (
    validate_aware_datetime,
    validate_future_datetime,
    validate_uuid,
)


class RetentionNotFoundError(LookupError):
    """Report an operation requested for an unknown retention record."""


class InvalidRetentionTransitionError(RuntimeError):
    """Report a retention transition incompatible with its current state."""


@dataclass(frozen=True, slots=True, kw_only=True)
class AudioRetentionSnapshot:
    """Immutable, typed view of one persisted audio-retention policy."""

    job_uuid: UUID
    policy: RetentionPolicy
    status: RetentionStatus
    hard_delete_at: datetime | None
    next_check_at: datetime | None
    last_checked_at: datetime | None
    last_glpi_status: str | None
    check_attempt_count: int
    consecutive_retry_count: int
    deletion_reason: DeletionReason | None
    deletion_started_at: datetime | None
    audio_deleted_at: datetime | None
    last_error_category: str | None
    last_error_code: str | None
    created_at: datetime
    updated_at: datetime


class AudioRetentionRepository:
    """Create or reset an audio retention schedule without touching its job."""

    def __init__(self, session: Session) -> None:
        if not isinstance(session, Session):
            raise TypeError("session must be a SQLAlchemy Session")
        self._session = session

    def activate(
        self,
        job_uuid: UUID,
        *,
        policy: RetentionPolicy,
        hard_delete_at: datetime,
        next_check_at: datetime | None,
        now: datetime,
    ) -> None:
        """Create or reset the schedule associated with one job UUID."""
        validate_uuid(job_uuid, "job_uuid")
        if not isinstance(policy, RetentionPolicy):
            raise TypeError("policy must be a RetentionPolicy")
        now = validate_aware_datetime(now, "now")
        hard_delete_at = validate_future_datetime(
            hard_delete_at,
            now=now,
            field_name="hard_delete_at",
        )
        if next_check_at is not None:
            next_check_at = validate_future_datetime(
                next_check_at,
                now=now,
                field_name="next_check_at",
            )
            if next_check_at > hard_delete_at:
                raise ValueError("next_check_at must not be later than hard_delete_at")
        if policy is RetentionPolicy.TICKET_OR_DEADLINE and next_check_at is None:
            raise ValueError("ticket_or_deadline retention requires next_check_at")
        if policy is RetentionPolicy.DEADLINE_ONLY and next_check_at is not None:
            raise ValueError("deadline_only retention does not accept next_check_at")

        retention = self._session.get(
            AudioRetention,
            job_uuid,
            with_for_update=True,
        )
        if retention is None:
            retention = AudioRetention(
                job_uuid=job_uuid,
                policy=policy.value,
                status=RetentionStatus.SCHEDULED.value,
                hard_delete_at=hard_delete_at,
                next_check_at=next_check_at,
                check_attempt_count=0,
                consecutive_retry_count=0,
                created_at=now,
                updated_at=now,
            )
            self._session.add(retention)
        else:
            retention.policy = policy.value
            retention.status = RetentionStatus.SCHEDULED.value
            retention.hard_delete_at = hard_delete_at
            retention.next_check_at = next_check_at
            retention.last_checked_at = None
            retention.last_glpi_status = None
            retention.consecutive_retry_count = 0
            retention.deletion_reason = None
            retention.deletion_started_at = None
            retention.audio_deleted_at = None
            retention.last_error_category = None
            retention.last_error_code = None
            retention.updated_at = now

        self._session.flush()

    def get_by_job_uuid(self, job_uuid: UUID) -> AudioRetentionSnapshot | None:
        """Return the retention record associated with a job UUID."""
        validate_uuid(job_uuid, "job_uuid")
        retention = self._session.get(AudioRetention, job_uuid)
        return _to_snapshot(retention) if retention is not None else None

    def deactivate_for_operator_retry(
        self,
        job_uuid: UUID,
        *,
        now: datetime,
    ) -> AudioRetentionSnapshot:
        """Deactivate a terminal job's retention before an operator retry."""
        validate_uuid(job_uuid, "job_uuid")
        now = validate_aware_datetime(now, "now")
        retention = self._session.get(
            AudioRetention,
            job_uuid,
            with_for_update=True,
        )
        if retention is None:
            raise RetentionNotFoundError(f"retention for job {job_uuid} does not exist")
        if retention.status not in (
            RetentionStatus.SCHEDULED.value,
            RetentionStatus.BLOCKED.value,
            RetentionStatus.RETRY_SCHEDULED.value,
        ):
            raise InvalidRetentionTransitionError(
                f"retention for job {job_uuid} cannot be deactivated from "
                f"status {retention.status}"
            )

        retention.status = RetentionStatus.INACTIVE.value
        retention.hard_delete_at = None
        retention.next_check_at = None
        retention.last_checked_at = None
        retention.last_glpi_status = None
        retention.consecutive_retry_count = 0
        retention.deletion_reason = None
        retention.deletion_started_at = None
        retention.audio_deleted_at = None
        retention.last_error_category = None
        retention.last_error_code = None
        retention.updated_at = now
        self._session.flush()
        return _to_snapshot(retention)


def _to_snapshot(retention: AudioRetention) -> AudioRetentionSnapshot:
    return AudioRetentionSnapshot(
        job_uuid=retention.job_uuid,
        policy=RetentionPolicy(retention.policy),
        status=RetentionStatus(retention.status),
        hard_delete_at=retention.hard_delete_at,
        next_check_at=retention.next_check_at,
        last_checked_at=retention.last_checked_at,
        last_glpi_status=retention.last_glpi_status,
        check_attempt_count=retention.check_attempt_count,
        consecutive_retry_count=retention.consecutive_retry_count,
        deletion_reason=(
            DeletionReason(retention.deletion_reason)
            if retention.deletion_reason is not None
            else None
        ),
        deletion_started_at=retention.deletion_started_at,
        audio_deleted_at=retention.audio_deleted_at,
        last_error_category=retention.last_error_category,
        last_error_code=retention.last_error_code,
        created_at=retention.created_at,
        updated_at=retention.updated_at,
    )


__all__ = [
    "AudioRetentionRepository",
    "AudioRetentionSnapshot",
    "InvalidRetentionTransitionError",
    "RetentionNotFoundError",
]
