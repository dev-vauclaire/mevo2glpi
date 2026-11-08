"""PostgreSQL contracts for model-scoped aggregate purge primitives."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import Engine, func, select

from mevo2glpi_database import (
    AudioExtension,
    AudioRetention,
    AudioRetentionRepository,
    DeletionReason,
    JobEventActor,
    JobEventType,
    JobStage,
    JobStatus,
    MailIngestion,
    MailIngestionDisposition,
    MailIngestionEvent,
    MailIngestionEventRepository,
    MailIngestionEventType,
    MailIngestionStage,
    MailIngestionStatus,
    RetentionPolicy,
    RetentionStatus,
    TicketCreationJob,
    TicketCreationJobEvent,
    TicketCreationJobEventRepository,
    TicketCreationJobRepository,
    create_session_factory,
)
from mevo2glpi_database.repositories import MailIngestionRepository

pytestmark = pytest.mark.database

NOW = datetime(2026, 11, 8, 12, tzinfo=UTC)
JOB_UUID = UUID("00000000-0000-0000-0000-000000000901")
MAILBOX_UUID = UUID("00000000-0000-0000-0000-000000000902")
OTHER_JOB_UUID = UUID("00000000-0000-0000-0000-000000000903")
REJECTED_INGESTION_UUID = UUID("00000000-0000-0000-0000-000000000904")


def test_model_scoped_repositories_delete_one_complete_job_aggregate(
    migrated_engine: Engine,
) -> None:
    session_factory = create_session_factory(migrated_engine)
    _add_complete_aggregate(session_factory)
    _add_preserved_rows(session_factory)

    with session_factory.begin() as session:
        ingestion = MailIngestionRepository(session).get_by_job_uuid(JOB_UUID)
        assert ingestion is not None
        assert (
            MailIngestionEventRepository(session).delete_by_ingestion_uuid(
                ingestion.uuid
            )
            == 1
        )
        MailIngestionRepository(session).delete_by_uuid(ingestion.uuid)
        assert (
            TicketCreationJobEventRepository(session).delete_by_job_uuid(JOB_UUID) == 1
        )
        AudioRetentionRepository(session).delete_by_job_uuid(JOB_UUID)
        TicketCreationJobRepository(session).delete_by_uuid(JOB_UUID)

    with session_factory() as session:
        assert session.get(TicketCreationJob, JOB_UUID) is None
        assert session.get(AudioRetention, JOB_UUID) is None
        assert session.get(MailIngestion, JOB_UUID) is None
        assert session.get(TicketCreationJob, OTHER_JOB_UUID) is not None
        assert session.get(AudioRetention, OTHER_JOB_UUID) is not None
        assert session.get(MailIngestion, OTHER_JOB_UUID) is not None
        rejected = session.get(MailIngestion, REJECTED_INGESTION_UUID)
        assert rejected is not None
        assert rejected.job_uuid is None
        assert rejected.disposition == MailIngestionDisposition.REJECTED.value
        assert (
            session.scalar(
                select(func.count())
                .select_from(TicketCreationJobEvent)
                .where(TicketCreationJobEvent.job_uuid == JOB_UUID)
            )
            == 0
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(MailIngestionEvent)
                .where(MailIngestionEvent.ingestion_uuid == JOB_UUID)
            )
            == 0
        )


def _add_complete_aggregate(session_factory: object) -> None:
    with session_factory.begin() as session:  # type: ignore[union-attr]
        session.add(
            TicketCreationJob(
                uuid=JOB_UUID,
                status=JobStatus.COMPLETED.value,
                stage=JobStage.FINALIZATION.value,
                storage_key=f"{JOB_UUID}.wav",
                audio_extension=AudioExtension.WAV.value,
                audio_size_bytes=128,
                glpi_ticket_id=901,
                attempt_count=1,
                consecutive_retry_count=0,
                created_at=NOW - timedelta(days=1),
                first_started_at=NOW - timedelta(days=1),
                updated_at=NOW,
                finished_at=NOW,
            )
        )
        session.flush()
        session.add_all(
            (
                MailIngestion(
                    uuid=JOB_UUID,
                    mailbox_id=MAILBOX_UUID,
                    message_id="immutable-message-901",
                    attachment_id="attachment-901",
                    job_uuid=JOB_UUID,
                    status=MailIngestionStatus.COMPLETED.value,
                    stage=MailIngestionStage.ACKNOWLEDGEMENT.value,
                    disposition=MailIngestionDisposition.ACCEPTED.value,
                    audio_extension=AudioExtension.WAV.value,
                    attachment_size_bytes=128,
                    attempt_count=1,
                    consecutive_retry_count=0,
                    received_at=NOW - timedelta(days=1),
                    created_at=NOW - timedelta(days=1),
                    first_started_at=NOW - timedelta(days=1),
                    updated_at=NOW,
                    finished_at=NOW,
                ),
                AudioRetention(
                    job_uuid=JOB_UUID,
                    policy=RetentionPolicy.TICKET_OR_DEADLINE.value,
                    status=RetentionStatus.DELETING.value,
                    hard_delete_at=NOW + timedelta(days=1),
                    check_attempt_count=1,
                    consecutive_retry_count=0,
                    deletion_reason=DeletionReason.TICKET_CLOSED.value,
                    deletion_started_at=NOW,
                    created_at=NOW - timedelta(days=1),
                    updated_at=NOW,
                ),
                TicketCreationJobEvent(
                    job_uuid=JOB_UUID,
                    occurred_at=NOW,
                    status=JobStatus.COMPLETED.value,
                    stage=JobStage.FINALIZATION.value,
                    event_type=JobEventType.AUDIO_DELETION_STARTED.value,
                    actor=JobEventActor.WORKER_RETENTION.value,
                ),
            )
        )
        session.flush()
        session.add(
            MailIngestionEvent(
                ingestion_uuid=JOB_UUID,
                occurred_at=NOW,
                status=MailIngestionStatus.COMPLETED.value,
                stage=MailIngestionStage.ACKNOWLEDGEMENT.value,
                disposition=MailIngestionDisposition.ACCEPTED.value,
                event_type=MailIngestionEventType.INGESTION_COMPLETED.value,
            )
        )


def _add_preserved_rows(session_factory: object) -> None:
    with session_factory.begin() as session:  # type: ignore[union-attr]
        session.add(
            TicketCreationJob(
                uuid=OTHER_JOB_UUID,
                status=JobStatus.COMPLETED.value,
                stage=JobStage.FINALIZATION.value,
                storage_key=f"{OTHER_JOB_UUID}.wav",
                audio_extension=AudioExtension.WAV.value,
                audio_size_bytes=256,
                glpi_ticket_id=903,
                attempt_count=1,
                consecutive_retry_count=0,
                created_at=NOW - timedelta(days=1),
                first_started_at=NOW - timedelta(days=1),
                updated_at=NOW,
                finished_at=NOW,
            )
        )
        session.flush()
        session.add_all(
            (
                MailIngestion(
                    uuid=OTHER_JOB_UUID,
                    mailbox_id=MAILBOX_UUID,
                    message_id="immutable-message-903",
                    attachment_id="attachment-903",
                    job_uuid=OTHER_JOB_UUID,
                    status=MailIngestionStatus.COMPLETED.value,
                    stage=MailIngestionStage.ACKNOWLEDGEMENT.value,
                    disposition=MailIngestionDisposition.ACCEPTED.value,
                    audio_extension=AudioExtension.WAV.value,
                    attachment_size_bytes=256,
                    attempt_count=1,
                    consecutive_retry_count=0,
                    received_at=NOW - timedelta(days=1),
                    created_at=NOW - timedelta(days=1),
                    first_started_at=NOW - timedelta(days=1),
                    updated_at=NOW,
                    finished_at=NOW,
                ),
                AudioRetention(
                    job_uuid=OTHER_JOB_UUID,
                    policy=RetentionPolicy.TICKET_OR_DEADLINE.value,
                    status=RetentionStatus.SCHEDULED.value,
                    hard_delete_at=NOW + timedelta(days=1),
                    next_check_at=NOW + timedelta(hours=1),
                    check_attempt_count=0,
                    consecutive_retry_count=0,
                    created_at=NOW - timedelta(days=1),
                    updated_at=NOW,
                ),
                MailIngestion(
                    uuid=REJECTED_INGESTION_UUID,
                    mailbox_id=MAILBOX_UUID,
                    message_id="immutable-message-rejected-904",
                    status=MailIngestionStatus.FAILED.value,
                    stage=MailIngestionStage.ACKNOWLEDGEMENT.value,
                    disposition=MailIngestionDisposition.REJECTED.value,
                    attempt_count=1,
                    consecutive_retry_count=0,
                    rejection_category="invalid_attachment",
                    rejection_code="audio_attachment_missing",
                    received_at=NOW - timedelta(days=1),
                    created_at=NOW - timedelta(days=1),
                    first_started_at=NOW - timedelta(days=1),
                    updated_at=NOW,
                    finished_at=NOW,
                ),
            )
        )
