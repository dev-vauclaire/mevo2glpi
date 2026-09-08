"""HTTP contract tests for dashboard observability."""

from datetime import UTC, datetime
from uuid import UUID

import pytest
from fastapi import FastAPI, HTTPException

from glpi_ticketing_database import WorkerRuntimeState, WorkerServiceName
from glpi_ticketing_web.controllers import ObservabilityController
from glpi_ticketing_web.routes import create_observability_router
from glpi_ticketing_web.services import (
    AlertKind,
    AlertSeverity,
    HeartbeatStatus,
    ObservabilitySnapshot,
    ObservabilityUnavailableError,
    OperationalAlert,
    WorkerObservation,
)

NOW = datetime(2026, 8, 29, 12, tzinfo=UTC)
JOB_UUID = UUID("e53683a9-f884-402c-acf8-e9ed5d9fe07c")
INGESTION_UUID = UUID("04cb377a-6827-4e6d-845d-3ebd48dbd137")


class FakeGetObservability:
    def __init__(self, result: ObservabilitySnapshot | Exception) -> None:
        self._result = result

    def execute(self) -> ObservabilitySnapshot:
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def test_observability_controller_serializes_only_stable_sanitized_contract() -> None:
    snapshot = ObservabilitySnapshot(
        observed_at=NOW,
        workers=(
            WorkerObservation(
                service_name=WorkerServiceName.PUBLISHER,
                heartbeat_status=HeartbeatStatus.FRESH,
                runtime_state=WorkerRuntimeState.RUNNING,
                last_heartbeat_at=NOW,
                current_job_uuid=None,
                current_ingestion_uuid=INGESTION_UUID,
                last_error_category=None,
                last_error_code=None,
            ),
            WorkerObservation(
                service_name=WorkerServiceName.AUTOMATION,
                heartbeat_status=HeartbeatStatus.FRESH,
                runtime_state=WorkerRuntimeState.BLOCKED,
                last_heartbeat_at=NOW,
                current_job_uuid=JOB_UUID,
                current_ingestion_uuid=None,
                last_error_category="dependency_unavailable",
                last_error_code="transcribe_unavailable",
            ),
            WorkerObservation(
                service_name=WorkerServiceName.RETENTION,
                heartbeat_status=HeartbeatStatus.ABSENT,
                runtime_state=None,
                last_heartbeat_at=None,
                current_job_uuid=None,
                current_ingestion_uuid=None,
                last_error_category=None,
                last_error_code=None,
            ),
        ),
        alerts=(
            OperationalAlert(
                key="jobs:blocked",
                kind=AlertKind.JOB_BLOCKED,
                severity=AlertSeverity.CRITICAL,
                count=2,
                condition_since=NOW,
            ),
        ),
    )
    controller = ObservabilityController(
        FakeGetObservability(snapshot)  # type: ignore[arg-type]
    )

    response = controller.get_observability()

    assert response.model_dump(mode="json") == {
        "observed_at": "2026-08-29T12:00:00Z",
        "workers": [
            {
                "service_name": "worker_publisher",
                "heartbeat_status": "fresh",
                "runtime_state": "running",
                "last_heartbeat_at": "2026-08-29T12:00:00Z",
                "current_job_uuid": None,
                "current_ingestion_uuid": str(INGESTION_UUID),
                "last_error_category": None,
                "last_error_code": None,
            },
            {
                "service_name": "worker_automation",
                "heartbeat_status": "fresh",
                "runtime_state": "blocked",
                "last_heartbeat_at": "2026-08-29T12:00:00Z",
                "current_job_uuid": str(JOB_UUID),
                "current_ingestion_uuid": None,
                "last_error_category": "dependency_unavailable",
                "last_error_code": "transcribe_unavailable",
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
        ],
        "alerts": [
            {
                "key": "jobs:blocked",
                "kind": "job_blocked",
                "severity": "critical",
                "count": 2,
                "condition_since": "2026-08-29T12:00:00Z",
            }
        ],
    }
    serialized = response.model_dump_json()
    assert "instance_id" not in serialized
    assert "storage_key" not in serialized
    assert "transcription" not in serialized


def test_observability_controller_returns_sanitized_503() -> None:
    secret = "postgresql://user:secret@database/private"
    controller = ObservabilityController(
        FakeGetObservability(  # type: ignore[arg-type]
            ObservabilityUnavailableError(secret)
        )
    )

    with pytest.raises(HTTPException) as captured_error:
        controller.get_observability()

    assert captured_error.value.status_code == 503
    assert captured_error.value.detail == "Observability is temporarily unavailable"
    assert secret not in str(captured_error.value.detail)


def test_observability_route_is_a_read_only_endpoint() -> None:
    app = FastAPI()
    app.include_router(
        create_observability_router(object())  # type: ignore[arg-type]
    )

    operation = app.openapi()["paths"]["/api/observability"]

    assert set(operation) == {"get"}
    assert operation["get"]["responses"]["200"]["content"]["application/json"][
        "schema"
    ] == {"$ref": "#/components/schemas/ObservabilityResponse"}
