"""Internal contracts and detached work items for the retention worker."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from mevo2glpi_database import (
    AudioRetentionSnapshot,
    DeletionReason,
    RetentionCandidate,
)
from mevo2glpi_glpi import TicketStatus

from .outcomes import (
    RejectedIngestionsPurgedOutcome,
    RetentionFailure,
    RetentionRunOutcome,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class CheckRecoveryWork:
    """An interrupted GLPI check that has already been durably requeued."""

    job_uuid: UUID


@dataclass(frozen=True, slots=True, kw_only=True)
class GlpiCheckWork:
    """Detached data needed to inspect one known GLPI ticket."""

    job_uuid: UUID
    storage_key: str
    ticket_id: int
    hard_delete_at: datetime
    consecutive_retry_count: int


@dataclass(frozen=True, slots=True, kw_only=True)
class AudioPurgeWork:
    """Detached data needed to resume one durable deletion intent."""

    job_uuid: UUID
    storage_key: str
    reason: DeletionReason
    consecutive_retry_count: int


@dataclass(frozen=True, slots=True, kw_only=True)
class DatabasePurgeWork:
    """Detached data needed to remove one legacy audio-deleted aggregate."""

    job_uuid: UUID
    reason: DeletionReason


type PreparedRetentionWork = (
    CheckRecoveryWork | GlpiCheckWork | AudioPurgeWork | DatabasePurgeWork
)
type ExternalRetentionWork = GlpiCheckWork | AudioPurgeWork


class RetentionLifecycleProtocol(Protocol):
    """Short database transitions owned by the retention application."""

    def find_next_candidate(self, now: datetime) -> RetentionCandidate | None: ...

    def get_glpi_probe_gate(self) -> AudioRetentionSnapshot | None: ...

    def prepare_candidate(
        self,
        candidate: RetentionCandidate,
        now: datetime,
    ) -> PreparedRetentionWork: ...

    def reschedule_glpi_check(
        self,
        work: GlpiCheckWork,
        *,
        ticket_status: TicketStatus,
        next_check_at: datetime,
        now: datetime,
    ) -> None: ...

    def start_deletion_after_check(
        self,
        work: GlpiCheckWork,
        *,
        reason: DeletionReason,
        now: datetime,
        ticket_status: TicketStatus | None = None,
    ) -> AudioPurgeWork: ...

    def schedule_retry(
        self,
        work: ExternalRetentionWork,
        *,
        failure: RetentionFailure,
        next_attempt_at: datetime,
        now: datetime,
    ) -> None: ...

    def mark_blocked(
        self,
        work: ExternalRetentionWork,
        *,
        failure: RetentionFailure,
        now: datetime,
    ) -> None: ...

    def purge_database_aggregate(
        self,
        work: AudioPurgeWork | DatabasePurgeWork,
    ) -> None: ...


class RejectedIngestionRetentionLifecycleProtocol(Protocol):
    """Bounded PostgreSQL cleanup operations for terminal mail rejections."""

    def purge_expired(self, cutoff: datetime, *, limit: int) -> int: ...


class RejectedIngestionRetentionProtocol(Protocol):
    """Periodically purge expired rejected ingestions."""

    def run_if_due(
        self,
        now: datetime,
    ) -> RejectedIngestionsPurgedOutcome | None: ...


class RetryBackoffProtocol(Protocol):
    """Compute the next retry timestamp from persisted failure count."""

    def next_attempt_at(
        self,
        *,
        now: datetime,
        consecutive_retry_count: int,
    ) -> datetime: ...


class AudioPurgerProtocol(Protocol):
    """Delete one exact canonical audio through a crash-safe quarantine."""

    def purge(self, storage_key: str) -> object: ...


class GlpiCheckStageProtocol(Protocol):
    """Inspect and classify one known ticket without persisting state."""

    def execute(self, work: GlpiCheckWork) -> object: ...


class RetentionWorkerProtocol(Protocol):
    """Port consumed by the runtime loop and heartbeat wrapper."""

    def run_once(self) -> RetentionRunOutcome: ...


__all__ = [
    "AudioPurgeWork",
    "AudioPurgerProtocol",
    "CheckRecoveryWork",
    "DatabasePurgeWork",
    "ExternalRetentionWork",
    "GlpiCheckStageProtocol",
    "GlpiCheckWork",
    "PreparedRetentionWork",
    "RejectedIngestionRetentionLifecycleProtocol",
    "RejectedIngestionRetentionProtocol",
    "RetentionLifecycleProtocol",
    "RetentionWorkerProtocol",
    "RetryBackoffProtocol",
]
