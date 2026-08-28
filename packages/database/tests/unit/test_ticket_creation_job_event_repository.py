from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import create_autospec
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from glpi_ticketing_database import (
    ErrorScope,
    JobEventActor,
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
