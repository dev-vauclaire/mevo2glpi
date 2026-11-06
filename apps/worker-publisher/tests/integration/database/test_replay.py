"""PostgreSQL contracts for audited publisher operator replay."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import Engine

from mevo2glpi_database import (
    DiscoveredMail,
    MailIngestionDisposition,
    MailIngestionEventRepository,
    MailIngestionEventType,
    MailIngestionReplayFilter,
    MailIngestionRepository,
    MailIngestionStage,
    MailIngestionStatus,
    create_session_factory,
)
from mevo2glpi_worker_publisher.replay import (
    PublisherReplayService,
    ReplayMode,
    ReplayRequest,
)

pytestmark = pytest.mark.database

NOW = datetime(2026, 10, 26, 12, tzinfo=UTC)
MAILBOX_ID = UUID("94023353-3fb5-45fa-8336-a938e1dc914c")
INGESTION_UUID = UUID("00000000-0000-4000-8000-000000000401")


def test_apply_rearms_once_and_appends_one_audit_event(
    migrated_engine: Engine,
) -> None:
    session_factory = create_session_factory(migrated_engine)
    with session_factory.begin() as session:
        repository = MailIngestionRepository(session)
        repository.add_discovered(
            DiscoveredMail(
                uuid=INGESTION_UUID,
                mailbox_id=MAILBOX_ID,
                message_id="synthetic-immutable-rejected-message",
                received_at=NOW,
            ),
            now=NOW,
        )
        assert repository.resume_or_claim_next(NOW + timedelta(seconds=1)) is not None
        repository.record_rejected(
            INGESTION_UUID,
            rejection_category="invalid_attachment",
            rejection_code="wav_attachment_missing",
            attachment_id="synthetic-ogg-attachment",
            attachment_size_bytes=1_024,
            now=NOW + timedelta(seconds=2),
        )
        repository.record_failed(
            INGESTION_UUID,
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
    request = ReplayRequest(replay_filter=replay_filter, mode=ReplayMode.APPLY)
    service = PublisherReplayService(
        session_factory=session_factory,
        now=lambda: NOW + timedelta(seconds=4),
    )

    first = service.execute(request)
    second = service.execute(request)

    assert first.rearmed_ingestion_uuids == (INGESTION_UUID,)
    assert second.selected_ingestion_uuids == ()
    with session_factory() as session:
        ingestion = MailIngestionRepository(session).get_by_uuid(INGESTION_UUID)
        events = (
            MailIngestionEventRepository(session)
            .list_page(
                INGESTION_UUID,
                limit=10,
            )
            .items
        )

    assert ingestion is not None
    assert ingestion.status is MailIngestionStatus.PENDING
    assert ingestion.stage is MailIngestionStage.DISCOVERY
    assert ingestion.disposition is None
    assert [event.event_type for event in events] == [
        MailIngestionEventType.OPERATOR_REPLAY_REQUESTED
    ]
