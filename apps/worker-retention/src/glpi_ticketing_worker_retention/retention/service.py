"""Readable orchestration of one persisted audio-retention operation."""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from glpi_ticketing_common.audio_purge import AudioPurgeError
from glpi_ticketing_database import DeletionReason, ErrorScope
from glpi_ticketing_glpi import GlpiErrorCategory, TicketStatus

from .lifecycle import StaleRetentionCandidateError
from .outcomes import (
    AudioDeletedOutcome,
    CheckRecoveredOutcome,
    DependencyCondition,
    DependencyWaitingOutcome,
    GlpiCheckRescheduledOutcome,
    IdleOutcome,
    RetentionBlockedOutcome,
    RetentionFailure,
    RetentionOperation,
    RetentionRunOutcome,
    RetryScheduledOutcome,
    StaleCandidateOutcome,
    SystemBlockedOutcome,
)
from .policies import RetentionCheckPolicy
from .ports import (
    AudioPurgerProtocol,
    AudioPurgeWork,
    CheckRecoveryWork,
    GlpiCheckStageProtocol,
    GlpiCheckWork,
    RetentionLifecycleProtocol,
    RetryBackoffProtocol,
)
from .stages import (
    GlpiCheckFailure,
    GlpiCheckFailureDisposition,
    GlpiCheckSuccess,
)

LOGGER = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _no_op_job_started(_job_uuid: UUID) -> None:
    return None


