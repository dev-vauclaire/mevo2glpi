from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.exc import IntegrityError

from mevo2glpi_database import create_session_factory
from mevo2glpi_database.models import (
    AudioExtension,
    MailIngestion,
    MailIngestionDisposition,
    MailIngestionErrorScope,
    MailIngestionStage,
    MailIngestionStatus,
)
from mevo2glpi_database.repositories import (
    DiscoveredMail,
    MailIngestionReplayFilter,
    MailIngestionRepository,
    PendingJob,
    TicketCreationJobRepository,
)

pytestmark = pytest.mark.database

NOW = datetime(2026, 9, 8, 12, tzinfo=UTC)
MAILBOX_UUID = UUID("94023353-3fb5-45fa-8336-a938e1dc914c")


def test_source_identity_is_idempotent_but_distinct_messages_are_allowed(
    migrated_engine: Engine,
) -> None:
    session_factory = create_session_factory(migrated_engine)
    first_uuid = UUID("00000000-0000-0000-0000-000000000101")
    duplicate_uuid = UUID("00000000-0000-0000-0000-000000000102")
    distinct_uuid = UUID("00000000-0000-0000-0000-000000000103")

    with session_factory.begin() as session:
        repository = MailIngestionRepository(session)
        repository.add_discovered(
            _discovered(first_uuid, "immutable-message-a"),
            now=NOW,
        )
        repository.add_discovered(
            _discovered(distinct_uuid, "immutable-message-b"),
            now=NOW,
        )

    with pytest.raises(IntegrityError):
        with session_factory.begin() as session:
            MailIngestionRepository(session).add_discovered(
                _discovered(duplicate_uuid, "immutable-message-a"),
                now=NOW,
            )

    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(MailIngestion)) == 2
        assert (
            MailIngestionRepository(session)
            .get_by_source(MAILBOX_UUID, "immutable-message-a")
            .uuid
            == first_uuid
        )


def test_protected_originating_number_is_persisted_then_cleared_by_job(
    migrated_engine: Engine,
) -> None:
    session_factory = create_session_factory(migrated_engine)
    ingestion_uuid = UUID("00000000-0000-0000-0000-000000000104")
    ciphertext = b"versioned-authenticated-ciphertext"

    with session_factory.begin() as session:
        repository = MailIngestionRepository(session)
        repository.add_discovered(
            DiscoveredMail(
                uuid=ingestion_uuid,
                mailbox_id=MAILBOX_UUID,
                message_id="immutable-message-protected",
                received_at=NOW,
                originating_phone_number_ciphertext=ciphertext,
            ),
            now=NOW,
        )
        repository.resume_or_claim_next(NOW)
        repository.record_attachment_validated(
            ingestion_uuid,
            attachment_id="attachment-protected",
            audio_extension=AudioExtension.WAV,
            attachment_size_bytes=128,
            now=NOW + timedelta(seconds=1),
        )
        TicketCreationJobRepository(session).add_pending(
            PendingJob(
                uuid=ingestion_uuid,
                storage_key=f"{ingestion_uuid}.wav",
                audio_extension=AudioExtension.WAV,
                audio_size_bytes=128,
            )
        )
        repository.record_accepted(
            ingestion_uuid,
            job_uuid=ingestion_uuid,
            now=NOW + timedelta(seconds=2),
        )
        repository.record_completed(
            ingestion_uuid,
            now=NOW + timedelta(seconds=3),
        )

    with session_factory.begin() as session:
        repository = MailIngestionRepository(session)
        before = repository.get_by_uuid(ingestion_uuid)
        assert before is not None
        assert before.originating_phone_number_ciphertext == ciphertext

        cleared = repository.clear_originating_phone_number_ciphertext(
            ingestion_uuid,
            now=NOW + timedelta(seconds=4),
        )
        assert cleared is not None
        assert cleared.originating_phone_number_ciphertext is None

        cleared_again = repository.clear_originating_phone_number_ciphertext(
            ingestion_uuid,
            now=NOW + timedelta(seconds=5),
        )
        assert cleared_again is not None
        assert cleared_again.originating_phone_number_ciphertext is None


def test_claim_priority_and_both_terminal_dispositions(
    migrated_engine: Engine,
) -> None:
    session_factory = create_session_factory(migrated_engine)
    accepted_uuid = UUID("00000000-0000-0000-0000-000000000111")
    rejected_uuid = UUID("00000000-0000-0000-0000-000000000112")
    job_uuid = UUID("00000000-0000-0000-0000-000000000113")

    with session_factory.begin() as session:
        repository = MailIngestionRepository(session)
        repository.add_discovered(
            _discovered(accepted_uuid, "immutable-message-old", minutes_ago=2),
            now=NOW,
        )
        repository.add_discovered(
            _discovered(rejected_uuid, "immutable-message-new", minutes_ago=1),
            now=NOW,
        )

    with session_factory.begin() as session:
        repository = MailIngestionRepository(session)
        acquisition = repository.resume_or_claim_next(NOW)
        assert acquisition is not None
        assert acquisition.ingestion.uuid == accepted_uuid
        repository.record_attachment_validated(
            accepted_uuid,
            attachment_id="attachment-a",
            audio_extension=AudioExtension.WAV,
            attachment_size_bytes=128,
            now=NOW + timedelta(seconds=1),
        )
        TicketCreationJobRepository(session).add_pending(
            PendingJob(
                uuid=job_uuid,
                storage_key=f"{job_uuid}.wav",
                audio_extension=AudioExtension.WAV,
                audio_size_bytes=128,
            )
        )
        repository.record_accepted(
            accepted_uuid,
            job_uuid=job_uuid,
            now=NOW + timedelta(seconds=2),
        )
        repository.record_completed(
            accepted_uuid,
            now=NOW + timedelta(seconds=3),
        )

    with session_factory.begin() as session:
        repository = MailIngestionRepository(session)
        acquisition = repository.resume_or_claim_next(NOW + timedelta(seconds=4))
        assert acquisition is not None
        assert acquisition.ingestion.uuid == rejected_uuid
        repository.record_rejected(
            rejected_uuid,
            rejection_category="invalid_audio",
            rejection_code="empty_file",
            attachment_id="attachment-b",
            attachment_size_bytes=0,
            now=NOW + timedelta(seconds=5),
        )
        repository.record_failed(
            rejected_uuid,
            now=NOW + timedelta(seconds=6),
        )

    with session_factory() as session:
        accepted = MailIngestionRepository(session).get_by_uuid(accepted_uuid)
        rejected = MailIngestionRepository(session).get_by_uuid(rejected_uuid)
        assert accepted is not None
        assert accepted.status is MailIngestionStatus.COMPLETED
        assert accepted.disposition is MailIngestionDisposition.ACCEPTED
        assert accepted.job_uuid == job_uuid
        assert rejected is not None
        assert rejected.status is MailIngestionStatus.FAILED
        assert rejected.disposition is MailIngestionDisposition.REJECTED
        assert rejected.attachment_size_bytes == 0


