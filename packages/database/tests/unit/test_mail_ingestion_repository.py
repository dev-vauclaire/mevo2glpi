from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import MagicMock, create_autospec
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session

from glpi_ticketing_database.models import (
    MailIngestion,
    MailIngestionDisposition,
    MailIngestionErrorScope,
    MailIngestionStage,
    MailIngestionStatus,
)
from glpi_ticketing_database.repositories import (
    DiscoveredMail,
    InvalidMailIngestionTransitionError,
    MailIngestionAcquisition,
    MailIngestionRepository,
)

NOW = datetime(2026, 9, 8, 12, tzinfo=UTC)
INGESTION_UUID = UUID("7c614532-f8b9-4bc3-b853-247487eb0be2")
MAILBOX_UUID = UUID("94023353-3fb5-45fa-8336-a938e1dc914c")


def test_add_discovered_adds_only_a_pending_mail_ingestion() -> None:
    session = _session()

    snapshot = MailIngestionRepository(session).add_discovered(
        DiscoveredMail(
            uuid=INGESTION_UUID,
            mailbox_id=MAILBOX_UUID,
            message_id="immutable-message-id",
            received_at=NOW - timedelta(minutes=2),
        ),
        now=NOW,
    )

    added = session.add.call_args.args[0]
    assert isinstance(added, MailIngestion)
    assert snapshot.status is MailIngestionStatus.PENDING
    assert snapshot.stage is MailIngestionStage.DISCOVERY
    assert snapshot.attempt_count == 0
    assert snapshot.first_started_at is None
    session.flush.assert_called_once_with()


def test_get_by_source_uses_the_case_sensitive_opaque_identity() -> None:
    session = _session()
    ingestion = _ingestion()
    session.scalars.return_value = _scalar_result(ingestion)

    snapshot = MailIngestionRepository(session).get_by_source(
        MAILBOX_UUID,
        "immutable-message-id",
    )

    assert snapshot is not None
    assert snapshot.uuid == INGESTION_UUID
    session.scalars.assert_called_once()
    session.flush.assert_not_called()


@pytest.mark.parametrize("message_id", ["", "x" * 513, "opaque\nidentifier"])
def test_add_discovered_rejects_invalid_bounded_message_ids(
    message_id: str,
) -> None:
    session = _session()

    with pytest.raises(ValueError, match="bounded printable identifier"):
        MailIngestionRepository(session).add_discovered(
            DiscoveredMail(
                uuid=INGESTION_UUID,
                mailbox_id=MAILBOX_UUID,
                message_id=message_id,
                received_at=NOW,
            ),
            now=NOW,
        )

    session.add.assert_not_called()
    session.flush.assert_not_called()


def test_add_discovered_requires_strict_utc_timestamps() -> None:
    session = _session()

    with pytest.raises(ValueError, match="UTC"):
        MailIngestionRepository(session).add_discovered(
            DiscoveredMail(
                uuid=INGESTION_UUID,
                mailbox_id=MAILBOX_UUID,
                message_id="immutable-message-id",
                received_at=NOW,
            ),
            now=datetime(2026, 9, 8, 12, tzinfo=timezone(timedelta(hours=2))),
        )

    session.add.assert_not_called()


def test_processing_recovery_has_priority_and_preserves_stage() -> None:
    session = _session()
    ingestion = _ingestion(
        status=MailIngestionStatus.PROCESSING,
        stage=MailIngestionStage.PUBLICATION,
    )
    session.scalars.return_value = _scalar_result(ingestion)

    acquisition = MailIngestionRepository(session).resume_or_claim_next(NOW)

    assert acquisition == MailIngestionAcquisition(
        ingestion=acquisition.ingestion,  # type: ignore[union-attr]
        previous_status=MailIngestionStatus.PROCESSING,
        resumed=True,
    )
    assert acquisition is not None
    assert acquisition.ingestion.stage is MailIngestionStage.PUBLICATION
    assert acquisition.ingestion.attempt_count == 2
    statement = session.scalars.call_args.args[0]
    assert statement._for_update_arg.skip_locked is True
    assert session.scalars.call_count == 1


