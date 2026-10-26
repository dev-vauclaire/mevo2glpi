"""PostgreSQL invariants for the publisher ingestion lifecycle."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, Table, inspect, select
from sqlalchemy.exc import IntegrityError

from glpi_ticketing_database.models import (
    MailIngestion,
    MailIngestionEvent,
    TicketCreationJob,
    WorkerRuntimeStatus,
)

pytestmark = pytest.mark.database

NOW = datetime(2026, 9, 8, 10, tzinfo=UTC)
LATER = NOW + timedelta(minutes=5)
INGESTIONS = MailIngestion.__table__
INGESTION_EVENTS = MailIngestionEvent.__table__
JOBS = TicketCreationJob.__table__
RUNTIMES = WorkerRuntimeStatus.__table__


def _pending_ingestion(
    *,
    ingestion_uuid: UUID | None = None,
    mailbox_id: UUID | None = None,
    message_id: str = "AAMkSyntheticImmutableId",
    **overrides: object,
) -> dict[str, object]:
    values: dict[str, object] = {
        "uuid": ingestion_uuid or uuid4(),
        "mailbox_id": mailbox_id or uuid4(),
        "message_id": message_id,
        "received_at": NOW,
    }
    values.update(overrides)
    return values


def _processing_ingestion(**overrides: object) -> dict[str, object]:
    values = _pending_ingestion(
        status="processing",
        stage="validation",
        attempt_count=1,
        first_started_at=NOW,
    )
    values.update(overrides)
    return values


def _retry_ingestion(**overrides: object) -> dict[str, object]:
    values = _processing_ingestion(
        status="retry_scheduled",
        consecutive_retry_count=1,
        next_attempt_at=LATER,
        last_error_scope="system",
        last_error_category="graph",
        last_error_code="service_unavailable",
    )
    values.update(overrides)
    return values


def _job_values(job_uuid: UUID) -> dict[str, object]:
    return {
        "uuid": job_uuid,
        "storage_key": f"{job_uuid}.wav",
        "audio_extension": "wav",
        "audio_size_bytes": 128,
    }


def _accepted_ingestion(job_uuid: UUID, **overrides: object) -> dict[str, object]:
    values = _pending_ingestion(
        status="completed",
        stage="acknowledgement",
        disposition="accepted",
        attachment_id="AAMkSyntheticAttachmentId",
        audio_extension="wav",
        attachment_size_bytes=128,
        job_uuid=job_uuid,
        attempt_count=1,
        first_started_at=NOW,
        finished_at=NOW,
    )
    values.update(overrides)
    return values


def _rejected_ingestion(**overrides: object) -> dict[str, object]:
    values = _pending_ingestion(
        status="failed",
        stage="acknowledgement",
        disposition="rejected",
        rejection_category="invalid_audio",
        rejection_code="wav_empty",
        attempt_count=1,
        first_started_at=NOW,
        finished_at=NOW,
    )
    values.update(overrides)
    return values


def _event_values(ingestion_uuid: UUID, **overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "ingestion_uuid": ingestion_uuid,
        "status": "pending",
        "stage": "discovery",
        "disposition": None,
        "event_type": "message_discovered",
        "error_scope": None,
        "error_category": None,
        "technical_code": None,
    }
    values.update(overrides)
    return values


def _insert(engine: Engine, table: Table, *rows: Mapping[str, object]) -> None:
    with engine.begin() as connection:
        for row in rows:
            connection.execute(table.insert(), row)


def _assert_rejected(
    engine: Engine,
    table: Table,
    values: Mapping[str, object],
    expected_constraints: str | set[str],
) -> None:
    expected_names = (
        {expected_constraints}
        if isinstance(expected_constraints, str)
        else expected_constraints
    )
    with pytest.raises(IntegrityError) as captured_error:
        with engine.begin() as connection:
            connection.execute(table.insert(), values)

    diagnostic = getattr(captured_error.value.orig, "diag", None)
    assert getattr(diagnostic, "constraint_name", None) in expected_names


def test_immutable_message_identity_is_unique_and_case_sensitive(
    migrated_engine: Engine,
) -> None:
    mailbox_id = uuid4()
    exact_id = "AAMkSyntheticImmutableId"
    case_variant = "aAMkSyntheticImmutableId"
    first = _pending_ingestion(mailbox_id=mailbox_id, message_id=exact_id)
    _insert(migrated_engine, INGESTIONS, first)

    _assert_rejected(
        migrated_engine,
        INGESTIONS,
        _pending_ingestion(mailbox_id=mailbox_id, message_id=exact_id),
        "uq_mail_ingestions_source",
    )
    _insert(
        migrated_engine,
        INGESTIONS,
        _pending_ingestion(mailbox_id=mailbox_id, message_id=case_variant),
        _pending_ingestion(mailbox_id=uuid4(), message_id=exact_id),
    )

    with migrated_engine.connect() as connection:
        persisted_ids = connection.scalars(
            select(INGESTIONS.c.message_id).where(
                INGESTIONS.c.message_id.in_((exact_id, case_variant))
            )
        ).all()

    assert sorted(persisted_ids) == [exact_id, exact_id, case_variant]
    assert INGESTIONS.c.message_id.type.collation == "C"
    assert INGESTIONS.c.attachment_id.type.collation == "C"


def test_mail_source_identifiers_and_counters_are_database_enforced(
    migrated_engine: Engine,
) -> None:
    invalid_rows = (
        (
            _pending_ingestion(message_id="   "),
            "ck_mail_ingestions_message_id_not_empty",
        ),
        (
            _pending_ingestion(attachment_id=" "),
            {
                "ck_mail_ingestions_attachment_id_not_empty",
                "ck_mail_ingestions_pending_state",
            },
        ),
        (
            _pending_ingestion(attachment_size_bytes=-1),
            {
                "ck_mail_ingestions_attachment_size_bytes_non_negative",
                "ck_mail_ingestions_pending_state",
            },
        ),
        (
            _pending_ingestion(attempt_count=-1),
            {
                "ck_mail_ingestions_attempt_count_non_negative",
                "ck_mail_ingestions_attempt_state",
            },
        ),
        (
            _pending_ingestion(consecutive_retry_count=-1),
            {
                "ck_mail_ingestions_consecutive_retry_count_non_negative",
                "ck_mail_ingestions_attempt_state",
            },
        ),
    )

    for values, constraint_names in invalid_rows:
        _assert_rejected(
            migrated_engine,
            INGESTIONS,
            values,
            constraint_names,
        )


def test_mail_retry_requires_schedule_attempt_and_sanitized_error(
    migrated_engine: Engine,
) -> None:
    valid_retry = _retry_ingestion()
    _insert(migrated_engine, INGESTIONS, valid_retry)

    invalid_rows = (
        (
            _retry_ingestion(next_attempt_at=None),
            "ck_mail_ingestions_retry_schedule",
        ),
        (
            _retry_ingestion(consecutive_retry_count=0),
            "ck_mail_ingestions_retry_has_attempt",
        ),
        (
            _retry_ingestion(last_error_code=None),
            "ck_mail_ingestions_error_fields",
        ),
        (
            _retry_ingestion(
                last_error_scope=None,
                last_error_category=None,
                last_error_code=None,
            ),
            "ck_mail_ingestions_error_for_status",
        ),
    )
    for values, constraint_name in invalid_rows:
        _assert_rejected(
            migrated_engine,
            INGESTIONS,
            values,
            constraint_name,
        )


def test_accepted_and_rejected_dispositions_have_distinct_invariants(
    migrated_engine: Engine,
) -> None:
    accepted_job_uuid = uuid4()
    invalid_accepted_job_uuid = uuid4()
    _insert(
        migrated_engine,
        JOBS,
        _job_values(accepted_job_uuid),
        _job_values(invalid_accepted_job_uuid),
    )
    accepted = _accepted_ingestion(accepted_job_uuid)
    rejected = _rejected_ingestion(message_id="AAMkRejectedMessage")
    _insert(migrated_engine, INGESTIONS, accepted, rejected)

    _assert_rejected(
        migrated_engine,
        INGESTIONS,
        {
            **_accepted_ingestion(invalid_accepted_job_uuid),
            "message_id": "AAMkAcceptedWithoutAttachment",
            "attachment_id": None,
        },
        "ck_mail_ingestions_accepted_disposition",
    )
    _assert_rejected(
        migrated_engine,
        INGESTIONS,
        {
            **_rejected_ingestion(message_id="AAMkRejectedWithJob"),
            "job_uuid": accepted_job_uuid,
        },
        {
            "ck_mail_ingestions_rejected_disposition",
            "ck_mail_ingestions_job_for_accepted_disposition",
        },
    )
    _assert_rejected(
        migrated_engine,
        INGESTIONS,
        _processing_ingestion(
            status="failed",
            stage="acknowledgement",
            finished_at=NOW,
        ),
        {
            "ck_mail_ingestions_disposition_at_acknowledgement",
            "ck_mail_ingestions_failed_disposition",
        },
    )


def test_audio_extension_and_operator_replay_state_are_database_enforced(
    migrated_engine: Engine,
) -> None:
    replayed = _pending_ingestion(
        message_id="AAMkReplayedMessage",
        attempt_count=2,
        first_started_at=NOW,
    )
    _insert(migrated_engine, INGESTIONS, replayed)
    _insert(
        migrated_engine,
        INGESTIONS,
        _rejected_ingestion(
            message_id="AAMkEmptyOggMessage",
            attachment_id="AAMkEmptyOggAttachment",
            audio_extension="ogg",
            attachment_size_bytes=0,
            rejection_code="audio_empty",
        ),
    )

    invalid_rows = (
        (
            _processing_ingestion(
                stage="publication",
                attachment_id="AAMkOggAttachment",
                attachment_size_bytes=128,
                audio_extension=None,
            ),
            "ck_mail_ingestions_publication_has_audio_extension",
        ),
        (
            _processing_ingestion(
                attachment_id="AAMkUnsupportedAttachment",
                attachment_size_bytes=128,
                audio_extension="flac",
            ),
            "ck_mail_ingestions_audio_extension",
        ),
        (
            _processing_ingestion(audio_extension="ogg"),
            "ck_mail_ingestions_audio_extension_has_attachment",
        ),
        (
            _pending_ingestion(
                attempt_count=1,
                first_started_at=None,
            ),
            "ck_mail_ingestions_attempt_state",
        ),
        (
            _pending_ingestion(
                attempt_count=1,
                first_started_at=NOW,
                attachment_id="stale-attachment",
            ),
            "ck_mail_ingestions_pending_state",
        ),
    )
    for values, constraint_name in invalid_rows:
        _assert_rejected(
            migrated_engine,
            INGESTIONS,
            values,
            constraint_name,
        )

    ingestion_uuid = replayed["uuid"]
    _insert(
        migrated_engine,
        INGESTION_EVENTS,
        _event_values(
            ingestion_uuid,
            event_type="operator_replay_requested",
        ),
    )


def test_single_processing_ingestion_and_job_link_are_unique(
    migrated_engine: Engine,
) -> None:
    _insert(migrated_engine, INGESTIONS, _processing_ingestion())
    _assert_rejected(
        migrated_engine,
        INGESTIONS,
        _processing_ingestion(),
        "uq_mail_ingestions_single_processing",
    )

    job_uuid = uuid4()
    _insert(migrated_engine, JOBS, _job_values(job_uuid))
    _insert(migrated_engine, INGESTIONS, _accepted_ingestion(job_uuid))
    _assert_rejected(
        migrated_engine,
        INGESTIONS,
        _accepted_ingestion(job_uuid, message_id="AAMkSecondMessage"),
        "uq_mail_ingestions_job_uuid",
    )


def test_ingestion_events_validate_enums_and_atomic_error_fields(
    migrated_engine: Engine,
) -> None:
    ingestion_uuid = uuid4()
    _insert(
        migrated_engine,
        INGESTIONS,
        _pending_ingestion(ingestion_uuid=ingestion_uuid),
    )
    _insert(
        migrated_engine,
        INGESTION_EVENTS,
        _event_values(ingestion_uuid),
    )

    invalid_events = (
        (
            _event_values(ingestion_uuid, event_type="unknown"),
            "ck_mail_ingestion_events_event_type",
        ),
        (
            _event_values(ingestion_uuid, status="unknown"),
            "ck_mail_ingestion_events_status",
        ),
        (
            _event_values(ingestion_uuid, stage="unknown"),
            "ck_mail_ingestion_events_stage",
        ),
        (
            _event_values(
                ingestion_uuid,
                error_scope="message",
                error_category="audio",
                technical_code=None,
            ),
            "ck_mail_ingestion_events_error_fields",
        ),
        (
            _event_values(
                ingestion_uuid,
                stage="acknowledgement",
                disposition=None,
            ),
            "ck_mail_ingestion_events_disposition_at_acknowledgement",
        ),
        (
            _event_values(
                ingestion_uuid,
                stage="discovery",
                disposition="accepted",
            ),
            "ck_mail_ingestion_events_disposition_at_acknowledgement",
        ),
    )
    for values, constraint_name in invalid_events:
        _assert_rejected(
            migrated_engine,
            INGESTION_EVENTS,
            values,
            constraint_name,
        )


def test_worker_runtime_tracks_only_the_publishers_current_ingestion(
    migrated_engine: Engine,
) -> None:
    ingestion_uuid = uuid4()
    job_uuid = uuid4()
    _insert(
        migrated_engine, INGESTIONS, _pending_ingestion(ingestion_uuid=ingestion_uuid)
    )
    _insert(migrated_engine, JOBS, _job_values(job_uuid))

    _assert_rejected(
        migrated_engine,
        RUNTIMES,
        {
            "service_name": "worker_automation",
            "instance_id": uuid4(),
            "current_ingestion_uuid": ingestion_uuid,
        },
        "ck_worker_runtime_status_current_work_for_service",
    )
    _assert_rejected(
        migrated_engine,
        RUNTIMES,
        {
            "service_name": "worker_publisher",
            "instance_id": uuid4(),
            "current_job_uuid": job_uuid,
        },
        "ck_worker_runtime_status_current_work_for_service",
    )

    _insert(
        migrated_engine,
        RUNTIMES,
        {
            "service_name": "worker_publisher",
            "instance_id": uuid4(),
            "current_ingestion_uuid": ingestion_uuid,
        },
    )
    foreign_keys = {
        foreign_key["name"]: foreign_key["options"].get("ondelete")
        for foreign_key in inspect(migrated_engine).get_foreign_keys(RUNTIMES.name)
    }
    assert (
        foreign_keys["fk_worker_runtime_status_current_ingestion_uuid_mail_ingestions"]
        == "SET NULL"
    )

    with migrated_engine.begin() as connection:
        connection.execute(
            INGESTIONS.delete().where(INGESTIONS.c.uuid == ingestion_uuid)
        )
    with migrated_engine.connect() as connection:
        assert (
            connection.scalar(
                select(RUNTIMES.c.current_ingestion_uuid).where(
                    RUNTIMES.c.service_name == "worker_publisher"
                )
            )
            is None
        )


def test_ingestion_audit_foreign_keys_restrict_parent_deletion(
    migrated_engine: Engine,
) -> None:
    job_uuid = uuid4()
    ingestion_uuid = uuid4()
    _insert(migrated_engine, JOBS, _job_values(job_uuid))
    _insert(
        migrated_engine,
        INGESTIONS,
        _accepted_ingestion(job_uuid, uuid=ingestion_uuid),
    )
    _insert(
        migrated_engine,
        INGESTION_EVENTS,
        _event_values(
            ingestion_uuid,
            status="completed",
            stage="acknowledgement",
            disposition="accepted",
            event_type="ingestion_completed",
        ),
    )

    foreign_keys = {
        foreign_key["name"]: foreign_key["options"].get("ondelete")
        for table_name in (INGESTIONS.name, INGESTION_EVENTS.name)
        for foreign_key in inspect(migrated_engine).get_foreign_keys(table_name)
    }
    assert {
        "fk_mail_ingestions_job_uuid_ticket_creation_jobs": "RESTRICT",
        "fk_mail_ingestion_events_ingestion_uuid_mail_ingestions": "RESTRICT",
    }.items() <= foreign_keys.items()

    with pytest.raises(IntegrityError):
        with migrated_engine.begin() as connection:
            connection.execute(JOBS.delete().where(JOBS.c.uuid == job_uuid))
    with pytest.raises(IntegrityError):
        with migrated_engine.begin() as connection:
            connection.execute(
                INGESTIONS.delete().where(INGESTIONS.c.uuid == ingestion_uuid)
            )