def test_future_system_retry_prevents_claiming_another_message(
    migrated_engine: Engine,
) -> None:
    session_factory = create_session_factory(migrated_engine)
    retry_uuid = UUID("00000000-0000-0000-0000-000000000121")
    pending_uuid = UUID("00000000-0000-0000-0000-000000000122")

    with session_factory.begin() as session:
        repository = MailIngestionRepository(session)
        repository.add_discovered(
            _discovered(retry_uuid, "immutable-retry", minutes_ago=2),
            now=NOW,
        )
        repository.add_discovered(
            _discovered(pending_uuid, "immutable-pending", minutes_ago=1),
            now=NOW,
        )
    with session_factory.begin() as session:
        repository = MailIngestionRepository(session)
        acquisition = repository.resume_or_claim_next(NOW)
        assert acquisition is not None
        assert acquisition.ingestion.uuid == retry_uuid
        repository.schedule_retry(
            retry_uuid,
            stage=MailIngestionStage.VALIDATION,
            error_scope=MailIngestionErrorScope.SYSTEM,
            error_category="unavailable",
            error_code="graph_unavailable",
            next_attempt_at=NOW + timedelta(minutes=1),
            now=NOW,
        )
    with session_factory.begin() as session:
        repository = MailIngestionRepository(session)
        assert repository.resume_or_claim_next(NOW + timedelta(seconds=30)) is None
        assert repository.get_next_system_retry_at() == NOW + timedelta(minutes=1)

    with session_factory() as session:
        pending = session.get(MailIngestion, pending_uuid)
        assert pending is not None
        assert pending.status == MailIngestionStatus.PENDING.value


def test_operator_replay_filters_and_rearms_one_rejected_ingestion(
    migrated_engine: Engine,
) -> None:
    session_factory = create_session_factory(migrated_engine)
    ingestion_uuid = UUID("00000000-0000-0000-0000-000000000131")
    with session_factory.begin() as session:
        repository = MailIngestionRepository(session)
        repository.add_discovered(
            _discovered(ingestion_uuid, "immutable-rejected-ogg"),
            now=NOW,
        )
        repository.resume_or_claim_next(NOW + timedelta(seconds=1))
        repository.record_rejected(
            ingestion_uuid,
            rejection_category="invalid_attachment",
            rejection_code="wav_attachment_missing",
            attachment_id="opaque-ogg-attachment",
            attachment_size_bytes=2048,
            now=NOW + timedelta(seconds=2),
        )
        repository.record_failed(
            ingestion_uuid,
            now=NOW + timedelta(seconds=3),
        )

    replay_filter = MailIngestionReplayFilter(
        status=MailIngestionStatus.FAILED,
        disposition=MailIngestionDisposition.REJECTED,
        rejection_category="invalid_attachment",
        rejection_code="wav_attachment_missing",
        received_after=NOW - timedelta(days=1),
        received_before=NOW + timedelta(days=1),
        limit=100,
        job_uuid_is_null=True,
        attachment_id_is_not_null=True,
    )
    with session_factory.begin() as session:
        repository = MailIngestionRepository(session)
        candidates = repository.list_replay_candidates(replay_filter)
        assert [candidate.uuid for candidate in candidates] == [ingestion_uuid]
        original_attempt_count = candidates[0].attempt_count
        original_first_started_at = candidates[0].first_started_at
        replayed = repository.rearm_for_operator_replay(
            ingestion_uuid,
            expected=replay_filter,
            now=NOW + timedelta(seconds=4),
        )

    assert replayed.status is MailIngestionStatus.PENDING
    assert replayed.stage is MailIngestionStage.DISCOVERY
    assert replayed.disposition is None
    assert replayed.attachment_id is None
    assert replayed.audio_extension is None
    assert replayed.attachment_size_bytes is None
    assert replayed.rejection_category is None
    assert replayed.rejection_code is None
    assert replayed.attempt_count == original_attempt_count
    assert replayed.first_started_at == original_first_started_at

    with session_factory() as session:
        assert not MailIngestionRepository(session).list_replay_candidates(
            replay_filter
        )


def _discovered(
    ingestion_uuid: UUID,
    message_id: str,
    *,
    minutes_ago: int = 1,
) -> DiscoveredMail:
    return DiscoveredMail(
        uuid=ingestion_uuid,
        mailbox_id=MAILBOX_UUID,
        message_id=message_id,
        received_at=NOW - timedelta(minutes=minutes_ago),
    )
