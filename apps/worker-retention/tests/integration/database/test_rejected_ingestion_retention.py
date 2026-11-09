"""PostgreSQL contracts for rejected-ingestion retention transactions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from mevo2glpi_database import (
    DiscoveredMail,
    MailIngestion,
    MailIngestionDisposition,
    MailIngestionErrorScope,
    MailIngestionEvent,
    MailIngestionEventRepository,
    MailIngestionEventType,
    MailIngestionRepository,
    MailIngestionStage,
    MailIngestionStatus,
    create_session_factory,
)
from mevo2glpi_worker_retention.retention.lifecycle import (
    RejectedIngestionRetentionLifecycleService,
)

pytestmark = pytest.mark.database

NOW = datetime(2026, 11, 9, 12, tzinfo=UTC)
MAILBOX_UUID = UUID("00000000-0000-4000-8000-000000000901")


def test_expired_rejection_purge_removes_events_and_parent_atomically(
    migrated_engine: Engine,
) -> None:
    session_factory = create_session_factory(migrated_engine)
    expired_uuid = UUID("00000000-0000-4000-8000-000000000902")
    recent_uuid = UUID("00000000-0000-4000-8000-000000000903")
    _persist_rejected(
        session_factory,
        ingestion_uuid=expired_uuid,
        finished_at=NOW - timedelta(days=91),
    )
    _persist_rejected(
        session_factory,
        ingestion_uuid=recent_uuid,
        finished_at=NOW - timedelta(days=1),
    )
    lifecycle = RejectedIngestionRetentionLifecycleService(
        session_factory=session_factory
    )

    cutoff = NOW - timedelta(days=90)
    assert lifecycle.count_expired(cutoff) == 1
    assert lifecycle.purge_expired(cutoff, limit=100) == 1

    with session_factory() as session:
        assert session.get(MailIngestion, expired_uuid) is None
        assert _event_count(session, expired_uuid) == 0
        assert session.get(MailIngestion, recent_uuid) is not None
        assert _event_count(session, recent_uuid) == 1


def test_rejection_purge_rolls_back_event_deletion_when_parent_delete_fails(
    migrated_engine: Engine,
) -> None:
    session_factory = create_session_factory(migrated_engine)
    ingestion_uuid = UUID("00000000-0000-4000-8000-000000000904")
    _persist_rejected(
        session_factory,
        ingestion_uuid=ingestion_uuid,
        finished_at=NOW - timedelta(days=91),
    )
    lifecycle = RejectedIngestionRetentionLifecycleService(
        session_factory=session_factory,
        ingestion_repository_factory=_FailingDeleteMailIngestionRepository,
    )

    with pytest.raises(RuntimeError, match="synthetic parent deletion failure"):
        lifecycle.purge_expired(NOW - timedelta(days=90), limit=100)

    with session_factory() as session:
        assert session.get(MailIngestion, ingestion_uuid) is not None
        assert _event_count(session, ingestion_uuid) == 1


class _FailingDeleteMailIngestionRepository(MailIngestionRepository):
    def delete_by_uuid(self, ingestion_uuid: UUID) -> None:
        raise RuntimeError("synthetic parent deletion failure")


def _persist_rejected(
    session_factory: sessionmaker[Session],
    *,
    ingestion_uuid: UUID,
    finished_at: datetime,
) -> None:
    with session_factory.begin() as session:
        ingestion_repository = MailIngestionRepository(session)
        ingestion_repository.add_discovered(
            DiscoveredMail(
                uuid=ingestion_uuid,
                mailbox_id=MAILBOX_UUID,
                message_id=f"synthetic-message-{ingestion_uuid}",
                received_at=finished_at - timedelta(days=1),
            ),
            now=finished_at - timedelta(seconds=2),
        )
        acquisition = ingestion_repository.resume_or_claim_next(
            finished_at - timedelta(seconds=2)
        )
        assert acquisition is not None
        assert acquisition.ingestion.uuid == ingestion_uuid
        ingestion_repository.record_rejected(
            ingestion_uuid,
            rejection_category="invalid_audio",
            rejection_code="audio_empty",
            attachment_id="synthetic-attachment",
            attachment_size_bytes=0,
            now=finished_at - timedelta(seconds=1),
        )
        ingestion_repository.record_failed(ingestion_uuid, now=finished_at)
        MailIngestionEventRepository(session).append(
            ingestion_uuid,
            occurred_at=finished_at,
            status=MailIngestionStatus.FAILED,
            stage=MailIngestionStage.ACKNOWLEDGEMENT,
            disposition=MailIngestionDisposition.REJECTED,
            event_type=MailIngestionEventType.MESSAGE_REJECTED,
            error_scope=MailIngestionErrorScope.MESSAGE,
            error_category="invalid_audio",
            technical_code="audio_empty",
        )


def _event_count(session: Session, ingestion_uuid: UUID) -> int:
    count = session.scalar(
        select(func.count())
        .select_from(MailIngestionEvent)
        .where(MailIngestionEvent.ingestion_uuid == ingestion_uuid)
    )
    return int(count or 0)
