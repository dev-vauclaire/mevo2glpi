"""HTTP translation for the dashboard observability use case."""

from fastapi import HTTPException, status

from glpi_ticketing_web.schemas import (
    ObservabilityResponse,
    OperationalAlertKind,
    OperationalAlertResponse,
    OperationalAlertSeverity,
    WorkerHeartbeatStatus,
    WorkerObservationResponse,
    WorkerRuntimeState,
    WorkerService,
)
from glpi_ticketing_web.services import (
    GetObservability,
    ObservabilityUnavailableError,
)


class ObservabilityController:
    """Translate the bounded operational snapshot to its HTTP contract."""

    def __init__(self, get_observability: GetObservability) -> None:
        self._get_observability = get_observability

    def get_observability(self) -> ObservabilityResponse:
        """Return active worker and lifecycle conditions for the dashboard."""
        try:
            snapshot = self._get_observability.execute()
        except ObservabilityUnavailableError as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Observability is temporarily unavailable",
            ) from error
        return ObservabilityResponse(
            observed_at=snapshot.observed_at,
            workers=tuple(
                WorkerObservationResponse(
                    service_name=WorkerService(worker.service_name.value),
                    heartbeat_status=WorkerHeartbeatStatus(
                        worker.heartbeat_status.value
                    ),
                    runtime_state=(
                        WorkerRuntimeState(worker.runtime_state.value)
                        if worker.runtime_state is not None
                        else None
                    ),
                    last_heartbeat_at=worker.last_heartbeat_at,
                    current_job_uuid=worker.current_job_uuid,
                    current_ingestion_uuid=worker.current_ingestion_uuid,
                    last_error_category=worker.last_error_category,
                    last_error_code=worker.last_error_code,
                )
                for worker in snapshot.workers
            ),
            alerts=tuple(
                OperationalAlertResponse(
                    key=alert.key,
                    kind=OperationalAlertKind(alert.kind.value),
                    severity=OperationalAlertSeverity(alert.severity.value),
                    count=alert.count,
                    condition_since=alert.condition_since,
                )
                for alert in snapshot.alerts
            ),
        )


__all__ = ["ObservabilityController"]