def test_due_retry_precedes_pending_and_preserves_reached_stage() -> None:
    session = _session()
    ingestion = _ingestion(
        status=MailIngestionStatus.RETRY_SCHEDULED,
        stage=MailIngestionStage.ACKNOWLEDGEMENT,
        disposition=MailIngestionDisposition.REJECTED,
    )
    ingestion.rejection_category = "invalid_audio"
    ingestion.rejection_code = "empty_file"
    ingestion.next_attempt_at = NOW
    ingestion.last_error_scope = MailIngestionErrorScope.SYSTEM.value
    ingestion.last_error_category = "unavailable"
    ingestion.last_error_code = "http_503"
    ingestion.consecutive_retry_count = 2
    session.scalars.side_effect = [
        _scalar_result(None),
        _scalar_result(ingestion),
    ]

    acquisition = MailIngestionRepository(session).resume_or_claim_next(NOW)

    assert acquisition is not None
    assert acquisition.previous_status is MailIngestionStatus.RETRY_SCHEDULED
    assert acquisition.ingestion.status is MailIngestionStatus.PROCESSING
    assert acquisition.ingestion.stage is MailIngestionStage.ACKNOWLEDGEMENT
    assert acquisition.ingestion.disposition is MailIngestionDisposition.REJECTED
    assert acquisition.ingestion.consecutive_retry_count == 2
    assert acquisition.ingestion.last_error_scope is None
    assert session.scalar.call_count == 0


def test_future_system_retry_suspends_pending_fanout() -> None:
    session = _session()
    session.scalars.side_effect = [_scalar_result(None), _scalar_result(None)]
    session.scalar.return_value = INGESTION_UUID

    assert MailIngestionRepository(session).resume_or_claim_next(NOW) is None

    assert session.scalars.call_count == 2
    session.flush.assert_not_called()


def test_pending_claim_enters_validation_and_initializes_attempt_timestamps() -> None:
    session = _session()
    pending = _ingestion(
        status=MailIngestionStatus.PENDING,
        stage=MailIngestionStage.DISCOVERY,
    )
    pending.attempt_count = 0
    pending.first_started_at = None
    session.scalars.side_effect = [
        _scalar_result(None),
        _scalar_result(None),
        _scalar_result(pending),
    ]
    session.scalar.return_value = None

    acquisition = MailIngestionRepository(session).resume_or_claim_next(NOW)

    assert acquisition is not None
    assert acquisition.previous_status is MailIngestionStatus.PENDING
    assert acquisition.ingestion.stage is MailIngestionStage.VALIDATION
    assert acquisition.ingestion.attempt_count == 1
    assert acquisition.ingestion.first_started_at == NOW


def test_accepted_lifecycle_links_but_never_creates_the_job() -> None:
    session = _session()
    ingestion = _ingestion()
    session.get.return_value = ingestion
    repository = MailIngestionRepository(session)

    validated = repository.record_attachment_validated(
        INGESTION_UUID,
        attachment_id="opaque-attachment-id",
        attachment_size_bytes=2048,
        now=NOW,
    )
    job_uuid = uuid4()
    accepted = repository.record_accepted(
        INGESTION_UUID,
        job_uuid=job_uuid,
        now=NOW + timedelta(seconds=1),
    )
    completed = repository.record_completed(
        INGESTION_UUID,
        now=NOW + timedelta(seconds=2),
    )

    assert validated.stage is MailIngestionStage.PUBLICATION
    assert accepted.stage is MailIngestionStage.ACKNOWLEDGEMENT
    assert accepted.disposition is MailIngestionDisposition.ACCEPTED
    assert accepted.job_uuid == job_uuid
    assert completed.status is MailIngestionStatus.COMPLETED
    assert completed.finished_at == NOW + timedelta(seconds=2)
    session.add.assert_not_called()


def test_rejected_lifecycle_accepts_empty_attachment_metadata() -> None:
    session = _session()
    ingestion = _ingestion()
    session.get.return_value = ingestion
    repository = MailIngestionRepository(session)

    rejected = repository.record_rejected(
        INGESTION_UUID,
        rejection_category="invalid_audio",
        rejection_code="empty_file",
        attachment_id="opaque-attachment-id",
        attachment_size_bytes=0,
        now=NOW,
    )
    failed = repository.record_failed(
        INGESTION_UUID,
        now=NOW + timedelta(seconds=1),
    )

    assert rejected.disposition is MailIngestionDisposition.REJECTED
    assert rejected.attachment_size_bytes == 0
    assert rejected.job_uuid is None
    assert failed.status is MailIngestionStatus.FAILED
    assert failed.last_error_scope is None


