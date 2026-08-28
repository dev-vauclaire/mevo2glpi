"""Repository dedicated to the ``AudioRetention`` model."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy.orm import Session

from ..models import AudioRetention, RetentionPolicy, RetentionStatus
from ._validation import (
    validate_aware_datetime,
    validate_future_datetime,
    validate_uuid,
)


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


__all__ = ["AudioRetentionRepository"]
