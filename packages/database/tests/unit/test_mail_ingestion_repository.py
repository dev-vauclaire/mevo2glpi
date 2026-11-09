from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, create_autospec
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session

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
    InvalidMailIngestionTransitionError,
    MailIngestionAcquisition,
    MailIngestionCursor,
    MailIngestionPage,
    MailIngestionReplayFilter,
    MailIngestionRepository,
    RejectedMailIngestionSnapshot,
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


def test_add_discovered_persists_only_bounded_protected_caller_metadata() -> None:
    session = _session()
    ciphertext = b"protected-originating-phone-number"

    snapshot = MailIngestionRepository(session).add_discovered(
        DiscoveredMail(
            uuid=INGESTION_UUID,
            mailbox_id=MAILBOX_UUID,
            message_id="immutable-message-id",
            received_at=NOW,
            originating_phone_number_ciphertext=ciphertext,
        ),
        now=NOW,
    )

    added = session.add.call_args.args[0]
    assert added.originating_phone_number_ciphertext == ciphertext
    assert snapshot.originating_phone_number_ciphertext == ciphertext


@pytest.mark.parametrize(
    "ciphertext,expected_error",
    [
        (b"", ValueError),
        (b"x" * 129, ValueError),
        ("plaintext", TypeError),
    ],
)
def test_add_discovered_rejects_invalid_protected_caller_metadata(
    ciphertext: object,
    expected_error: type[Exception],
) -> None:
    session = _session()

    with pytest.raises(expected_error):
        MailIngestionRepository(session).add_discovered(
            DiscoveredMail(
                uuid=INGESTION_UUID,
                mailbox_id=MAILBOX_UUID,
                message_id="immutable-message-id",
                received_at=NOW,
                originating_phone_number_ciphertext=ciphertext,  # type: ignore[arg-type]
            ),
            now=NOW,
        )

    session.add.assert_not_called()
    session.flush.assert_not_called()


def test_clear_originating_phone_number_ciphertext_is_idempotent() -> None:
    session = _session()
    ingestion = _ingestion(disposition=MailIngestionDisposition.ACCEPTED)
    ingestion.job_uuid = INGESTION_UUID
    ingestion.originating_phone_number_ciphertext = b"protected"
    session.get.return_value = ingestion
    repository = MailIngestionRepository(session)

    cleared = repository.clear_originating_phone_number_ciphertext(
        INGESTION_UUID,
        now=NOW,
    )

    assert cleared is not None
    assert cleared.originating_phone_number_ciphertext is None
    assert cleared.updated_at == NOW
    session.flush.assert_called_once_with()

    session.flush.reset_mock()
    cleared_again = repository.clear_originating_phone_number_ciphertext(
        INGESTION_UUID,
        now=NOW,
    )

    assert cleared_again is not None
    assert cleared_again.originating_phone_number_ciphertext is None
    session.flush.assert_not_called()


def test_clear_originating_phone_number_ciphertext_rejects_missing_source() -> None:
    session = _session()
    session.get.return_value = None

    with pytest.raises(LookupError, match="does not exist"):
        MailIngestionRepository(session).clear_originating_phone_number_ciphertext(
            INGESTION_UUID,
            now=NOW,
        )

    session.flush.assert_not_called()


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


def test_get_by_job_uuid_returns_the_optional_linked_ingestion() -> None:
    session = _session()
    ingestion = _ingestion(
        status=MailIngestionStatus.COMPLETED,
        stage=MailIngestionStage.ACKNOWLEDGEMENT,
        disposition=MailIngestionDisposition.ACCEPTED,
    )
    ingestion.job_uuid = INGESTION_UUID
    ingestion.finished_at = NOW
    session.scalars.return_value = _scalar_result(ingestion)

    snapshot = MailIngestionRepository(session).get_by_job_uuid(INGESTION_UUID)

    assert snapshot is not None
    assert snapshot.uuid == INGESTION_UUID
    assert snapshot.job_uuid == INGESTION_UUID
    session.scalars.assert_called_once()
    session.flush.assert_not_called()


def test_list_rejected_returns_safe_deterministic_page() -> None:
    session = _session()
    first = _rejected_failed_ingestion()
    second = _rejected_failed_ingestion()
    second.uuid = UUID("8dfd2d0b-27a8-4962-ae21-71739e92d218")
    second.finished_at = NOW - timedelta(seconds=2)
    session.execute.return_value.all.return_value = [
        _rejected_row(first),
        _rejected_row(second),
    ]

    page = MailIngestionRepository(session).list_rejected(page_size=1)

    assert page == MailIngestionPage(
        items=(
            RejectedMailIngestionSnapshot(
                uuid=INGESTION_UUID,
                rejection_category="invalid_attachment",
                rejection_code="wav_attachment_missing",
                received_at=NOW - timedelta(minutes=2),
                finished_at=NOW - timedelta(seconds=1),
            ),
        ),
        next_cursor=MailIngestionCursor(
            finished_at=NOW - timedelta(seconds=1),
            uuid=INGESTION_UUID,
        ),
        has_more=True,
    )
    assert not hasattr(page.items[0], "message_id")
    statement = session.execute.call_args.args[0]
    assert statement._limit_clause.value == 2
    assert tuple(column.key for column in statement.selected_columns) == (
        "uuid",
        "rejection_category",
        "rejection_code",
        "received_at",
        "finished_at",
    )
    session.flush.assert_not_called()


