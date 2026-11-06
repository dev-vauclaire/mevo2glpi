from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import create_autospec
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session

from mevo2glpi_database.models import (
    MailIngestionDisposition,
    MailIngestionErrorScope,
    MailIngestionEvent,
    MailIngestionEventType,
    MailIngestionStage,
    MailIngestionStatus,
)
from mevo2glpi_database.repositories import (
    MailIngestionEventCursor,
    MailIngestionEventPage,
    MailIngestionEventRepository,
    MailIngestionEventSnapshot,
)

NOW = datetime(2026, 9, 8, 12, tzinfo=UTC)


def test_append_adds_only_a_mail_ingestion_event() -> None:
    session = create_autospec(Session, instance=True)
    ingestion_uuid = uuid4()

    MailIngestionEventRepository(session).append(
        ingestion_uuid,
        occurred_at=NOW,
        status=MailIngestionStatus.RETRY_SCHEDULED,
        stage=MailIngestionStage.ACKNOWLEDGEMENT,
        disposition=MailIngestionDisposition.ACCEPTED,
        event_type=MailIngestionEventType.RETRY_SCHEDULED,
        error_scope=MailIngestionErrorScope.SYSTEM,
        error_category="unavailable",
        technical_code="http_503",
    )

    event = session.add.call_args.args[0]
    assert isinstance(event, MailIngestionEvent)
    assert event.ingestion_uuid == ingestion_uuid
    assert event.status == MailIngestionStatus.RETRY_SCHEDULED.value
    assert event.stage == MailIngestionStage.ACKNOWLEDGEMENT.value
    assert event.disposition == MailIngestionDisposition.ACCEPTED.value
    assert event.event_type == MailIngestionEventType.RETRY_SCHEDULED.value
    assert event.error_scope == MailIngestionErrorScope.SYSTEM.value
    assert event.error_category == "unavailable"
    assert event.technical_code == "http_503"
    session.flush.assert_called_once_with()


def test_list_page_returns_typed_events_and_stable_cursor() -> None:
    session = create_autospec(Session, instance=True)
    ingestion_uuid = uuid4()
    events = (
        _event(ingestion_uuid=ingestion_uuid, event_id=3),
        _event(ingestion_uuid=ingestion_uuid, event_id=2),
        _event(ingestion_uuid=ingestion_uuid, event_id=1),
    )
    session.scalars.return_value.all.return_value = events

    page = MailIngestionEventRepository(session).list_page(
        ingestion_uuid,
        limit=2,
    )

    assert page == MailIngestionEventPage(
        items=(_snapshot(events[0]), _snapshot(events[1])),
        next_cursor=MailIngestionEventCursor(
            occurred_at=events[1].occurred_at,
            event_id=2,
        ),
        has_more=True,
    )
    session.flush.assert_not_called()


def test_append_rejects_partial_or_unsafe_diagnostics_before_mutation() -> None:
    session = create_autospec(Session, instance=True)
    repository = MailIngestionEventRepository(session)

    with pytest.raises(ValueError, match="provided together"):
        repository.append(
            uuid4(),
            occurred_at=NOW,
            status=MailIngestionStatus.BLOCKED,
            stage=MailIngestionStage.VALIDATION,
            disposition=None,
            event_type=MailIngestionEventType.INGESTION_BLOCKED,
            error_scope=MailIngestionErrorScope.SYSTEM,
            error_category="configuration",
        )
    with pytest.raises(ValueError, match="safe identifier"):
        repository.append(
            uuid4(),
            occurred_at=NOW,
            status=MailIngestionStatus.BLOCKED,
            stage=MailIngestionStage.VALIDATION,
            disposition=None,
            event_type=MailIngestionEventType.INGESTION_BLOCKED,
            error_scope=MailIngestionErrorScope.SYSTEM,
            error_category="secret=value",
            technical_code="forbidden",
        )

    session.add.assert_not_called()
    session.flush.assert_not_called()


def test_append_rejects_a_disposition_outside_acknowledgement() -> None:
    session = create_autospec(Session, instance=True)

    with pytest.raises(ValueError, match="exactly at acknowledgement"):
        MailIngestionEventRepository(session).append(
            uuid4(),
            occurred_at=NOW,
            status=MailIngestionStatus.PROCESSING,
            stage=MailIngestionStage.PUBLICATION,
            disposition=MailIngestionDisposition.ACCEPTED,
            event_type=MailIngestionEventType.AUDIO_PUBLISHED,
        )

    session.add.assert_not_called()


def test_event_repository_never_owns_transaction_or_session() -> None:
    session = create_autospec(Session, instance=True)

    MailIngestionEventRepository(session).append(
        uuid4(),
        occurred_at=NOW,
        status=MailIngestionStatus.PENDING,
        stage=MailIngestionStage.DISCOVERY,
        disposition=None,
        event_type=MailIngestionEventType.MESSAGE_DISCOVERED,
    )

    session.commit.assert_not_called()
    session.rollback.assert_not_called()
    session.close.assert_not_called()


def _event(*, ingestion_uuid: UUID, event_id: int) -> MailIngestionEvent:
    return MailIngestionEvent(
        event_id=event_id,
        ingestion_uuid=ingestion_uuid,
        occurred_at=NOW,
        status=MailIngestionStatus.FAILED.value,
        stage=MailIngestionStage.ACKNOWLEDGEMENT.value,
        disposition=MailIngestionDisposition.REJECTED.value,
        event_type=MailIngestionEventType.INGESTION_FAILED.value,
        error_scope=MailIngestionErrorScope.MESSAGE.value,
        error_category="invalid_audio",
        technical_code="empty_file",
    )


def _snapshot(event: MailIngestionEvent) -> MailIngestionEventSnapshot:
    return MailIngestionEventSnapshot(
        event_id=event.event_id,
        ingestion_uuid=event.ingestion_uuid,
        occurred_at=event.occurred_at,
        status=MailIngestionStatus(event.status),
        stage=MailIngestionStage(event.stage),
        disposition=MailIngestionDisposition(event.disposition),
        event_type=MailIngestionEventType(event.event_type),
        error_scope=MailIngestionErrorScope(event.error_scope),
        error_category=event.error_category,
        technical_code=event.technical_code,
    )
