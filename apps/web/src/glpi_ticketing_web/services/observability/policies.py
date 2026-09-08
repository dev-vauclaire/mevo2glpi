"""Pure alert policy for worker, job and retention observations."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from glpi_ticketing_database import (
    AlertAggregate,
    JobAlertSummary,
    RetentionAlertSummary,
    WorkerRuntimeSnapshot,
    WorkerRuntimeState,
    WorkerServiceName,
)

from .models import (
    AlertKind,
    AlertSeverity,
    HeartbeatStatus,
    ObservabilitySnapshot,
    ObservabilityThresholds,
    OperationalAlert,
    WorkerObservation,
)

_EXPECTED_SERVICES = (
    WorkerServiceName.PUBLISHER,
    WorkerServiceName.AUTOMATION,
    WorkerServiceName.RETENTION,
)
_SAFE_IDENTIFIER = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_CRITICAL_KINDS = {
    AlertKind.WORKER_ABSENT,
    AlertKind.WORKER_STOPPED,
    AlertKind.WORKER_STALE,
    AlertKind.WORKER_BLOCKED,
    AlertKind.JOB_BLOCKED,
    AlertKind.JOB_FAILED,
    AlertKind.RETENTION_DEADLINE_OVERDUE,
    AlertKind.RETENTION_GLPI_CHECK_BLOCKED,
    AlertKind.RETENTION_AUDIO_DELETION_BLOCKED,
}


class ObservabilityPolicy:
    """Project persistence summaries into bounded, deduplicated alerts."""

    def __init__(self, thresholds: ObservabilityThresholds) -> None:
        if not isinstance(thresholds, ObservabilityThresholds):
            raise TypeError("thresholds must be ObservabilityThresholds")
        self._thresholds = thresholds

    def evaluate(
        self,
        *,
        observed_at: datetime,
        runtimes: tuple[WorkerRuntimeSnapshot, ...],
        jobs: JobAlertSummary,
        retentions: RetentionAlertSummary,
    ) -> ObservabilitySnapshot:
        """Return one deterministic observation without persisting alert state."""
        observed_at = _utc_datetime(observed_at, "observed_at")
        runtime_by_service = self._index_runtimes(runtimes)
        workers: list[WorkerObservation] = []
        alerts: dict[str, OperationalAlert] = {}

        for service_name in _EXPECTED_SERVICES:
            worker, alert = self._observe_worker(
                service_name,
                runtime_by_service.get(service_name),
                observed_at=observed_at,
            )
            workers.append(worker)
            if alert is not None:
                _add_unique(alerts, alert)

        for alert in self._job_alerts(jobs):
            _add_unique(alerts, alert)
        for alert in self._retention_alerts(retentions):
            _add_unique(alerts, alert)

        return ObservabilitySnapshot(
            observed_at=observed_at,
            workers=tuple(workers),
            alerts=tuple(sorted(alerts.values(), key=_alert_order)),
        )

    @staticmethod
    def _index_runtimes(
        runtimes: tuple[WorkerRuntimeSnapshot, ...],
    ) -> dict[WorkerServiceName, WorkerRuntimeSnapshot]:
        if not isinstance(runtimes, tuple):
            raise TypeError("runtimes must be a tuple")
        indexed: dict[WorkerServiceName, WorkerRuntimeSnapshot] = {}
        for runtime in runtimes:
            if not isinstance(runtime, WorkerRuntimeSnapshot):
                raise TypeError("runtime must be a WorkerRuntimeSnapshot")
            if runtime.service_name not in _EXPECTED_SERVICES:
                # A migration may introduce a worker heartbeat before its
                # dashboard projection is deployed. Ignore that known future
                # service instead of making observability unavailable.
                continue
            if runtime.service_name in indexed:
                raise ValueError("duplicate worker runtime")
            indexed[runtime.service_name] = runtime
        return indexed

    def _observe_worker(
        self,
        service_name: WorkerServiceName,
        runtime: WorkerRuntimeSnapshot | None,
        *,
        observed_at: datetime,
    ) -> tuple[WorkerObservation, OperationalAlert | None]:
        if runtime is None:
            worker = WorkerObservation(
                service_name=service_name,
                heartbeat_status=HeartbeatStatus.ABSENT,
                runtime_state=None,
                last_heartbeat_at=None,
                current_job_uuid=None,
                current_ingestion_uuid=None,
                last_error_category=None,
                last_error_code=None,
            )
            return worker, OperationalAlert(
                key=f"workers:{service_name.value}",
                kind=AlertKind.WORKER_ABSENT,
                severity=AlertSeverity.CRITICAL,
                count=1,
                condition_since=None,
            )

        heartbeat_at = _utc_datetime(
            runtime.last_heartbeat_at,
            "runtime.last_heartbeat_at",
        )
        stale_since = heartbeat_at + self._thresholds.worker_stale_after
        heartbeat_status = (
            HeartbeatStatus.STALE
            if stale_since < observed_at
            else HeartbeatStatus.FRESH
        )
        error_category, error_code = _sanitized_error(runtime)
        worker = WorkerObservation(
            service_name=service_name,
            heartbeat_status=heartbeat_status,
            runtime_state=runtime.state,
            last_heartbeat_at=heartbeat_at,
            current_job_uuid=runtime.current_job_uuid,
            current_ingestion_uuid=runtime.current_ingestion_uuid,
            last_error_category=error_category,
            last_error_code=error_code,
        )

        kind: AlertKind | None = None
        condition_since: datetime | None = None
        if runtime.state is WorkerRuntimeState.STOPPED:
            kind = AlertKind.WORKER_STOPPED
            condition_since = heartbeat_at
        elif heartbeat_status is HeartbeatStatus.STALE:
            kind = AlertKind.WORKER_STALE
            condition_since = stale_since
        elif runtime.state is WorkerRuntimeState.BLOCKED:
            # A checkpoint refreshes last_heartbeat_at without recording when
            # this runtime state began, so no truthful duration is available.
            kind = AlertKind.WORKER_BLOCKED
        elif runtime.state is WorkerRuntimeState.DEGRADED:
            kind = AlertKind.WORKER_DEGRADED

        if kind is None:
            return worker, None
        return worker, OperationalAlert(
            key=f"workers:{service_name.value}",
            kind=kind,
            severity=_severity(kind),
            count=1,
            condition_since=condition_since,
        )

    def _job_alerts(self, summary: JobAlertSummary) -> tuple[OperationalAlert, ...]:
        if not isinstance(summary, JobAlertSummary):
            raise TypeError("jobs must be a JobAlertSummary")
        candidates = (
            _aggregate_alert(
                key="jobs:blocked",
                kind=AlertKind.JOB_BLOCKED,
                aggregate=summary.blocked,
            ),
            _aggregate_alert(
                key="jobs:failed",
                kind=AlertKind.JOB_FAILED,
                aggregate=summary.failed,
            ),
            _aggregate_alert(
                key="jobs:pending_overdue",
                kind=AlertKind.JOB_PENDING_OVERDUE,
                aggregate=summary.pending_overdue,
                activation_delay=self._thresholds.pending_alert_after,
            ),
            _aggregate_alert(
                key="jobs:retry_overdue",
                kind=AlertKind.JOB_RETRY_OVERDUE,
                aggregate=summary.retry_overdue,
                activation_delay=self._thresholds.retry_overdue_grace,
            ),
        )
        return tuple(alert for alert in candidates if alert is not None)

    def _retention_alerts(
        self,
        summary: RetentionAlertSummary,
    ) -> tuple[OperationalAlert, ...]:
        if not isinstance(summary, RetentionAlertSummary):
            raise TypeError("retentions must be a RetentionAlertSummary")
        candidates = (
            _aggregate_alert(
                key="retentions:deadline_overdue",
                kind=AlertKind.RETENTION_DEADLINE_OVERDUE,
                aggregate=summary.deadline_overdue,
                activation_delay=self._thresholds.retention_deadline_grace,
            ),
            _aggregate_alert(
                key="retentions:glpi_check_blocked",
                kind=AlertKind.RETENTION_GLPI_CHECK_BLOCKED,
                aggregate=summary.glpi_check_blocked,
            ),
            _aggregate_alert(
                key="retentions:audio_deletion_blocked",
                kind=AlertKind.RETENTION_AUDIO_DELETION_BLOCKED,
                aggregate=summary.deletion_blocked,
            ),
            _aggregate_alert(
                key="retentions:glpi_retry_overdue",
                kind=AlertKind.RETENTION_GLPI_RETRY_OVERDUE,
                aggregate=summary.glpi_retry_overdue,
                activation_delay=self._thresholds.retry_overdue_grace,
            ),
            _aggregate_alert(
                key="retentions:audio_deletion_retry_overdue",
                kind=AlertKind.RETENTION_AUDIO_DELETION_RETRY_OVERDUE,
                aggregate=summary.deletion_retry_overdue,
                activation_delay=self._thresholds.retry_overdue_grace,
            ),
        )
        return tuple(alert for alert in candidates if alert is not None)


def _aggregate_alert(
    *,
    key: str,
    kind: AlertKind,
    aggregate: AlertAggregate,
    activation_delay: timedelta = timedelta(),
) -> OperationalAlert | None:
    if not isinstance(aggregate, AlertAggregate):
        raise TypeError("aggregate must be an AlertAggregate")
    if (
        isinstance(aggregate.count, bool)
        or not isinstance(aggregate.count, int)
        or aggregate.count < 0
    ):
        raise ValueError("aggregate count must be a non-negative integer")
    if aggregate.count == 0:
        if aggregate.condition_since is not None:
            raise ValueError("empty aggregate must not carry a timestamp")
        return None
    if aggregate.condition_since is None:
        raise ValueError("non-empty aggregate requires a timestamp")
    source_time = _utc_datetime(
        aggregate.condition_since,
        "aggregate.condition_since",
    )
    return OperationalAlert(
        key=key,
        kind=kind,
        severity=_severity(kind),
        count=aggregate.count,
        condition_since=source_time + activation_delay,
    )


def _sanitized_error(
    runtime: WorkerRuntimeSnapshot,
) -> tuple[str | None, str | None]:
    if runtime.state not in {
        WorkerRuntimeState.DEGRADED,
        WorkerRuntimeState.BLOCKED,
    }:
        return None, None
    category = runtime.last_error_category
    code = runtime.last_error_code
    if category is None or code is None:
        return None, None
    if (
        _SAFE_IDENTIFIER.fullmatch(category) is None
        or _SAFE_IDENTIFIER.fullmatch(code) is None
    ):
        return None, None
    return category, code


def _severity(kind: AlertKind) -> AlertSeverity:
    return AlertSeverity.CRITICAL if kind in _CRITICAL_KINDS else AlertSeverity.WARNING


def _add_unique(
    alerts: dict[str, OperationalAlert],
    alert: OperationalAlert,
) -> None:
    if alert.key in alerts:
        raise ValueError("duplicate alert key")
    alerts[alert.key] = alert


def _alert_order(alert: OperationalAlert) -> tuple[int, int, str]:
    severity_order = 0 if alert.severity is AlertSeverity.CRITICAL else 1
    scope_order = 0 if alert.kind.value.startswith("worker_") else 1
    return severity_order, scope_order, alert.key


def _utc_datetime(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    if value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


__all__ = ["ObservabilityPolicy"]
