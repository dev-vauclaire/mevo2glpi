"""PostgreSQL contracts for the publisher application lifecycle."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import Engine, select

from glpi_ticketing_common import OriginatingPhoneNumberCipher
from glpi_ticketing_database import (
    AudioExtension,
    JobEventActor,
    JobEventType,
    MailIngestion,
    MailIngestionDisposition,
    MailIngestionErrorScope,
    MailIngestionEvent,
    MailIngestionEventType,
    MailIngestionRepository,
    MailIngestionStatus,
    TicketCreationJob,
    TicketCreationJobEvent,
    create_session_factory,
)
from glpi_ticketing_worker_publisher.graph import (
    AttachmentId,
    FolderId,
    GraphAttachment,
    GraphMessage,
    MessageId,
)
from glpi_ticketing_worker_publisher.publisher import (
    PublisherFailure,
    PublisherLifecycleService,
)

pytestmark = pytest.mark.database

NOW = datetime(2026, 9, 8, 12, tzinfo=UTC)
MAILBOX_ID = UUID("94023353-3fb5-45fa-8336-a938e1dc914c")


def _phone_number_cipher() -> OriginatingPhoneNumberCipher:
    return OriginatingPhoneNumberCipher(b"x" * 32)


def _message(
    message_id: str,
    *,
    minutes_ago: int,
    subject: str | None = None,
) -> GraphMessage:
    return GraphMessage(
        message_id=MessageId(message_id),
        parent_folder_id=FolderId("inbox-folder"),
        received_at=NOW - timedelta(minutes=minutes_ago),
        has_attachments=True,
        subject=subject,
    )


def _attachment(size_bytes: int = 128) -> GraphAttachment:
    return GraphAttachment(
        attachment_id=AttachmentId("attachment-id"),
        name="message.wav",
        content_type="audio/wav",
        size_bytes=size_bytes,
        is_inline=False,
    )


def test_accepted_message_creates_one_aligned_job_and_complete_audit(
    migrated_engine: Engine,
) -> None:
    session_factory = create_session_factory(migrated_engine)
    ingestion_uuid = UUID("00000000-0000-4000-8000-000000000301")
    phone_number_cipher = _phone_number_cipher()
    lifecycle = PublisherLifecycleService(
        session_factory=session_factory,
        originating_phone_number_cipher=phone_number_cipher,
        uuid_factory=lambda: ingestion_uuid,
    )

    assert (
        lifecycle.discover_messages(
            mailbox_id=MAILBOX_ID,
            messages=(
                _message(
                    "immutable-message-a",
                    minutes_ago=1,
                    subject=(
                        "Message vocale de la part de06 78 37 43 63"
                        "pourMEVO CH-VAUCLAIRE 29 34 23 12 32"
                    ),
                ),
            ),
            now=NOW,
        )
        == 1
    )
    acquisition = lifecycle.resume_or_claim_next(NOW + timedelta(seconds=1))
    assert acquisition is not None
    lifecycle.record_attachment_validated(
        ingestion_uuid,
        _attachment(),
        AudioExtension.WAV,
        NOW + timedelta(seconds=2),
    )
    published = lifecycle.record_audio_published(
        ingestion_uuid,
        storage_key=f"{ingestion_uuid}.wav",
        size_bytes=128,
        now=NOW + timedelta(seconds=3),
    )
    assert published.job_uuid == ingestion_uuid
    lifecycle.record_message_moved(
        ingestion_uuid,
        now=NOW + timedelta(seconds=4),
    )

    with session_factory() as session:
        ingestion = session.get(MailIngestion, ingestion_uuid)
        job = session.get(TicketCreationJob, ingestion_uuid)
        ingestion_events = tuple(
            session.scalars(
                select(MailIngestionEvent)
                .where(MailIngestionEvent.ingestion_uuid == ingestion_uuid)
                .order_by(MailIngestionEvent.event_id)
            )
        )
        job_events = tuple(
            session.scalars(
                select(TicketCreationJobEvent).where(
                    TicketCreationJobEvent.job_uuid == ingestion_uuid
                )
            )
        )

    assert ingestion is not None
    assert ingestion.originating_phone_number_ciphertext is not None
    assert b"0678374363" not in ingestion.originating_phone_number_ciphertext
    assert (
        phone_number_cipher.decrypt(
            ingestion.originating_phone_number_ciphertext,
            ingestion_uuid,
        )
        == "0678374363"
    )
    assert ingestion.status == MailIngestionStatus.COMPLETED.value
    assert job is not None
    assert job.uuid == ingestion.uuid
    assert [event.event_type for event in ingestion_events] == [
        MailIngestionEventType.MESSAGE_DISCOVERED.value,
        MailIngestionEventType.PROCESSING_STARTED.value,
        MailIngestionEventType.ATTACHMENT_VALIDATED.value,
        MailIngestionEventType.AUDIO_PUBLISHED.value,
        MailIngestionEventType.MESSAGE_MOVED.value,
        MailIngestionEventType.INGESTION_COMPLETED.value,
    ]
    assert len(job_events) == 1
    assert job_events[0].event_type == JobEventType.JOB_CREATED.value
    assert job_events[0].actor == JobEventActor.WORKER_PUBLISHER.value


def test_ogg_attachment_creates_an_aligned_ogg_job(migrated_engine: Engine) -> None:
    session_factory = create_session_factory(migrated_engine)
    ingestion_uuid = UUID("00000000-0000-4000-8000-000000000302")
    lifecycle = PublisherLifecycleService(
        session_factory=session_factory,
        originating_phone_number_cipher=_phone_number_cipher(),
        uuid_factory=lambda: ingestion_uuid,
    )
    attachment = GraphAttachment(
        attachment_id=AttachmentId("ogg-attachment-id"),
        name="message.ogg",
        content_type="application/ogg",
        size_bytes=96,
        is_inline=False,
    )

    lifecycle.discover_messages(
        mailbox_id=MAILBOX_ID,
        messages=(_message("immutable-message-ogg", minutes_ago=1),),
        now=NOW,
    )
    assert lifecycle.resume_or_claim_next(NOW + timedelta(seconds=1)) is not None
    lifecycle.record_attachment_validated(
        ingestion_uuid,
        attachment,
        AudioExtension.OGG,
        NOW + timedelta(seconds=2),
    )
    lifecycle.record_audio_published(
        ingestion_uuid,
        storage_key=f"{ingestion_uuid}.ogg",
        size_bytes=attachment.size_bytes,
        now=NOW + timedelta(seconds=3),
    )

    with session_factory() as session:
        ingestion = MailIngestionRepository(session).get_by_uuid(ingestion_uuid)
        job = session.get(TicketCreationJob, ingestion_uuid)

    assert ingestion is not None
    assert ingestion.audio_extension is AudioExtension.OGG
    assert job is not None
    assert job.audio_extension == AudioExtension.OGG.value
    assert job.storage_key == f"{ingestion_uuid}.ogg"


def test_empty_download_can_be_rejected_after_nonempty_ogg_metadata(
    migrated_engine: Engine,
) -> None:
    session_factory = create_session_factory(migrated_engine)
    ingestion_uuid = UUID("00000000-0000-4000-8000-000000000303")
    lifecycle = PublisherLifecycleService(
        session_factory=session_factory,
        originating_phone_number_cipher=_phone_number_cipher(),
        uuid_factory=lambda: ingestion_uuid,
    )
    attachment = GraphAttachment(
        attachment_id=AttachmentId("empty-ogg-attachment-id"),
        name="message.ogg",
        content_type="application/ogg",
        size_bytes=128,
        is_inline=False,
    )

    lifecycle.discover_messages(
        mailbox_id=MAILBOX_ID,
        messages=(_message("immutable-message-empty-ogg", minutes_ago=1),),
        now=NOW,
    )
    assert lifecycle.resume_or_claim_next(NOW + timedelta(seconds=1)) is not None
    lifecycle.record_attachment_validated(
        ingestion_uuid,
        attachment,
        AudioExtension.OGG,
        NOW + timedelta(seconds=2),
    )

    rejected = lifecycle.record_rejected(
        ingestion_uuid,
        failure=PublisherFailure(
            scope=MailIngestionErrorScope.MESSAGE,
            category="invalid_audio",
            technical_code="audio_empty",
        ),
        actual_size_bytes=0,
        now=NOW + timedelta(seconds=3),
    )

    assert rejected.disposition is MailIngestionDisposition.REJECTED
    assert rejected.audio_extension is AudioExtension.OGG
    assert rejected.attachment_size_bytes == 0


def test_discovery_is_idempotent_and_equal_audio_messages_remain_distinct(
    migrated_engine: Engine,
) -> None:
    session_factory = create_session_factory(migrated_engine)
    generated = iter(
        (
            UUID("00000000-0000-4000-8000-000000000311"),
            UUID("00000000-0000-4000-8000-000000000312"),
        )
    )
    lifecycle = PublisherLifecycleService(
        session_factory=session_factory,
        originating_phone_number_cipher=_phone_number_cipher(),
        uuid_factory=lambda: next(generated),
    )
    first = _message("immutable-message-a", minutes_ago=2)
    second = _message("immutable-message-b", minutes_ago=1)

    assert (
        lifecycle.discover_messages(
            mailbox_id=MAILBOX_ID,
            messages=(first, second),
            now=NOW,
        )
        == 2
    )
    assert (
        lifecycle.discover_messages(
            mailbox_id=MAILBOX_ID,
            messages=(first, second),
            now=NOW + timedelta(seconds=1),
        )
        == 0
    )

    with session_factory() as session:
        first_ingestion = MailIngestionRepository(session).get_by_source(
            MAILBOX_ID,
            "immutable-message-a",
        )
        second_ingestion = MailIngestionRepository(session).get_by_source(
            MAILBOX_ID,
            "immutable-message-b",
        )

    assert first_ingestion is not None
    assert second_ingestion is not None
    assert first_ingestion.uuid != second_ingestion.uuid

    for offset, ingestion_uuid in enumerate(
        (first_ingestion.uuid, second_ingestion.uuid),
        start=1,
    ):
        acquisition = lifecycle.resume_or_claim_next(
            NOW + timedelta(seconds=offset * 10)
        )
        assert acquisition is not None
        assert acquisition.ingestion.uuid == ingestion_uuid
        lifecycle.record_attachment_validated(
            ingestion_uuid,
            _attachment(),
            AudioExtension.WAV,
            NOW + timedelta(seconds=offset * 10 + 1),
        )
        lifecycle.record_audio_published(
            ingestion_uuid,
            storage_key=f"{ingestion_uuid}.wav",
            size_bytes=128,
            now=NOW + timedelta(seconds=offset * 10 + 2),
        )
        lifecycle.record_message_moved(
            ingestion_uuid,
            now=NOW + timedelta(seconds=offset * 10 + 3),
        )

    with session_factory() as session:
        jobs = tuple(session.scalars(select(TicketCreationJob)))
    assert {job.uuid for job in jobs} == {
        first_ingestion.uuid,
        second_ingestion.uuid,
    }
