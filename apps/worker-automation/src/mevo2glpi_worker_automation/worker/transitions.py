"""Durable worker transitions and their sanitized observable outcomes."""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from mevo2glpi_database import ErrorScope, JobStage
from mevo2glpi_glpi import TicketId

from .outcomes import (
    BlockedOutcome,
    CompletedOutcome,
    FailedOutcome,
    FailureDetails,
    RetryOutcome,
    SystemBlockedOutcome,
)
from .policies import RetentionSchedulePolicy, RetryBackoffPolicy
from .ports import JobLifecycleProtocol, JobSnapshotProtocol

LOGGER = logging.getLogger(__name__)


class JobTransitionService:
    """Persist terminal/retry transitions and build public outcomes.

    System-wide blocks intentionally remain in-memory: the current job stays
    ``processing`` so an automatic probe or an operator decision can resume it.
    """

    def __init__(
        self,
        *,
        lifecycle: JobLifecycleProtocol,
        retry_policy: RetryBackoffPolicy,
        retention_policy: RetentionSchedulePolicy,
        maximum_consecutive_retries: int = 3,
        system_probe_interval_seconds: float = 60.0,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not callable(getattr(lifecycle, "schedule_retry", None)):
            raise TypeError("lifecycle must expose schedule_retry()")
        if not isinstance(retry_policy, RetryBackoffPolicy):
            raise TypeError("retry_policy must be a RetryBackoffPolicy")
        if not isinstance(retention_policy, RetentionSchedulePolicy):
            raise TypeError("retention_policy must be a RetentionSchedulePolicy")
        if isinstance(maximum_consecutive_retries, bool) or not isinstance(
            maximum_consecutive_retries, int
        ):
            raise TypeError("maximum_consecutive_retries must be an integer")
        if maximum_consecutive_retries <= 0:
            raise ValueError("maximum_consecutive_retries must be greater than zero")
        if now is not None and not callable(now):
            raise TypeError("now must be callable")

        self._lifecycle = lifecycle
        self._retry_policy = retry_policy
        self._retention_policy = retention_policy
        self._maximum_consecutive_retries = maximum_consecutive_retries
        self._system_probe_interval_seconds = _positive_seconds(
            system_probe_interval_seconds,
            "system_probe_interval_seconds",
        )
        self._now = now or _utc_now

    def complete(self, audio_uuid: UUID, ticket_id: int) -> RunCompletedOrBlocked:
        """Finalize a ticket-confirmed job and activate its retention row."""

        if not _valid_ticket_id(ticket_id):
            return self.block(
                audio_uuid,
                stage=JobStage.FINALIZATION,
                failure=FailureDetails(
                    scope=ErrorScope.SYSTEM,
                    category="persistence",
                    technical_code="invalid_persisted_ticket_id",
                ),
                ticket_id=None,
                automatic_probe=False,
            )

        now = self.current_time()
        hard_delete_at, next_check_at = self._retention_policy.completed_schedule(now)
        self._lifecycle.mark_completed(
            audio_uuid,
            hard_delete_at=hard_delete_at,
            next_check_at=next_check_at,
            now=now,
        )
        LOGGER.info(
            "audio processing completed audio_uuid=%s ticket_id=%s",
            audio_uuid,
            ticket_id,
        )
        return CompletedOutcome(
            audio_uuid=audio_uuid,
            ticket_id=TicketId(ticket_id),
        )

    def retry(
        self,
        job: JobSnapshotProtocol,
        *,
        stage: JobStage,
        category: str,
        technical_code: str,
    ) -> RetryOutcome:
        """Schedule the next eligible attempt using the shared backoff policy."""

        if self.retry_budget_exhausted(job):
            raise RuntimeError("the job retry budget is exhausted")

        now = self.current_time()
        next_attempt_at = self._retry_policy.next_attempt_at(
            now=now,
            consecutive_retry_count=job.consecutive_retry_count,
        )
        failure = FailureDetails(
            scope=ErrorScope.SYSTEM,
            category=category,
            technical_code=technical_code,
        )
        self._lifecycle.schedule_retry(
            job.uuid,
            stage=stage,
            error_scope=failure.scope,
            error_category=failure.category,
            error_code=failure.technical_code,
            next_attempt_at=next_attempt_at,
            now=now,
        )
        LOGGER.warning(
            "audio processing retry scheduled audio_uuid=%s stage=%s code=%s",
            job.uuid,
            stage.value,
            technical_code,
        )
        return RetryOutcome(
            audio_uuid=job.uuid,
            stage=stage,
            failure=failure,
            next_attempt_at=next_attempt_at,
        )

    def retry_budget_exhausted(self, job: JobSnapshotProtocol) -> bool:
        """Return whether another durable retry would exceed the job budget."""

        retry_count = job.consecutive_retry_count
        if isinstance(retry_count, bool) or not isinstance(retry_count, int):
            raise TypeError("consecutive_retry_count must be an integer")
        if retry_count < 0:
            raise ValueError("consecutive_retry_count must not be negative")
        return retry_count >= self._maximum_consecutive_retries

    def fail(
        self,
        audio_uuid: UUID,
        *,
        stage: JobStage,
        category: str,
        technical_code: str,
    ) -> FailedOutcome:
        """Persist a terminal failure caused by this specific job."""

        now = self.current_time()
        failure = FailureDetails(
            scope=ErrorScope.JOB,
            category=category,
            technical_code=technical_code,
        )
        self._lifecycle.mark_failed(
            audio_uuid,
            stage=stage,
            error_category=failure.category,
            error_code=failure.technical_code,
            hard_delete_at=self._retention_policy.failed_deadline(now),
            now=now,
        )
        LOGGER.error(
            "audio processing failed audio_uuid=%s stage=%s code=%s",
            audio_uuid,
            stage.value,
            technical_code,
        )
        return FailedOutcome(
            audio_uuid=audio_uuid,
            stage=stage,
            failure=failure,
        )

    def block(
        self,
        audio_uuid: UUID,
        *,
        stage: JobStage,
        failure: FailureDetails,
        ticket_id: int | None = None,
        automatic_probe: bool = True,
    ) -> BlockedOutcome | SystemBlockedOutcome:
        """Expose a system block or durably isolate a job-scoped ambiguity."""

        now = self.current_time()
        if failure.scope is ErrorScope.SYSTEM:
            LOGGER.error(
                "audio processing system blocked audio_uuid=%s stage=%s code=%s",
                audio_uuid,
                stage.value,
                failure.technical_code,
            )
            return SystemBlockedOutcome(
                audio_uuid=audio_uuid,
                stage=stage,
                failure=failure,
                probe_after_seconds=self._system_probe_interval_seconds,
                automatic_probe=automatic_probe,
                ticket_id=(
                    TicketId(ticket_id) if _valid_ticket_id(ticket_id) else None
                ),
            )

        self._lifecycle.mark_blocked(
            audio_uuid,
            stage=stage,
            error_scope=failure.scope,
            error_category=failure.category,
            error_code=failure.technical_code,
            hard_delete_at=self._retention_policy.blocked_deadline(now),
            now=now,
        )
        LOGGER.error(
            "audio processing blocked audio_uuid=%s stage=%s scope=%s code=%s",
            audio_uuid,
            stage.value,
            failure.scope.value,
            failure.technical_code,
        )
        return BlockedOutcome(
            audio_uuid=audio_uuid,
            stage=stage,
            failure=failure,
            ticket_id=TicketId(ticket_id) if ticket_id is not None else None,
        )

    def current_time(self) -> datetime:
        """Return the validated UTC-aware clock value used by transitions."""

        current = self._now()
        if not isinstance(current, datetime):
            raise TypeError("now must return a datetime")
        if current.utcoffset() is None:
            raise ValueError("now must return a timezone-aware datetime")
        return current


type RunCompletedOrBlocked = CompletedOutcome | SystemBlockedOutcome


def _valid_ticket_id(ticket_id: object) -> bool:
    return (
        not isinstance(ticket_id, bool) and isinstance(ticket_id, int) and ticket_id > 0
    )


def _positive_seconds(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{field_name} must be a number")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized <= 0:
        raise ValueError(f"{field_name} must be finite and greater than zero")
    return normalized


def _utc_now() -> datetime:
    return datetime.now(UTC)


__all__ = ["JobTransitionService", "RunCompletedOrBlocked"]
