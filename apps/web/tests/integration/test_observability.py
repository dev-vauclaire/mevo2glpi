"""PostgreSQL and HTTP integration for dashboard observability."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from mevo2glpi_database import (
    AudioRetention,
    MailIngestion,
    TicketCreationJob,
    WorkerRuntimeStatus,
    create_session_factory,
)
from mevo2glpi_web.controllers import ObservabilityController
from mevo2glpi_web.routes import create_observability_router
from mevo2glpi_web.services import GetObservability, ObservabilityThresholds

pytestmark = pytest.mark.database

OBSERVED_AT = datetime(2026, 8, 29, 12, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class ObservabilityContext:
    client: TestClient
    session_factory: sessionmaker[Session]


@pytest.fixture
def observability_context(
    migrated_engine: Engine,
) -> Iterator[ObservabilityContext]:
    session_factory = create_session_factory(migrated_engine)
    thresholds = ObservabilityThresholds.from_seconds(
        worker_stale_after=45,
        pending_alert_after=900,
        retry_overdue_grace=60,
        retention_deadline_grace=120,
    )
    service = GetObservability(
        session_factory,
        thresholds=thresholds,
        clock=lambda: OBSERVED_AT,
    )
    app = FastAPI()
    app.include_router(create_observability_router(ObservabilityController(service)))

    with TestClient(app) as client:
        yield ObservabilityContext(
            client=client,
            session_factory=session_factory,
        )


def test_observability_endpoint_combines_workers_jobs_and_retentions(
    observability_context: ObservabilityContext,
) -> None:
    pending_uuid = uuid4()
    rearmed_uuid = uuid4()
    blocked_uuid = uuid4()
    completed_uuid = uuid4()
    ingestion_uuid = uuid4()
    with observability_context.session_factory.begin() as session:
        session.add_all(
            [
                TicketCreationJob(
                    uuid=rearmed_uuid,
                    status="pending",
                    stage=None,
                    storage_key=f"{rearmed_uuid}.wav",
                    audio_extension="wav",
                    audio_size_bytes=10,
                    created_at=OBSERVED_AT - timedelta(days=30),
                    updated_at=OBSERVED_AT - timedelta(minutes=5),
                ),
                TicketCreationJob(
                    uuid=pending_uuid,
                    status="pending",
                    stage=None,
                    storage_key=f"{pending_uuid}.wav",
                    audio_extension="wav",
                    audio_size_bytes=10,
                    created_at=OBSERVED_AT - timedelta(minutes=20),
                    updated_at=OBSERVED_AT - timedelta(minutes=20),
                ),
                TicketCreationJob(
                    uuid=blocked_uuid,
                    status="blocked",
                    stage="transcription",
                    storage_key=f"{blocked_uuid}.wav",
                    audio_extension="wav",
                    audio_size_bytes=10,
                    last_error_scope="system",
                    last_error_category="dependency_unavailable",
                    last_error_code="transcribe_unavailable",
                    created_at=OBSERVED_AT - timedelta(hours=2),
                    updated_at=OBSERVED_AT - timedelta(hours=1),
                    finished_at=OBSERVED_AT - timedelta(hours=1),
                ),
                TicketCreationJob(
                    uuid=completed_uuid,
                    status="completed",
                    stage="finalization",
                    storage_key=f"{completed_uuid}.wav",
                    audio_extension="wav",
                    audio_size_bytes=10,
                    glpi_ticket_id=42,
                    created_at=OBSERVED_AT - timedelta(days=2),
                    updated_at=OBSERVED_AT - timedelta(days=1),
                    finished_at=OBSERVED_AT - timedelta(days=1),
                ),
            ]
        )
        session.flush()
        session.add(
            MailIngestion(
                uuid=ingestion_uuid,
                mailbox_id=uuid4(),
                message_id="synthetic-immutable-message-id",
                status="processing",
                stage="validation",
                attempt_count=1,
                received_at=OBSERVED_AT - timedelta(minutes=2),
                first_started_at=OBSERVED_AT - timedelta(minutes=1),
                updated_at=OBSERVED_AT - timedelta(seconds=5),
            )
        )
        session.flush()
        session.add_all(
            [
                AudioRetention(
                    job_uuid=completed_uuid,
                    policy="deadline_only",
                    status="scheduled",
                    hard_delete_at=OBSERVED_AT - timedelta(minutes=10),
                    created_at=OBSERVED_AT - timedelta(days=1),
                    updated_at=OBSERVED_AT - timedelta(days=1),
                ),
                WorkerRuntimeStatus(
                    service_name="worker_publisher",
                    instance_id=uuid4(),
                    state="running",
                    last_heartbeat_at=OBSERVED_AT - timedelta(seconds=5),
                    current_ingestion_uuid=ingestion_uuid,
                    updated_at=OBSERVED_AT - timedelta(seconds=5),
                ),
                WorkerRuntimeStatus(
                    service_name="worker_automation",
                    instance_id=uuid4(),
                    state="running",
                    last_heartbeat_at=OBSERVED_AT - timedelta(seconds=45),
                    current_job_uuid=blocked_uuid,
                    updated_at=OBSERVED_AT - timedelta(seconds=45),
                ),
            ]
        )

    response = observability_context.client.get("/api/observability")

    assert response.status_code == 200
    body = response.json()
    assert body["observed_at"] == "2026-08-29T12:00:00Z"
    assert body["workers"] == [
        {
            "service_name": "worker_publisher",
            "heartbeat_status": "fresh",
            "runtime_state": "running",
            "last_heartbeat_at": "2026-08-29T11:59:55Z",
            "current_job_uuid": None,
            "current_ingestion_uuid": str(ingestion_uuid),
            "last_error_category": None,
            "last_error_code": None,
        },
        {
            "service_name": "worker_automation",
            "heartbeat_status": "fresh",
            "runtime_state": "running",
            "last_heartbeat_at": "2026-08-29T11:59:15Z",
            "current_job_uuid": str(blocked_uuid),
            "current_ingestion_uuid": None,
            "last_error_category": None,
            "last_error_code": None,
        },
        {
            "service_name": "worker_retention",
            "heartbeat_status": "absent",
            "runtime_state": None,
            "last_heartbeat_at": None,
            "current_job_uuid": None,
            "current_ingestion_uuid": None,
            "last_error_category": None,
            "last_error_code": None,
        },
    ]
    alerts = {alert["key"]: alert for alert in body["alerts"]}
    assert alerts == {
        "workers:worker_retention": {
            "key": "workers:worker_retention",
            "kind": "worker_absent",
            "severity": "critical",
            "count": 1,
            "condition_since": None,
        },
        "jobs:blocked": {
            "key": "jobs:blocked",
            "kind": "job_blocked",
            "severity": "critical",
            "count": 1,
            "condition_since": "2026-08-29T11:00:00Z",
        },
        "retentions:deadline_overdue": {
            "key": "retentions:deadline_overdue",
            "kind": "retention_deadline_overdue",
            "severity": "critical",
            "count": 1,
            "condition_since": "2026-08-29T11:52:00Z",
        },
        "jobs:pending_overdue": {
            "key": "jobs:pending_overdue",
            "kind": "job_pending_overdue",
            "severity": "warning",
            "count": 1,
            "condition_since": "2026-08-29T11:55:00Z",
        },
    }
    assert "instance_id" not in response.text
    assert "storage_key" not in response.text
    assert "transcription" not in response.text

    with observability_context.session_factory.begin() as session:
        automation = session.get(WorkerRuntimeStatus, "worker_automation")
        assert automation is not None
        automation.last_heartbeat_at = OBSERVED_AT - timedelta(
            seconds=45,
            microseconds=1,
        )
        automation.updated_at = automation.last_heartbeat_at

    stale_response = observability_context.client.get("/api/observability")

    assert stale_response.status_code == 200
    stale_body = stale_response.json()
    assert stale_body["workers"][1]["heartbeat_status"] == "stale"
    stale_alerts = {alert["key"]: alert for alert in stale_body["alerts"]}
    assert stale_alerts["workers:worker_automation"] == {
        "key": "workers:worker_automation",
        "kind": "worker_stale",
        "severity": "critical",
        "count": 1,
        "condition_since": "2026-08-29T11:59:59.999999Z",
    }
    assert stale_alerts["jobs:pending_overdue"]["count"] == 1