def test_list_rejected_applies_a_stable_keyset_cursor() -> None:
    session = _session()
    session.execute.return_value.all.return_value = []

    page = MailIngestionRepository(session).list_rejected(
        page_size=10,
        cursor=MailIngestionCursor(
            finished_at=NOW - timedelta(hours=1),
            uuid=INGESTION_UUID,
        ),
    )

    assert page == MailIngestionPage(items=(), next_cursor=None, has_more=False)
    statement = session.execute.call_args.args[0]
    assert len(statement._where_criteria) == 2


def test_count_expired_rejected_uses_a_strict_utc_cutoff() -> None:
    session = _session()
    session.scalar.return_value = 4

    count = MailIngestionRepository(session).count_expired_rejected(NOW)

    assert count == 4
    session.scalar.assert_called_once()
    session.flush.assert_not_called()

    with pytest.raises(ValueError, match="UTC"):
        MailIngestionRepository(session).count_expired_rejected(
            datetime(2026, 9, 8, 14, tzinfo=timezone(timedelta(hours=2)))
        )


def test_expired_rejected_batch_is_bounded_ordered_and_locked() -> None:
    session = _session()
    rejected = _rejected_failed_ingestion()
    session.scalars.return_value.all.return_value = [rejected]

    candidates = MailIngestionRepository(session).list_expired_rejected_for_update(
        NOW, 100
    )

    assert candidates == (
        RejectedMailIngestionSnapshot(
            uuid=INGESTION_UUID,
            rejection_category="invalid_attachment",
            rejection_code="wav_attachment_missing",
            received_at=NOW - timedelta(minutes=2),
            finished_at=NOW - timedelta(seconds=1),
        ),
    )
    statement = session.scalars.call_args.args[0]
    assert statement._limit_clause.value == 100
    assert statement._for_update_arg.skip_locked is True
    assert len(statement._order_by_clauses) == 2
    session.flush.assert_not_called()


@pytest.mark.parametrize("limit", [0, 101, True])
def test_rejected_queries_reject_unbounded_limits(limit: object) -> None:
    session = _session()

    with pytest.raises((TypeError, ValueError)):
        MailIngestionRepository(session).list_rejected(
            page_size=limit,  # type: ignore[arg-type]
        )
    with pytest.raises((TypeError, ValueError)):
        MailIngestionRepository(session).list_expired_rejected_for_update(
            NOW,
            limit,  # type: ignore[arg-type]
        )

    session.scalars.assert_not_called()


def test_delete_by_uuid_deletes_only_the_locked_ingestion() -> None:
    session = _session()
    ingestion = _ingestion()
    session.get.return_value = ingestion

    MailIngestionRepository(session).delete_by_uuid(INGESTION_UUID)

    session.get.assert_called_once_with(
        MailIngestion,
        INGESTION_UUID,
        with_for_update=True,
    )
    session.delete.assert_called_once_with(ingestion)
    session.flush.assert_called_once_with()
    session.commit.assert_not_called()


def test_delete_by_uuid_rejects_an_unknown_ingestion() -> None:
    session = _session()
    session.get.return_value = None

    with pytest.raises(LookupError, match="does not exist"):
        MailIngestionRepository(session).delete_by_uuid(INGESTION_UUID)

    session.delete.assert_not_called()
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


def test_next_system_retry_deadline_is_exposed_without_mutation() -> None:
    session = _session()
    expected_deadline = NOW + timedelta(minutes=1)
    session.scalar.return_value = expected_deadline

    deadline = MailIngestionRepository(session).get_next_system_retry_at()

    assert deadline == expected_deadline
    session.scalar.assert_called_once()
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
        audio_extension=AudioExtension.WAV,
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
    assert validated.audio_extension is AudioExtension.WAV
    assert accepted.stage is MailIngestionStage.ACKNOWLEDGEMENT
    assert accepted.disposition is MailIngestionDisposition.ACCEPTED
    assert accepted.job_uuid == job_uuid
    assert completed.status is MailIngestionStatus.COMPLETED
    assert completed.finished_at == NOW + timedelta(seconds=2)
    session.add.assert_not_called()


def test_rejected_lifecycle_accepts_empty_attachment_metadata() -> None:
    session = _session()
    ingestion = _ingestion()
    ingestion.originating_phone_number_ciphertext = b"protected-caller-metadata"
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
    assert rejected.originating_phone_number_ciphertext is None
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


