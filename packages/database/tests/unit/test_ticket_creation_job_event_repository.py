from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import create_autospec
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session

from mevo2glpi_database import (
    ErrorScope,
    JobEventActor,
    JobEventCursor,
    JobEventPage,
    JobEventSnapshot,
    JobEventType,
    JobStage,
    JobStatus,
    TicketCreationJobEvent,
    TicketCreationJobEventRepository,
)

NOW = datetime(2026, 8, 28, 12, tzinfo=UTC)


def test_append_adds_only_a_ticket_creation_job_event() -> None:
    session = create_autospec(Session, instance=True)
    job_uuid = uuid4()

    TicketCreationJobEventRepository(session).append(
        job_uuid,
        occurred_at=NOW,
        status=JobStatus.RETRY_SCHEDULED,
        stage=JobStage.TRANSCRIPTION,
        event_type=JobEventType.RETRY_SCHEDULED,
        actor=JobEventActor.WORKER_AUTOMATION,
        error_scope=ErrorScope.SYSTEM,
        error_category="unavailable",
        technical_code="http_503",
    )

    event = session.add.call_args.args[0]
    assert isinstance(event, TicketCreationJobEvent)
    assert event.job_uuid == job_uuid
    assert event.status == JobStatus.RETRY_SCHEDULED.value
    assert event.stage == JobStage.TRANSCRIPTION.value
    assert event.event_type == JobEventType.RETRY_SCHEDULED.value
    assert event.actor == JobEventActor.WORKER_AUTOMATION.value
    assert event.error_scope == ErrorScope.SYSTEM.value
    assert event.error_category == "unavailable"
    assert event.technical_code == "http_503"
    session.flush.assert_called_once_with()


def test_exists_reads_only_the_event_model() -> None:
    session = create_autospec(Session, instance=True)
    session.scalar.return_value = 7

    exists = TicketCreationJobEventRepository(session).exists(
        uuid4(),
        event_type=JobEventType.JOB_CREATED,
        actor=JobEventActor.WEB,
    )

    assert exists is True
    session.scalar.assert_called_once()
    session.add.assert_not_called()
    session.flush.assert_not_called()


def test_delete_by_job_uuid_deletes_only_matching_events() -> None:
    session = create_autospec(Session, instance=True)
    session.execute.return_value.rowcount = 3
    job_uuid = uuid4()

    deleted = TicketCreationJobEventRepository(session).delete_by_job_uuid(job_uuid)

    assert deleted == 3
    session.execute.assert_called_once()
    session.flush.assert_called_once_with()
    session.commit.assert_not_called()


def test_list_page_returns_typed_events_and_a_stable_cursor() -> None:
    session = create_autospec(Session, instance=True)
    job_uuid = uuid4()
    events = (
        _event(job_uuid=job_uuid, event_id=3, event_type=JobEventType.JOB_FAILED),
        _event(job_uuid=job_uuid, event_id=2, event_type=JobEventType.JOB_BLOCKED),
        _event(job_uuid=job_uuid, event_id=1, event_type=JobEventType.JOB_CREATED),
    )
    session.scalars.return_value.all.return_value = events

    page = TicketCreationJobEventRepository(session).list_page(
        job_uuid,
        limit=2,
    )

    assert page == JobEventPage(
        items=(
            _event_snapshot(events[0]),
            _event_snapshot(events[1]),
        ),
        next_cursor=JobEventCursor(
            occurred_at=events[1].occurred_at,
            event_id=2,
        ),
        has_more=True,
    )
    session.scalars.assert_called_once()
    session.flush.assert_not_called()


def test_list_page_rejects_an_invalid_cursor_before_reading() -> None:
    session = create_autospec(Session, instance=True)

    with pytest.raises(ValueError, match="positive"):
        TicketCreationJobEventRepository(session).list_page(
            uuid4(),
            limit=25,
            cursor=JobEventCursor(occurred_at=NOW, event_id=0),
        )

    session.scalars.assert_not_called()


def test_operator_retry_event_is_a_typed_append_contract() -> None:
    session = create_autospec(Session, instance=True)

    TicketCreationJobEventRepository(session).append(
        uuid4(),
        occurred_at=NOW,
        status=JobStatus.PENDING,
        stage=None,
        event_type=JobEventType.OPERATOR_RETRY_REQUESTED,
        actor=JobEventActor.WEB,
    )

    event = session.add.call_args.args[0]
    assert event.event_type == "operator_retry_requested"


def test_publisher_can_append_the_job_created_event_without_creating_a_job() -> None:
    session = create_autospec(Session, instance=True)

    TicketCreationJobEventRepository(session).append(
        uuid4(),
        occurred_at=NOW,
        status=JobStatus.PENDING,
        stage=None,
        event_type=JobEventType.JOB_CREATED,
        actor=JobEventActor.WORKER_PUBLISHER,
    )

    event = session.add.call_args.args[0]
    assert event.actor == "worker_publisher"
    assert event.event_type == "job_created"
    session.commit.assert_not_called()


def test_append_rejects_partial_error_fields_before_mutating_session() -> None:
    session = create_autospec(Session, instance=True)

    with pytest.raises(ValueError, match="provided together"):
        TicketCreationJobEventRepository(session).append(
            uuid4(),
            occurred_at=NOW,
            status=JobStatus.FAILED,
            stage=JobStage.TRANSCRIPTION,
            event_type=JobEventType.JOB_FAILED,
            actor=JobEventActor.WORKER_AUTOMATION,
            error_scope=ErrorScope.JOB,
            error_category="terminal",
        )

    session.add.assert_not_called()
    session.flush.assert_not_called()


def test_event_repository_never_owns_transaction_or_session() -> None:
    session = create_autospec(Session, instance=True)

    TicketCreationJobEventRepository(session).append(
        uuid4(),
        occurred_at=NOW,
        status=JobStatus.PENDING,
        stage=None,
        event_type=JobEventType.JOB_CREATED,
        actor=JobEventActor.WEB,
    )

    session.commit.assert_not_called()
    session.rollback.assert_not_called()
    session.close.assert_not_called()


def _event(
    *,
    job_uuid: UUID,
    event_id: int,
    event_type: JobEventType,
) -> TicketCreationJobEvent:
    return TicketCreationJobEvent(
        event_id=event_id,
        job_uuid=job_uuid,
        occurred_at=NOW,
        status=JobStatus.FAILED.value,
        stage=JobStage.TRANSCRIPTION.value,
        event_type=event_type.value,
        error_scope=ErrorScope.JOB.value,
        error_category="terminal",
        technical_code="remote_failed",
        actor=JobEventActor.WORKER_AUTOMATION.value,
    )


def _event_snapshot(event: TicketCreationJobEvent) -> JobEventSnapshot:
    return JobEventSnapshot(
        event_id=event.event_id,
        job_uuid=event.job_uuid,
        occurred_at=event.occurred_at,
        status=JobStatus(event.status),
        stage=JobStage(event.stage) if event.stage is not None else None,
        event_type=JobEventType(event.event_type),
        error_scope=(
            ErrorScope(event.error_scope) if event.error_scope is not None else None
        ),
        error_category=event.error_category,
        technical_code=event.technical_code,
        actor=JobEventActor(event.actor),
    )
