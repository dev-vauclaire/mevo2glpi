"""Readable fakes shared by the pure retention orchestration tests."""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime, timedelta
from uuid import UUID

from glpi_ticketing_database import (
    AudioRetentionSnapshot,
    DeletionReason,
    RetentionCandidate,
    RetentionCandidateKind,
    RetentionPolicy,
    RetentionStatus,
)
from glpi_ticketing_glpi import GlpiTicketSnapshot, TicketId, TicketStatus
from glpi_ticketing_worker_retention.retention.outcomes import (
    RetentionFailure,
    RetentionRunOutcome,
)
from glpi_ticketing_worker_retention.retention.ports import (
    AudioPurgeWork,
    ExternalRetentionWork,
    GlpiCheckWork,
    PreparedRetentionWork,
)

JOB_UUID = UUID("123e4567-e89b-42d3-a456-426614174000")
OTHER_JOB_UUID = UUID("223e4567-e89b-42d3-a456-426614174000")
STORAGE_KEY = f"{JOB_UUID}.wav"
NOW = datetime(2026, 8, 28, 10, 0, tzinfo=UTC)
HARD_DELETE_AT = NOW + timedelta(days=30)
TICKET_ID = 42


def retention_snapshot(
    *,
    job_uuid: UUID = JOB_UUID,
    policy: RetentionPolicy = RetentionPolicy.TICKET_OR_DEADLINE,
    status: RetentionStatus = RetentionStatus.SCHEDULED,
    hard_delete_at: datetime = HARD_DELETE_AT,
    next_check_at: datetime | None = NOW,
    consecutive_retry_count: int = 0,
    deletion_reason: DeletionReason | None = None,
    last_error_category: str | None = None,
    last_error_code: str | None = None,
) -> AudioRetentionSnapshot:
    """Build a coherent detached snapshot for service-level tests."""

    return AudioRetentionSnapshot(
        job_uuid=job_uuid,
        policy=policy,
        status=status,
        hard_delete_at=hard_delete_at,
        next_check_at=next_check_at,
        last_checked_at=None,
        last_glpi_status_id=None,
        check_attempt_count=0,
        consecutive_retry_count=consecutive_retry_count,
        deletion_reason=deletion_reason,
        deletion_started_at=None,
        audio_deleted_at=None,
        last_error_category=last_error_category,
        last_error_code=last_error_code,
        created_at=NOW - timedelta(days=1),
        updated_at=NOW - timedelta(hours=1),
    )


def retention_candidate(
    kind: RetentionCandidateKind = RetentionCandidateKind.START_GLPI_CHECK,
    *,
    snapshot: AudioRetentionSnapshot | None = None,
) -> RetentionCandidate:
    return RetentionCandidate(
        kind=kind,
        retention=snapshot or retention_snapshot(),
    )


def glpi_work(
    *,
    job_uuid: UUID = JOB_UUID,
    hard_delete_at: datetime = HARD_DELETE_AT,
    consecutive_retry_count: int = 0,
) -> GlpiCheckWork:
    return GlpiCheckWork(
        job_uuid=job_uuid,
        storage_key=f"{job_uuid}.wav",
        ticket_id=TICKET_ID,
        hard_delete_at=hard_delete_at,
        consecutive_retry_count=consecutive_retry_count,
    )


def purge_work(
    *,
    job_uuid: UUID = JOB_UUID,
    reason: DeletionReason = DeletionReason.TICKET_CLOSED,
    consecutive_retry_count: int = 0,
) -> AudioPurgeWork:
    return AudioPurgeWork(
        job_uuid=job_uuid,
        storage_key=f"{job_uuid}.wav",
        reason=reason,
        consecutive_retry_count=consecutive_retry_count,
    )


def ticket_snapshot(
    *,
    job_uuid: UUID = JOB_UUID,
    status: TicketStatus = TicketStatus.ASSIGNED,
    external_id: str | None = None,
) -> GlpiTicketSnapshot:
    return GlpiTicketSnapshot(
        ticket_id=TicketId(TICKET_ID),
        external_id=external_id or str(job_uuid),
        status=status,
    )