def test_replay_candidates_use_typed_bounded_filters_without_mutation() -> None:
    session = _session()
    failed = _rejected_failed_ingestion()
    session.scalars.return_value.all.return_value = [failed]
    replay_filter = _replay_filter()

    candidates = MailIngestionRepository(session).list_replay_candidates(replay_filter)

    assert len(candidates) == 1
    assert candidates[0].uuid == INGESTION_UUID
    statement = session.scalars.call_args.args[0]
    assert statement._limit_clause.value == replay_filter.limit
    session.flush.assert_not_called()


def test_operator_replay_rearms_rejected_ingestion_and_preserves_history() -> None:
    session = _session()
    failed = _rejected_failed_ingestion()
    original_attempt_count = failed.attempt_count
    original_first_started_at = failed.first_started_at
    session.get.return_value = failed

    replayed = MailIngestionRepository(session).rearm_for_operator_replay(
        INGESTION_UUID,
        expected=_replay_filter(),
        now=NOW,
    )

    assert replayed.status is MailIngestionStatus.PENDING
    assert replayed.stage is MailIngestionStage.DISCOVERY
    assert replayed.disposition is None
    assert replayed.attachment_id is None
    assert replayed.audio_extension is None
    assert replayed.attachment_size_bytes is None
    assert replayed.rejection_category is None
    assert replayed.rejection_code is None
    assert replayed.finished_at is None
    assert replayed.attempt_count == original_attempt_count
    assert replayed.first_started_at == original_first_started_at
    assert session.get.call_args.kwargs["with_for_update"] is True
    session.flush.assert_called_once_with()


def test_operator_replay_revalidates_filter_under_lock() -> None:
    session = _session()
    failed = _rejected_failed_ingestion()
    failed.rejection_code = "another_failure"
    session.get.return_value = failed

    with pytest.raises(
        InvalidMailIngestionTransitionError,
        match="no longer matches",
    ):
        MailIngestionRepository(session).rearm_for_operator_replay(
            INGESTION_UUID,
            expected=_replay_filter(),
            now=NOW,
        )

    session.flush.assert_not_called()


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": MailIngestionStatus.PROCESSING},
        {"disposition": MailIngestionDisposition.ACCEPTED},
        {"job_uuid_is_null": False},
        {"attachment_id_is_not_null": 1},
        {"limit": 0},
        {"limit": True},
        {"limit": 1001},
        {"rejection_category": "unsafe value"},
        {"received_after": NOW.replace(tzinfo=None)},
        {"received_before": NOW - timedelta(days=40)},
    ],
)
def test_replay_filter_rejects_unsafe_or_unbounded_values(
    overrides: dict[str, object],
) -> None:
    values: dict[str, object] = {
        "status": MailIngestionStatus.FAILED,
        "disposition": MailIngestionDisposition.REJECTED,
        "rejection_category": "invalid_attachment",
        "rejection_code": "wav_attachment_missing",
        "received_after": NOW - timedelta(days=30),
        "received_before": NOW + timedelta(seconds=1),
        "limit": 100,
        "job_uuid_is_null": True,
        "attachment_id_is_not_null": True,
    }
    values.update(overrides)

    with pytest.raises((TypeError, ValueError)):
        MailIngestionReplayFilter(**values)  # type: ignore[arg-type]


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
        audio_extension=None,
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


def _rejected_failed_ingestion() -> MailIngestion:
    ingestion = _ingestion(
        status=MailIngestionStatus.FAILED,
        stage=MailIngestionStage.ACKNOWLEDGEMENT,
        disposition=MailIngestionDisposition.REJECTED,
    )
    ingestion.attachment_id = "opaque-attachment-id"
    ingestion.attachment_size_bytes = 2048
    ingestion.rejection_category = "invalid_attachment"
    ingestion.rejection_code = "wav_attachment_missing"
    ingestion.finished_at = NOW - timedelta(seconds=1)
    return ingestion


def _rejected_row(ingestion: MailIngestion) -> SimpleNamespace:
    return SimpleNamespace(
        uuid=ingestion.uuid,
        rejection_category=ingestion.rejection_category,
        rejection_code=ingestion.rejection_code,
        received_at=ingestion.received_at,
        finished_at=ingestion.finished_at,
    )


def _replay_filter() -> MailIngestionReplayFilter:
    return MailIngestionReplayFilter(
        status=MailIngestionStatus.FAILED,
        disposition=MailIngestionDisposition.REJECTED,
        rejection_category="invalid_attachment",
        rejection_code="wav_attachment_missing",
        received_after=NOW - timedelta(days=30),
        received_before=NOW + timedelta(seconds=1),
        limit=100,
        job_uuid_is_null=True,
        attachment_id_is_not_null=True,
    )
