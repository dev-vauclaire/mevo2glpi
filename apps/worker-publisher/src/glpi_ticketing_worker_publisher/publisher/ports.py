"""Internal contracts used by the publisher orchestration."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from glpi_ticketing_database import (
    MailIngestionAcquisition,
    MailIngestionSnapshot,
    MailIngestionStage,
)

from ..graph import GraphAttachment, GraphMessage
from .outcomes import PublisherFailure, PublisherRunOutcome


class PublisherLifecycleProtocol(Protocol):
    """Durable transitions owned by the publisher application."""

    def discover_messages(
        self,
        *,
        mailbox_id: UUID,
        messages: tuple[GraphMessage, ...],
        now: datetime,
    ) -> int: ...

    def resume_or_claim_next(
        self,
        now: datetime,
    ) -> MailIngestionAcquisition | None: ...

    def get_next_system_retry_at(self) -> datetime | None: ...

    def record_attachment_validated(
        self,
        ingestion_uuid: UUID,
        attachment: GraphAttachment,
        now: datetime,
    ) -> MailIngestionSnapshot: ...

    def record_rejected(
        self,
        ingestion_uuid: UUID,
        *,
        failure: PublisherFailure,
        now: datetime,
        attachment: GraphAttachment | None = None,
        actual_size_bytes: int | None = None,
    ) -> MailIngestionSnapshot: ...

    def record_audio_published(
        self,
        ingestion_uuid: UUID,
        *,
        storage_key: str,
        size_bytes: int,
        now: datetime,
    ) -> MailIngestionSnapshot: ...

    def schedule_retry(
        self,
        ingestion_uuid: UUID,
        *,
        stage: MailIngestionStage,
        failure: PublisherFailure,
        next_attempt_at: datetime,
        now: datetime,
    ) -> MailIngestionSnapshot: ...

    def record_blocked(
        self,
        ingestion_uuid: UUID,
        *,
        stage: MailIngestionStage,
        failure: PublisherFailure,
        now: datetime,
    ) -> MailIngestionSnapshot: ...

    def record_message_moved(
        self,
        ingestion_uuid: UUID,
        *,
        now: datetime,
    ) -> MailIngestionSnapshot: ...


class PublisherWorkerProtocol(Protocol):
    """Port consumed by the runtime scheduling loop."""

    def run_once(self) -> PublisherRunOutcome:
        """Advance at most one ingestion to an observable boundary."""
        ...


__all__ = ["PublisherLifecycleProtocol", "PublisherWorkerProtocol"]
