"""Repository dedicated to the ``AudioRetention`` model."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import Select, select
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
    validate_positive_integer,
    validate_safe_identifier,
    validate_uuid,
)


class RetentionNotFoundError(LookupError):
    """Report an operation requested for an unknown retention record."""


class InvalidRetentionTransitionError(RuntimeError):
    """Report a retention transition incompatible with its current state."""


class RetentionCandidateKind(StrEnum):
    """Reason why a retention row is the next one eligible for work."""

    RESUME_DELETION = "resume_deletion"
    RECOVER_INTERRUPTED_CHECK = "recover_interrupted_check"
    START_DELETION_AT_DEADLINE = "start_deletion_at_deadline"
    RESUME_DELETION_RETRY = "resume_deletion_retry"
    START_GLPI_CHECK = "start_glpi_check"


@dataclass(frozen=True, slots=True, kw_only=True)
class AudioRetentionSnapshot:
    """Immutable, typed view of one persisted audio-retention policy."""

    job_uuid: UUID
    policy: RetentionPolicy
    status: RetentionStatus
    hard_delete_at: datetime | None
    next_check_at: datetime | None
    last_checked_at: datetime | None
    last_glpi_status_id: int | None
    check_attempt_count: int
    consecutive_retry_count: int
    deletion_reason: DeletionReason | None
    deletion_started_at: datetime | None
    audio_deleted_at: datetime | None
    last_error_category: str | None
    last_error_code: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class RetentionCandidate:
    """Describe one non-locking retention candidate and why it is eligible."""

    kind: RetentionCandidateKind
    retention: AudioRetentionSnapshot


class AudioRetentionRepository:
    """Read and mutate only retention rows using a caller-owned transaction."""

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
        """Create a schedule or reactivate one made inactive by an operator."""
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
            if retention.status != RetentionStatus.INACTIVE.value:
                self._raise_invalid_status(retention, "activate it")
            retention.policy = policy.value
            retention.status = RetentionStatus.SCHEDULED.value
            retention.hard_delete_at = hard_delete_at
            retention.next_check_at = next_check_at
            retention.last_checked_at = None
            retention.last_glpi_status_id = None
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

    def find_next_candidate(self, now: datetime) -> RetentionCandidate | None:
        """Return the highest-priority eligible row without locking it.

        The caller must subsequently lock the job first, then invoke the matching
        transition. That transition locks and revalidates the retention row.
        """
        now = validate_aware_datetime(now, "now")
        searches = (
            (
                RetentionCandidateKind.RESUME_DELETION,
                self._deleting_candidate_statement(),
            ),
            (
                RetentionCandidateKind.RECOVER_INTERRUPTED_CHECK,
                self._checking_candidate_statement(),
            ),
            (
                RetentionCandidateKind.START_DELETION_AT_DEADLINE,
                self._first_deadline_candidate_statement(now),
            ),
            (
                RetentionCandidateKind.RESUME_DELETION_RETRY,
                self._deletion_retry_candidate_statement(now),
            ),
            (
                RetentionCandidateKind.START_GLPI_CHECK,
                self._glpi_check_candidate_statement(now),
            ),
        )
        for kind, statement in searches:
            retention = self._session.scalars(statement).first()
            if retention is not None:
                return RetentionCandidate(kind=kind, retention=_to_snapshot(retention))
        return None

    def recover_interrupted_check(
        self,
        job_uuid: UUID,
        *,
        now: datetime,
    ) -> AudioRetentionSnapshot:
        """Return an interrupted GLPI check to an immediately due schedule."""
        now = validate_aware_datetime(now, "now")
        retention = self._lock(job_uuid)
        self._require_checking(retention)
        if retention.hard_delete_at is None:
            raise InvalidRetentionTransitionError(
                f"retention for job {job_uuid} has no deletion deadline"
            )

        retention.status = RetentionStatus.SCHEDULED.value
        retention.next_check_at = min(now, retention.hard_delete_at)
        retention.updated_at = now
        self._clear_error(retention)
        self._session.flush()
        return _to_snapshot(retention)

    def start_glpi_check(
        self,
        job_uuid: UUID,
        *,
        now: datetime,
    ) -> AudioRetentionSnapshot:
        """Claim one due GLPI check after locking and revalidating its row."""
        now = validate_aware_datetime(now, "now")
        retention = self._lock(job_uuid)
        if retention.policy != RetentionPolicy.TICKET_OR_DEADLINE.value:
            raise InvalidRetentionTransitionError(
                f"retention for job {job_uuid} does not use GLPI checks"
            )
        if retention.status not in (
            RetentionStatus.SCHEDULED.value,
            RetentionStatus.RETRY_SCHEDULED.value,
        ):
            self._raise_invalid_status(retention, "start a GLPI check")
        if retention.deletion_started_at is not None:
            raise InvalidRetentionTransitionError(
                f"retention for job {job_uuid} has already started deletion"
            )
        if retention.next_check_at is None or retention.next_check_at > now:
            raise InvalidRetentionTransitionError(
                f"retention for job {job_uuid} is not due for a GLPI check"
            )
        if retention.hard_delete_at is None or retention.hard_delete_at <= now:
            raise InvalidRetentionTransitionError(
                f"retention for job {job_uuid} has reached its deletion deadline"
            )

        retention.status = RetentionStatus.CHECKING.value
        retention.next_check_at = None
        retention.last_checked_at = now
        retention.check_attempt_count += 1
        retention.updated_at = now
        self._clear_error(retention)
        self._session.flush()
        return _to_snapshot(retention)

    def reschedule_after_glpi_check(
        self,
        job_uuid: UUID,
        *,
        glpi_status_id: int,
        next_check_at: datetime,
        now: datetime,
    ) -> AudioRetentionSnapshot:
        """Persist a successful non-terminal GLPI check and its next schedule."""
        glpi_status_id = validate_positive_integer(
            glpi_status_id,
            "glpi_status_id",
        )
        now = validate_aware_datetime(now, "now")
        next_check_at = validate_future_datetime(
            next_check_at,
            now=now,
            field_name="next_check_at",
        )
        retention = self._lock(job_uuid)
        self._require_checking(retention)
        if (
            retention.hard_delete_at is not None
            and next_check_at > retention.hard_delete_at
        ):
            raise ValueError("next_check_at must not be later than hard_delete_at")

        retention.status = RetentionStatus.SCHEDULED.value
        retention.next_check_at = next_check_at
        retention.last_glpi_status_id = glpi_status_id
        retention.consecutive_retry_count = 0
        retention.updated_at = now
        self._clear_error(retention)
        self._session.flush()
        return _to_snapshot(retention)

    def schedule_retry(
        self,
        job_uuid: UUID,
        *,
        error_category: str,
        error_code: str,
        next_check_at: datetime,
        now: datetime,
    ) -> AudioRetentionSnapshot:
        """Schedule a retry for a GLPI check or an in-progress deletion."""
        error_category = validate_safe_identifier(error_category, "error_category")
        error_code = validate_safe_identifier(error_code, "error_code")
        now = validate_aware_datetime(now, "now")
        next_check_at = validate_future_datetime(
            next_check_at,
            now=now,
            field_name="next_check_at",
        )
        retention = self._lock(job_uuid)
        if retention.status == RetentionStatus.CHECKING.value:
            self._require_checking(retention)
        elif retention.status == RetentionStatus.DELETING.value:
            self._require_deletion_context(retention)
        else:
            self._raise_invalid_status(retention, "schedule a retry")
        if (
            retention.deletion_started_at is None
            and retention.hard_delete_at is not None
            and next_check_at > retention.hard_delete_at
        ):
            raise ValueError("next_check_at must not be later than hard_delete_at")

        retention.status = RetentionStatus.RETRY_SCHEDULED.value
        retention.next_check_at = next_check_at
        retention.consecutive_retry_count += 1
        retention.last_error_category = error_category
        retention.last_error_code = error_code
        retention.updated_at = now
        self._session.flush()
        return _to_snapshot(retention)

    def mark_blocked(
        self,
        job_uuid: UUID,
        *,
        error_category: str,
        error_code: str,
        now: datetime,
    ) -> AudioRetentionSnapshot:
        """Block an ambiguous GLPI check or deletion without losing its context."""
        error_category = validate_safe_identifier(error_category, "error_category")
        error_code = validate_safe_identifier(error_code, "error_code")
        now = validate_aware_datetime(now, "now")
        retention = self._lock(job_uuid)
        if retention.status == RetentionStatus.CHECKING.value:
            self._require_checking(retention)
        elif retention.status == RetentionStatus.DELETING.value:
            self._require_deletion_context(retention)
        else:
            self._raise_invalid_status(retention, "mark it blocked")

        retention.status = RetentionStatus.BLOCKED.value
        retention.next_check_at = None
        retention.last_error_category = error_category
        retention.last_error_code = error_code
        retention.updated_at = now
        self._session.flush()
        return _to_snapshot(retention)

    def start_deletion(
        self,
        job_uuid: UUID,
        *,
        reason: DeletionReason,
        now: datetime,
        last_glpi_status_id: int | None = None,
    ) -> AudioRetentionSnapshot:
        """Persist the immutable deletion context before touching audio bytes."""
        if not isinstance(reason, DeletionReason):
            raise TypeError("reason must be a DeletionReason")
        now = validate_aware_datetime(now, "now")
        if last_glpi_status_id is not None:
            last_glpi_status_id = validate_positive_integer(
                last_glpi_status_id,
                "last_glpi_status_id",
            )
        retention = self._lock(job_uuid)
        self._validate_deletion_start(
            retention,
            reason=reason,
            now=now,
            last_glpi_status_id=last_glpi_status_id,
        )

        retention.status = RetentionStatus.DELETING.value
        retention.next_check_at = None
        retention.deletion_reason = reason.value
        retention.deletion_started_at = now
        if last_glpi_status_id is not None:
            retention.last_glpi_status_id = last_glpi_status_id
        retention.consecutive_retry_count = 0
        retention.updated_at = now
        self._clear_error(retention)
        self._session.flush()
        return _to_snapshot(retention)

    def resume_deletion(
        self,
        job_uuid: UUID,
        *,
        now: datetime,
    ) -> AudioRetentionSnapshot:
        """Resume a persisted deletion directly or after its backoff elapsed."""
        now = validate_aware_datetime(now, "now")
        retention = self._lock(job_uuid)
        if retention.status == RetentionStatus.DELETING.value:
            self._require_deletion_context(retention)
            return _to_snapshot(retention)
        if retention.status != RetentionStatus.RETRY_SCHEDULED.value:
            self._raise_invalid_status(retention, "resume deletion")
        self._require_deletion_context(retention)
        if retention.next_check_at is None or retention.next_check_at > now:
            raise InvalidRetentionTransitionError(
                f"retention for job {job_uuid} is not due for a deletion retry"
            )

        retention.status = RetentionStatus.DELETING.value
        retention.next_check_at = None
        retention.updated_at = now
        self._clear_error(retention)
        self._session.flush()
        return _to_snapshot(retention)

    def mark_deleted(
        self,
        job_uuid: UUID,
        *,
        now: datetime,
    ) -> AudioRetentionSnapshot:
        """Confirm that deletion completed after its durable intent was stored."""
        now = validate_aware_datetime(now, "now")
        retention = self._lock(job_uuid)
        self._require_status(retention, RetentionStatus.DELETING)
        self._require_deletion_context(retention)
        if now < retention.deletion_started_at:
            raise ValueError("now must not be earlier than deletion_started_at")

        retention.status = RetentionStatus.DELETED.value
        retention.audio_deleted_at = now
        retention.consecutive_retry_count = 0
        retention.updated_at = now
        self._clear_error(retention)
        self._session.flush()
        return _to_snapshot(retention)

    def deactivate_for_operator_retry(
        self,
        job_uuid: UUID,
        *,
        now: datetime,
    ) -> AudioRetentionSnapshot:
        """Deactivate a terminal job's retention before an operator retry."""
        now = validate_aware_datetime(now, "now")
        retention = self._lock(job_uuid)
        if retention.status not in (
            RetentionStatus.SCHEDULED.value,
            RetentionStatus.BLOCKED.value,
            RetentionStatus.RETRY_SCHEDULED.value,
        ):
            self._raise_invalid_status(retention, "deactivate it")
        if retention.deletion_started_at is not None:
            raise InvalidRetentionTransitionError(
                f"retention for job {job_uuid} has already started deletion"
            )

        retention.status = RetentionStatus.INACTIVE.value
        retention.hard_delete_at = None
        retention.next_check_at = None
        retention.last_checked_at = None
        retention.last_glpi_status_id = None
        retention.consecutive_retry_count = 0
        retention.deletion_reason = None
        retention.deletion_started_at = None
        retention.audio_deleted_at = None
        retention.last_error_category = None
        retention.last_error_code = None
        retention.updated_at = now
        self._session.flush()
        return _to_snapshot(retention)

    def _deleting_candidate_statement(self) -> Select[tuple[AudioRetention]]:
        return (
            select(AudioRetention)
            .where(AudioRetention.status == RetentionStatus.DELETING.value)
            .order_by(AudioRetention.deletion_started_at, AudioRetention.job_uuid)
            .limit(1)
        )

    def _checking_candidate_statement(self) -> Select[tuple[AudioRetention]]:
        return (
            select(AudioRetention)
            .where(AudioRetention.status == RetentionStatus.CHECKING.value)
            .order_by(AudioRetention.last_checked_at, AudioRetention.job_uuid)
            .limit(1)
        )

    def _first_deadline_candidate_statement(
        self,
        now: datetime,
    ) -> Select[tuple[AudioRetention]]:
        return (
            select(AudioRetention)
            .where(
                AudioRetention.status.in_(
                    (
                        RetentionStatus.SCHEDULED.value,
                        RetentionStatus.RETRY_SCHEDULED.value,
                        RetentionStatus.BLOCKED.value,
                    )
                ),
                AudioRetention.hard_delete_at <= now,
                AudioRetention.deletion_started_at.is_(None),
            )
            .order_by(AudioRetention.hard_delete_at, AudioRetention.job_uuid)
            .limit(1)
        )

    def _deletion_retry_candidate_statement(
        self,
        now: datetime,
    ) -> Select[tuple[AudioRetention]]:
        return (
            select(AudioRetention)
            .where(
                AudioRetention.status == RetentionStatus.RETRY_SCHEDULED.value,
                AudioRetention.next_check_at <= now,
                AudioRetention.deletion_started_at.is_not(None),
            )
            .order_by(AudioRetention.next_check_at, AudioRetention.job_uuid)
            .limit(1)
        )

    def _glpi_check_candidate_statement(
        self,
        now: datetime,
    ) -> Select[tuple[AudioRetention]]:
        return (
            select(AudioRetention)
            .where(
                AudioRetention.policy == RetentionPolicy.TICKET_OR_DEADLINE.value,
                AudioRetention.status.in_(
                    (
                        RetentionStatus.SCHEDULED.value,
                        RetentionStatus.RETRY_SCHEDULED.value,
                    )
                ),
                AudioRetention.next_check_at <= now,
                AudioRetention.deletion_started_at.is_(None),
            )
            .order_by(AudioRetention.next_check_at, AudioRetention.job_uuid)
            .limit(1)
        )

    def _lock(self, job_uuid: UUID) -> AudioRetention:
        validate_uuid(job_uuid, "job_uuid")
        retention = self._session.get(
            AudioRetention,
            job_uuid,
            with_for_update=True,
        )
        if retention is None:
            raise RetentionNotFoundError(f"retention for job {job_uuid} does not exist")
        return retention

    def _require_checking(self, retention: AudioRetention) -> None:
        self._require_status(retention, RetentionStatus.CHECKING)
        if retention.policy != RetentionPolicy.TICKET_OR_DEADLINE.value:
            raise InvalidRetentionTransitionError(
                f"retention for job {retention.job_uuid} does not use GLPI checks"
            )
        if retention.deletion_started_at is not None:
            raise InvalidRetentionTransitionError(
                f"retention for job {retention.job_uuid} has deletion context"
            )

    def _require_status(
        self,
        retention: AudioRetention,
        expected_status: RetentionStatus,
    ) -> None:
        if retention.status != expected_status.value:
            self._raise_invalid_status(
                retention, f"transition from {expected_status.value}"
            )

    def _raise_invalid_status(
        self,
        retention: AudioRetention,
        operation: str,
    ) -> None:
        raise InvalidRetentionTransitionError(
            f"retention for job {retention.job_uuid} cannot {operation} "
            f"from status {retention.status}"
        )

    def _validate_deletion_start(
        self,
        retention: AudioRetention,
        *,
        reason: DeletionReason,
        now: datetime,
        last_glpi_status_id: int | None,
    ) -> None:
        if retention.status not in (
            RetentionStatus.SCHEDULED.value,
            RetentionStatus.CHECKING.value,
            RetentionStatus.RETRY_SCHEDULED.value,
            RetentionStatus.BLOCKED.value,
        ):
            self._raise_invalid_status(retention, "start deletion")
        if retention.deletion_started_at is not None:
            raise InvalidRetentionTransitionError(
                f"retention for job {retention.job_uuid} has already started deletion"
            )

        policy = RetentionPolicy(retention.policy)
        if reason is DeletionReason.TICKET_CLOSED:
            if policy is not RetentionPolicy.TICKET_OR_DEADLINE:
                raise InvalidRetentionTransitionError(
                    "ticket_closed deletion requires ticket_or_deadline policy"
                )
            if retention.status != RetentionStatus.CHECKING.value:
                raise InvalidRetentionTransitionError(
                    "ticket_closed deletion requires an active GLPI check"
                )
            if last_glpi_status_id is None:
                raise ValueError("ticket_closed deletion requires last_glpi_status_id")
            return

        expected_policy = (
            RetentionPolicy.TICKET_OR_DEADLINE
            if reason is DeletionReason.DEADLINE_EXPIRED
            else RetentionPolicy.DEADLINE_ONLY
        )
        if policy is not expected_policy:
            raise InvalidRetentionTransitionError(
                f"{reason.value} deletion is incompatible with {policy.value} policy"
            )
        if last_glpi_status_id is not None:
            raise ValueError("deadline deletion does not accept last_glpi_status_id")
        if retention.hard_delete_at is None or retention.hard_delete_at > now:
            raise InvalidRetentionTransitionError(
                f"retention for job {retention.job_uuid} has not reached its deadline"
            )

    @staticmethod
    def _require_deletion_context(retention: AudioRetention) -> None:
        if retention.deletion_reason is None or retention.deletion_started_at is None:
            raise InvalidRetentionTransitionError(
                f"retention for job {retention.job_uuid} has no deletion context"
            )

    @staticmethod
    def _clear_error(retention: AudioRetention) -> None:
        retention.last_error_category = None
        retention.last_error_code = None


def _to_snapshot(retention: AudioRetention) -> AudioRetentionSnapshot:
    return AudioRetentionSnapshot(
        job_uuid=retention.job_uuid,
        policy=RetentionPolicy(retention.policy),
        status=RetentionStatus(retention.status),
        hard_delete_at=retention.hard_delete_at,
        next_check_at=retention.next_check_at,
        last_checked_at=retention.last_checked_at,
        last_glpi_status_id=retention.last_glpi_status_id,
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
    "RetentionCandidate",
    "RetentionCandidateKind",
    "RetentionNotFoundError",
]