class ScriptedClock:
    """Return explicit instants so tests expose every time boundary."""

    def __init__(self, *instants: datetime) -> None:
        self._instants = deque(instants)

    def __call__(self) -> datetime:
        if not self._instants:
            raise AssertionError("test clock was called more often than expected")
        return self._instants.popleft()


class FakeTicketReader:
    """Return one snapshot or raise one scripted GLPI error."""

    def __init__(self, response: GlpiTicketSnapshot | BaseException) -> None:
        self.response = response
        self.ticket_ids: list[int] = []

    def get_ticket(self, ticket_id: TicketId) -> GlpiTicketSnapshot:
        self.ticket_ids.append(int(ticket_id))
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


class FakeLifecycle:
    """Record application decisions without simulating repository internals."""

    def __init__(
        self,
        *,
        candidate: RetentionCandidate | None,
        prepared_work: PreparedRetentionWork | BaseException | None = None,
        gate: AudioRetentionSnapshot | None = None,
    ) -> None:
        self.candidate = candidate
        self.prepared_work = prepared_work
        self.gate = gate
        self.calls: list[tuple[object, ...]] = []

    def find_next_candidate(self, now: datetime) -> RetentionCandidate | None:
        self.calls.append(("find_next_candidate", now))
        return self.candidate

    def get_glpi_probe_gate(self) -> AudioRetentionSnapshot | None:
        self.calls.append(("get_glpi_probe_gate",))
        return self.gate

    def prepare_candidate(
        self,
        candidate: RetentionCandidate,
        now: datetime,
    ) -> PreparedRetentionWork:
        self.calls.append(("prepare_candidate", candidate, now))
        if isinstance(self.prepared_work, BaseException):
            raise self.prepared_work
        if self.prepared_work is None:
            raise AssertionError("test did not configure prepared_work")
        return self.prepared_work

    def reschedule_glpi_check(
        self,
        work: GlpiCheckWork,
        *,
        ticket_status: TicketStatus,
        next_check_at: datetime,
        now: datetime,
    ) -> None:
        self.calls.append(
            (
                "reschedule_glpi_check",
                work,
                ticket_status,
                next_check_at,
                now,
            )
        )

    def start_deletion_after_check(
        self,
        work: GlpiCheckWork,
        *,
        reason: DeletionReason,
        now: datetime,
        ticket_status: TicketStatus | None = None,
    ) -> AudioPurgeWork:
        self.calls.append(
            (
                "start_deletion_after_check",
                work,
                reason,
                ticket_status,
                now,
            )
        )
        return AudioPurgeWork(
            job_uuid=work.job_uuid,
            storage_key=work.storage_key,
            reason=reason,
            consecutive_retry_count=0,
        )

    def schedule_retry(
        self,
        work: ExternalRetentionWork,
        *,
        failure: RetentionFailure,
        next_attempt_at: datetime,
        now: datetime,
    ) -> None:
        self.calls.append(("schedule_retry", work, failure, next_attempt_at, now))

    def mark_blocked(
        self,
        work: ExternalRetentionWork,
        *,
        failure: RetentionFailure,
        now: datetime,
    ) -> None:
        self.calls.append(("mark_blocked", work, failure, now))

    def mark_deleted(self, work: AudioPurgeWork, *, now: datetime) -> None:
        self.calls.append(("mark_deleted", work, now))


class FakeAudioPurger:
    def __init__(self, result: object) -> None:
        self.result = result
        self.storage_keys: list[str] = []

    def purge(self, storage_key: str) -> object:
        self.storage_keys.append(storage_key)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class ScriptedWorker:
    def __init__(self, outcomes: list[RetentionRunOutcome]) -> None:
        self._outcomes = deque(outcomes)
        self.calls = 0

    def run_once(self) -> RetentionRunOutcome:
        self.calls += 1
        if not self._outcomes:
            raise AssertionError("test worker has no remaining outcome")
        return self._outcomes.popleft()


__all__ = [
    "HARD_DELETE_AT",
    "JOB_UUID",
    "NOW",
    "OTHER_JOB_UUID",
    "STORAGE_KEY",
    "TICKET_ID",
    "FakeAudioPurger",
    "FakeLifecycle",
    "FakeTicketReader",
    "ScriptedClock",
    "ScriptedWorker",
    "glpi_work",
    "purge_work",
    "retention_candidate",
    "retention_snapshot",
    "ticket_snapshot",
]