def test_rejection_is_allowed_after_download_before_validation_result() -> None:
    session = _session()
    ingestion = _ingestion(stage=MailIngestionStage.PUBLICATION)
    ingestion.attachment_id = "opaque-attachment-id"
    ingestion.attachment_size_bytes = 2048
    session.get.return_value = ingestion

    rejected = MailIngestionRepository(session).record_rejected(
        INGESTION_UUID,
        rejection_category="invalid_audio",
        rejection_code="invalid_container",
        now=NOW,
    )

    assert rejected.stage is MailIngestionStage.ACKNOWLEDGEMENT
    assert rejected.disposition is MailIngestionDisposition.REJECTED
    assert rejected.attachment_id == "opaque-attachment-id"
    assert rejected.attachment_size_bytes == 2048


def test_retry_and_blocked_transitions_keep_only_safe_diagnostics() -> None:
    session = _session()
    ingestion = _ingestion()
    session.get.return_value = ingestion
    repository = MailIngestionRepository(session)

    retry = repository.schedule_retry(
        INGESTION_UUID,
        stage=MailIngestionStage.VALIDATION,
        error_scope=MailIngestionErrorScope.SYSTEM,
        error_category="unavailable",
        error_code="http_503",
        next_attempt_at=NOW + timedelta(seconds=30),
        now=NOW,
    )

    assert retry.status is MailIngestionStatus.RETRY_SCHEDULED
    assert retry.consecutive_retry_count == 1
    ingestion.status = MailIngestionStatus.PROCESSING.value
    ingestion.next_attempt_at = None
    blocked = repository.record_blocked(
        INGESTION_UUID,
        stage=MailIngestionStage.VALIDATION,
        error_scope=MailIngestionErrorScope.SYSTEM,
        error_category="configuration",
        error_code="graph_forbidden",
        now=NOW + timedelta(seconds=1),
    )

    assert blocked.status is MailIngestionStatus.BLOCKED
    assert blocked.last_error_code == "graph_forbidden"
    assert blocked.finished_at == NOW + timedelta(seconds=1)


def test_wrong_stage_and_unsafe_identifier_are_rejected_before_mutation() -> None:
    session = _session()
    ingestion = _ingestion(
        stage=MailIngestionStage.ACKNOWLEDGEMENT,
        disposition=MailIngestionDisposition.ACCEPTED,
    )
    ingestion.attachment_id = "opaque-attachment-id"
    ingestion.attachment_size_bytes = 128
    ingestion.job_uuid = uuid4()
    session.get.return_value = ingestion
    repository = MailIngestionRepository(session)

    with pytest.raises(InvalidMailIngestionTransitionError):
        repository.record_rejected(
            INGESTION_UUID,
            rejection_category="invalid_audio",
            rejection_code="bad_container",
            now=NOW,
        )
    with pytest.raises(ValueError, match="safe identifier"):
        repository.record_blocked(
            INGESTION_UUID,
            stage=MailIngestionStage.PUBLICATION,
            error_scope=MailIngestionErrorScope.SYSTEM,
            error_category="secret=value",
            error_code="invalid",
            now=NOW,
        )

    session.add.assert_not_called()
    session.flush.assert_not_called()


def test_mail_ingestion_repository_never_owns_transaction_or_session() -> None:
    session = _session()
    session.scalars.side_effect = [_scalar_result(None), _scalar_result(None)]
    session.scalar.return_value = INGESTION_UUID

    MailIngestionRepository(session).resume_or_claim_next(NOW)

    session.commit.assert_not_called()
    session.rollback.assert_not_called()
    session.close.assert_not_called()


def _session() -> Session:
    return create_autospec(Session, instance=True)


def _scalar_result(value: MailIngestion | None) -> MagicMock:
    result = MagicMock()
    result.first.return_value = value
    return result


def _ingestion(
    *,
    status: MailIngestionStatus = MailIngestionStatus.PROCESSING,
    stage: MailIngestionStage = MailIngestionStage.VALIDATION,
    disposition: MailIngestionDisposition | None = None,
) -> MailIngestion:
    return MailIngestion(
        uuid=INGESTION_UUID,
        mailbox_id=MAILBOX_UUID,
        message_id="immutable-message-id",
        attachment_id=None,
        job_uuid=None,
        status=status.value,
        stage=stage.value,
        disposition=disposition.value if disposition is not None else None,
        attachment_size_bytes=None,
        attempt_count=1,
        consecutive_retry_count=0,
        next_attempt_at=None,
        rejection_category=None,
        rejection_code=None,
        last_error_scope=None,
        last_error_category=None,
        last_error_code=None,
        received_at=NOW - timedelta(minutes=2),
        created_at=NOW - timedelta(minutes=1),
        first_started_at=NOW - timedelta(seconds=30),
        updated_at=NOW - timedelta(seconds=1),
        finished_at=None,
    )