class RetentionWorker:
    """Advance at most one retention record to an observable boundary."""

    def __init__(
        self,
        *,
        lifecycle: RetentionLifecycleProtocol,
        glpi_check_stage: GlpiCheckStageProtocol,
        audio_purger: AudioPurgerProtocol,
        retry_policy: RetryBackoffProtocol,
        check_policy: RetentionCheckPolicy,
        poll_interval_seconds: float,
        system_probe_interval_seconds: float,
        now: Callable[[], datetime] = _utc_now,
        on_job_started: Callable[[UUID], None] = _no_op_job_started,
    ) -> None:
        if not callable(getattr(lifecycle, "find_next_candidate", None)):
            raise TypeError("lifecycle must expose find_next_candidate()")
        if not callable(getattr(glpi_check_stage, "execute", None)):
            raise TypeError("glpi_check_stage must expose execute()")
        if not callable(getattr(audio_purger, "purge", None)):
            raise TypeError("audio_purger must expose purge()")
        if not callable(getattr(retry_policy, "next_attempt_at", None)):
            raise TypeError("retry_policy must expose next_attempt_at()")
        if not isinstance(check_policy, RetentionCheckPolicy):
            raise TypeError("check_policy must be a RetentionCheckPolicy")
        if not callable(now):
            raise TypeError("now must be callable")
        if not callable(on_job_started):
            raise TypeError("on_job_started must be callable")

        self._lifecycle = lifecycle
        self._glpi_check_stage = glpi_check_stage
        self._audio_purger = audio_purger
        self._retry_policy = retry_policy
        self._check_policy = check_policy
        self._poll_interval_seconds = _positive_seconds(
            poll_interval_seconds,
            "poll_interval_seconds",
        )
        self._system_probe_interval_seconds = _positive_seconds(
            system_probe_interval_seconds,
            "system_probe_interval_seconds",
        )
        self._now = now
        self._on_job_started = on_job_started

    def run_once(self) -> RetentionRunOutcome:
        """Select and execute at most one durable retention operation."""
        selection_time = self._current_time()
        candidate = self._lifecycle.find_next_candidate(selection_time)
        if candidate is None:
            return self._idle_or_wait_for_probe(selection_time)

        job_uuid = candidate.retention.job_uuid
        self._on_job_started(job_uuid)
        try:
            work = self._lifecycle.prepare_candidate(candidate, selection_time)
            if isinstance(work, CheckRecoveryWork):
                LOGGER.info("retention check recovered job_uuid=%s", work.job_uuid)
                return CheckRecoveredOutcome(job_uuid=work.job_uuid)
            if isinstance(work, GlpiCheckWork):
                return self._execute_glpi_check(work)
            if isinstance(work, AudioPurgeWork):
                return self._execute_audio_purge(work)
            raise AssertionError("unsupported prepared retention work")
        except StaleRetentionCandidateError:
            LOGGER.info("retention candidate became stale job_uuid=%s", job_uuid)
            return StaleCandidateOutcome(job_uuid=job_uuid)

    def _idle_or_wait_for_probe(self, now: datetime) -> RetentionRunOutcome:
        gate = self._lifecycle.get_glpi_probe_gate()
        if gate is None:
            return IdleOutcome(wait_seconds=self._poll_interval_seconds)
        if (
            gate.next_check_at is None
            or gate.last_error_category is None
            or gate.last_error_code is None
        ):
            raise RuntimeError("persisted GLPI probe has incomplete retry context")

        remaining_seconds = (gate.next_check_at - now).total_seconds()
        wait_seconds = min(
            self._poll_interval_seconds,
            max(remaining_seconds, 0.001),
        )
        condition = (
            DependencyCondition.DEGRADED
            if gate.last_error_category == GlpiErrorCategory.UNAVAILABLE.value
            else DependencyCondition.BLOCKED
        )
        return DependencyWaitingOutcome(
            job_uuid=gate.job_uuid,
            failure=RetentionFailure(
                scope=ErrorScope.SYSTEM,
                category=gate.last_error_category,
                technical_code=gate.last_error_code,
            ),
            condition=condition,
            wait_seconds=wait_seconds,
        )

    def _execute_glpi_check(self, work: GlpiCheckWork) -> RetentionRunOutcome:
        LOGGER.info("GLPI retention check started job_uuid=%s", work.job_uuid)
        result = self._glpi_check_stage.execute(work)
        decision_time = self._current_time()

        if isinstance(result, GlpiCheckSuccess):
            if decision_time >= work.hard_delete_at:
                return self._delete_expired_check(
                    work,
                    decision_time,
                    ticket_status=result.snapshot.status,
                )
            if result.snapshot.status is TicketStatus.CLOSED:
                purge_work = self._lifecycle.start_deletion_after_check(
                    work,
                    reason=DeletionReason.TICKET_CLOSED,
                    now=decision_time,
                    ticket_status=result.snapshot.status,
                )
                return self._execute_audio_purge(purge_work)

            next_check_at = self._check_policy.next_check_at(
                now=decision_time,
                hard_delete_at=work.hard_delete_at,
            )
            self._lifecycle.reschedule_glpi_check(
                work,
                ticket_status=result.snapshot.status,
                next_check_at=next_check_at,
                now=decision_time,
            )
            LOGGER.info(
                "GLPI ticket retained job_uuid=%s ticket_status=%s",
                work.job_uuid,
                int(result.snapshot.status),
            )
            return GlpiCheckRescheduledOutcome(
                job_uuid=work.job_uuid,
                ticket_status=result.snapshot.status,
                next_check_at=next_check_at,
            )

        if not isinstance(result, GlpiCheckFailure):
            raise AssertionError("unsupported GLPI check result")
        if decision_time >= work.hard_delete_at:
            return self._delete_expired_check(work, decision_time)
        if result.disposition is GlpiCheckFailureDisposition.JOB_BLOCKED:
            self._lifecycle.mark_blocked(
                work,
                failure=result.failure,
                now=decision_time,
            )
            self._log_failure("retention blocked", work, result.failure)
            return RetentionBlockedOutcome(
                job_uuid=work.job_uuid,
                operation=RetentionOperation.GLPI_CHECK,
                failure=result.failure,
            )
        if result.disposition is GlpiCheckFailureDisposition.RETRYABLE:
            next_attempt_at = self._bounded_glpi_backoff(work, decision_time)
            self._lifecycle.schedule_retry(
                work,
                failure=result.failure,
                next_attempt_at=next_attempt_at,
                now=decision_time,
            )
            self._log_failure("retention retry scheduled", work, result.failure)
            return RetryScheduledOutcome(
                job_uuid=work.job_uuid,
                operation=RetentionOperation.GLPI_CHECK,
                failure=result.failure,
                next_attempt_at=next_attempt_at,
            )
        if result.disposition is GlpiCheckFailureDisposition.SYSTEM_BLOCKED:
            next_probe_at = self._check_policy.clamp_retry(
                proposed_at=(
                    decision_time
                    + timedelta(seconds=self._system_probe_interval_seconds)
                ),
                now=decision_time,
                hard_delete_at=work.hard_delete_at,
            )
            self._lifecycle.schedule_retry(
                work,
                failure=result.failure,
                next_attempt_at=next_probe_at,
                now=decision_time,
            )
            self._log_failure("retention system blocked", work, result.failure)
            return SystemBlockedOutcome(
                job_uuid=work.job_uuid,
                operation=RetentionOperation.GLPI_CHECK,
                failure=result.failure,
                next_probe_at=next_probe_at,
            )
        raise AssertionError("unsupported GLPI failure disposition")

    def _delete_expired_check(
        self,
        work: GlpiCheckWork,
        now: datetime,
        *,
        ticket_status: TicketStatus | None = None,
    ) -> RetentionRunOutcome:
        purge_work = self._lifecycle.start_deletion_after_check(
            work,
            reason=DeletionReason.DEADLINE_EXPIRED,
            now=now,
            ticket_status=ticket_status,
        )
        return self._execute_audio_purge(purge_work)

    def _execute_audio_purge(self, work: AudioPurgeWork) -> RetentionRunOutcome:
        LOGGER.info("audio purge started job_uuid=%s", work.job_uuid)
        try:
            self._audio_purger.purge(work.storage_key)
        except AudioPurgeError as error:
            decision_time = self._current_time()
            failure = RetentionFailure(
                scope=ErrorScope.SYSTEM if error.retryable else ErrorScope.JOB,
                category=("storage_unavailable" if error.retryable else "storage"),
                technical_code=error.code.value,
            )
            if error.retryable:
                next_attempt_at = self._retry_policy.next_attempt_at(
                    now=decision_time,
                    consecutive_retry_count=work.consecutive_retry_count,
                )
                self._lifecycle.schedule_retry(
                    work,
                    failure=failure,
                    next_attempt_at=next_attempt_at,
                    now=decision_time,
                )
                self._log_failure("audio purge retry scheduled", work, failure)
                return RetryScheduledOutcome(
                    job_uuid=work.job_uuid,
                    operation=RetentionOperation.AUDIO_PURGE,
                    failure=failure,
                    next_attempt_at=next_attempt_at,
                )

            self._lifecycle.mark_blocked(
                work,
                failure=failure,
                now=decision_time,
            )
            self._log_failure("audio purge blocked", work, failure)
            return RetentionBlockedOutcome(
                job_uuid=work.job_uuid,
                operation=RetentionOperation.AUDIO_PURGE,
                failure=failure,
            )

        completed_at = self._current_time()
        self._lifecycle.mark_deleted(work, now=completed_at)
        LOGGER.info(
            "audio purge completed job_uuid=%s deletion_reason=%s",
            work.job_uuid,
            work.reason.value,
        )
        return AudioDeletedOutcome(job_uuid=work.job_uuid, reason=work.reason)

    def _bounded_glpi_backoff(
        self,
        work: GlpiCheckWork,
        now: datetime,
    ) -> datetime:
        proposed_at = self._retry_policy.next_attempt_at(
            now=now,
            consecutive_retry_count=work.consecutive_retry_count,
        )
        return self._check_policy.clamp_retry(
            proposed_at=proposed_at,
            now=now,
            hard_delete_at=work.hard_delete_at,
        )

    def _current_time(self) -> datetime:
        value = self._now()
        if not isinstance(value, datetime):
            raise TypeError("now must return a datetime")
        if value.utcoffset() is None:
            raise ValueError("now must return a timezone-aware datetime")
        return value

    @staticmethod
    def _log_failure(
        message: str,
        work: GlpiCheckWork | AudioPurgeWork,
        failure: RetentionFailure,
    ) -> None:
        LOGGER.warning(
            "%s job_uuid=%s category=%s technical_code=%s",
            message,
            work.job_uuid,
            failure.category,
            failure.technical_code,
        )


def _positive_seconds(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{field_name} must be a number")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized <= 0:
        raise ValueError(f"{field_name} must be finite and greater than zero")
    return normalized


__all__ = ["RetentionWorker"]
