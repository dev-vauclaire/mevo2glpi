"""Publisher heartbeat integration with the shared PostgreSQL runtime table."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import Engine

from glpi_ticketing_database import (
    DiscoveredMail,
    MailIngestionErrorScope,
    MailIngestionRepository,
    MailIngestionStage,
    WorkerRuntimeState,
    WorkerRuntimeStatusRepository,
    WorkerServiceName,
    create_session_factory,
    try_acquire_advisory_lock,
)
from glpi_ticketing_worker_publisher.publisher import (
    CompletedOutcome,
    PublisherFailure,
    RetryScheduledOutcome,
)
from glpi_ticketing_worker_publisher.runtime.heartbeat import (
    PublisherRuntimeHeartbeatReporter,
)

pytestmark = pytest.mark.database

NOW = datetime(2026, 9, 8, 12, tzinfo=UTC)


def test_publisher_heartbeat_tracks_ingestion_and_sticky_dependency_state(
    migrated_engine: Engine,
) -> None:
    lock = try_acquire_advisory_lock(
        migrated_engine,
        WorkerServiceName.PUBLISHER,
    )
    assert lock is not None
    ingestion_uuid = UUID("00000000-0000-4000-8000-000000000351")
    with create_session_factory(migrated_engine).begin() as session:
        MailIngestionRepository(session).add_discovered(
            DiscoveredMail(
                uuid=ingestion_uuid,
                mailbox_id=UUID("10000000-0000-4000-8000-000000000001"),
                message_id="immutable-heartbeat-message",
                received_at=NOW,
            ),
            now=NOW,
        )
    reporter = PublisherRuntimeHeartbeatReporter(
        lock,
        now=lambda: NOW,
    )
    try:
        reporter.record_state(WorkerRuntimeState.STARTING)
        reporter.record_state(WorkerRuntimeState.RUNNING)
        reporter.start_ingestion(ingestion_uuid)
        reporter.report_outcome(
            RetryScheduledOutcome(
                ingestion_uuid=ingestion_uuid,
                stage=MailIngestionStage.VALIDATION,
                failure=PublisherFailure(
                    scope=MailIngestionErrorScope.SYSTEM,
                    category="graph_unavailable",
                    technical_code="transport_error",
                ),
                next_attempt_at=NOW + timedelta(seconds=5),
            )
        )

        snapshot = _publisher_snapshot(migrated_engine)
        assert snapshot.state is WorkerRuntimeState.DEGRADED
        assert snapshot.current_ingestion_uuid is None
        assert snapshot.last_error_code == "transport_error"

        reporter.report_outcome(
            CompletedOutcome(
                ingestion_uuid=ingestion_uuid,
                job_uuid=ingestion_uuid,
            )
        )
        recovered = _publisher_snapshot(migrated_engine)
        assert recovered.state is WorkerRuntimeState.RUNNING
        assert recovered.current_ingestion_uuid is None
        assert recovered.last_error_code is None
    finally:
        lock.release()


def _publisher_snapshot(engine: Engine):
    with create_session_factory(engine)() as session:
        return next(
            item
            for item in WorkerRuntimeStatusRepository(session).list_all()
            if item.service_name is WorkerServiceName.PUBLISHER
        )
