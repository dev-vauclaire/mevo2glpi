"""Internal contracts between the worker orchestration components."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from mevo2glpi_database import ErrorScope, JobStage

from .outcomes import RunOutcome


class JobSnapshotProtocol(Protocol):
    """Persisted fields needed to process one acquired job."""

    uuid: UUID
    storage_key: str
    audio_size_bytes: int
    glpi_ticket_id: int | None
    consecutive_retry_count: int


class JobAcquisitionProtocol(Protocol):
    """Lifecycle result describing the job selected in one transaction."""

    job: JobSnapshotProtocol
    source_received_at: datetime | None
    originating_phone_number: str | None
    originating_phone_number_error_code: str | None


class JobLifecycleProtocol(Protocol):
    """Application-owned persistent transitions used by the worker."""

    def resume_or_claim_next(self, now: datetime) -> JobAcquisitionProtocol | None: ...

    def record_transcription_succeeded(
        self,
        job_uuid: UUID,
        now: datetime,
    ) -> object: ...

    def record_transcription_failed(
        self,
        job_uuid: UUID,
        *,
        error_category: str,
        error_code: str,
        now: datetime,
    ) -> object: ...

    def schedule_retry(
        self,
        job_uuid: UUID,
        *,
        stage: JobStage,
        error_scope: ErrorScope,
        error_category: str,
        error_code: str,
        next_attempt_at: datetime,
        now: datetime,
    ) -> object: ...

    def record_ticket_confirmed(
        self,
        job_uuid: UUID,
        ticket_id: int,
        now: datetime,
        *,
        clear_originating_phone_number: bool = False,
    ) -> object: ...

    def mark_completed(
        self,
        job_uuid: UUID,
        *,
        hard_delete_at: datetime,
        next_check_at: datetime,
        now: datetime,
    ) -> object: ...

    def mark_failed(
        self,
        job_uuid: UUID,
        *,
        stage: JobStage,
        error_category: str,
        error_code: str,
        hard_delete_at: datetime,
        now: datetime,
    ) -> object: ...

    def mark_blocked(
        self,
        job_uuid: UUID,
        *,
        stage: JobStage,
        error_scope: ErrorScope,
        error_category: str,
        error_code: str,
        hard_delete_at: datetime,
        now: datetime,
    ) -> object: ...


class AutomationWorkerProtocol(Protocol):
    """Port consumed by the scheduling loop and runtime heartbeat."""

    def run_once(self) -> RunOutcome:
        """Advance at most one job to an observable boundary."""
        ...


__all__ = [
    "AutomationWorkerProtocol",
    "JobAcquisitionProtocol",
    "JobLifecycleProtocol",
    "JobSnapshotProtocol",
]
