"""Pure observability policy boundaries and sanitization."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from glpi_ticketing_database import (
    AlertAggregate,
    JobAlertSummary,
    RetentionAlertSummary,
    WorkerRuntimeSnapshot,
    WorkerRuntimeState,
    WorkerServiceName,
)
from glpi_ticketing_web.services import (
    AlertKind,
    AlertSeverity,
    HeartbeatStatus,
    ObservabilityPolicy,
    ObservabilityThresholds,
)

NOW = datetime(2026, 8, 29, 12, tzinfo=UTC)
INSTANCE_ID = UUID("e4b68965-df78-4d98-9c99-7ae9b32056a4")
JOB_UUID = UUID("e53683a9-f884-402c-acf8-e9ed5d9fe07c")
DEPLOYED_WORKER_SERVICES = (
    WorkerServiceName.AUTOMATION,
    WorkerServiceName.RETENTION,
)


@pytest.fixture
def policy() -> ObservabilityPolicy:
    return ObservabilityPolicy(_thresholds())


def test_absent_workers_are_always_projected_once(
    policy: ObservabilityPolicy,
) -> None:
    snapshot = policy.evaluate(
        observed_at=NOW,
        runtimes=(),
        jobs=_empty_jobs(),
        retentions=_empty_retentions(),
    )

    assert [worker.service_name for worker in snapshot.workers] == [
        WorkerServiceName.AUTOMATION,
        WorkerServiceName.RETENTION,
    ]
    assert all(
        worker.heartbeat_status is HeartbeatStatus.ABSENT for worker in snapshot.workers
    )
    assert [alert.key for alert in snapshot.alerts] == [
        "workers:worker_automation",
        "workers:worker_retention",
    ]
    assert all(alert.kind is AlertKind.WORKER_ABSENT for alert in snapshot.alerts)
    assert all(alert.condition_since is None for alert in snapshot.alerts)


def test_future_publisher_heartbeat_does_not_break_current_dashboard(
    policy: ObservabilityPolicy,
) -> None:
    publisher = WorkerRuntimeSnapshot(
        service_name=WorkerServiceName.PUBLISHER,
        instance_id=INSTANCE_ID,
        state=WorkerRuntimeState.RUNNING,
        last_heartbeat_at=NOW,
        current_job_uuid=None,
        current_ingestion_uuid=JOB_UUID,
        last_error_category=None,
        last_error_code=None,
        updated_at=NOW,
    )

    snapshot = policy.evaluate(
        observed_at=NOW,
        runtimes=(*_healthy_runtimes(), publisher),
        jobs=_empty_jobs(),
        retentions=_empty_retentions(),
    )

    assert tuple(worker.service_name for worker in snapshot.workers) == (
        WorkerServiceName.AUTOMATION,
        WorkerServiceName.RETENTION,
    )
    assert snapshot.alerts == ()


def test_heartbeat_is_fresh_at_exact_threshold_and_stale_after_it(
    policy: ObservabilityPolicy,
) -> None:
    exact = _runtime(
        WorkerServiceName.AUTOMATION,
        state=WorkerRuntimeState.RUNNING,
        heartbeat_at=NOW - timedelta(seconds=45),
    )
    stale_blocked = _runtime(
        WorkerServiceName.RETENTION,
        state=WorkerRuntimeState.BLOCKED,
        heartbeat_at=NOW - timedelta(seconds=45, microseconds=1),
        error=("dependency_unavailable", "glpi_unavailable"),
    )

    snapshot = policy.evaluate(
        observed_at=NOW,
        runtimes=(exact, stale_blocked),
        jobs=_empty_jobs(),
        retentions=_empty_retentions(),
    )

    assert snapshot.workers[0].heartbeat_status is HeartbeatStatus.FRESH
    assert snapshot.workers[1].heartbeat_status is HeartbeatStatus.STALE
    assert [alert.kind for alert in snapshot.alerts] == [AlertKind.WORKER_STALE]
    assert snapshot.alerts[0].condition_since == NOW - timedelta(microseconds=1)


def test_stopped_state_has_priority_over_stale_heartbeat(
    policy: ObservabilityPolicy,
) -> None:
    stopped = _runtime(
        WorkerServiceName.AUTOMATION,
        state=WorkerRuntimeState.STOPPED,
        heartbeat_at=NOW - timedelta(minutes=5),
    )
    running = _runtime(
        WorkerServiceName.RETENTION,
        state=WorkerRuntimeState.RUNNING,
        heartbeat_at=NOW,
    )

    snapshot = policy.evaluate(
        observed_at=NOW,
        runtimes=(stopped, running),
        jobs=_empty_jobs(),
        retentions=_empty_retentions(),
    )

    assert snapshot.workers[0].heartbeat_status is HeartbeatStatus.STALE
    assert [alert.kind for alert in snapshot.alerts] == [AlertKind.WORKER_STOPPED]


def test_worker_diagnostics_are_defensively_sanitized(
    policy: ObservabilityPolicy,
) -> None:
    secret = "https://user:secret@database/private"
    unsafe = _runtime(
        WorkerServiceName.AUTOMATION,
        state=WorkerRuntimeState.DEGRADED,
        heartbeat_at=NOW,
        error=(secret, "raw response body"),
    )
    safe = _runtime(
        WorkerServiceName.RETENTION,
        state=WorkerRuntimeState.BLOCKED,
        heartbeat_at=NOW,
        error=("dependency_unavailable", "glpi_unavailable"),
    )

    snapshot = policy.evaluate(
        observed_at=NOW,
        runtimes=(unsafe, safe),
        jobs=_empty_jobs(),
        retentions=_empty_retentions(),
    )

    assert snapshot.workers[0].last_error_category is None
    assert snapshot.workers[0].last_error_code is None
    assert snapshot.workers[1].last_error_category == "dependency_unavailable"
    assert snapshot.workers[1].last_error_code == "glpi_unavailable"
    assert secret not in repr(snapshot)
    by_kind = {alert.kind: alert for alert in snapshot.alerts}
    assert by_kind[AlertKind.WORKER_BLOCKED].severity is AlertSeverity.CRITICAL
    assert by_kind[AlertKind.WORKER_DEGRADED].severity is AlertSeverity.WARNING
    assert by_kind[AlertKind.WORKER_BLOCKED].condition_since is None
    assert by_kind[AlertKind.WORKER_DEGRADED].condition_since is None


def test_aggregate_alerts_use_activation_time_and_stable_deduplicated_keys(
    policy: ObservabilityPolicy,
) -> None:
    snapshot = policy.evaluate(
        observed_at=NOW,
        runtimes=_healthy_runtimes(),
        jobs=JobAlertSummary(
            pending_overdue=_aggregate(2, NOW - timedelta(minutes=20)),
            retry_overdue=_aggregate(3, NOW - timedelta(minutes=2)),
            failed=_aggregate(4, NOW - timedelta(hours=3)),
            blocked=_aggregate(5, NOW - timedelta(hours=2)),
        ),
        retentions=RetentionAlertSummary(
            deadline_overdue=_aggregate(6, NOW - timedelta(minutes=5)),
            glpi_check_blocked=_aggregate(7, NOW - timedelta(hours=4)),
            deletion_blocked=_aggregate(8, NOW - timedelta(hours=5)),
            glpi_retry_overdue=_aggregate(9, NOW - timedelta(minutes=3)),
            deletion_retry_overdue=_aggregate(10, NOW - timedelta(minutes=4)),
        ),
    )

    by_key = {alert.key: alert for alert in snapshot.alerts}
    assert len(by_key) == len(snapshot.alerts) == 9
    assert by_key["jobs:pending_overdue"].condition_since == (
        NOW - timedelta(minutes=5)
    )
    assert by_key["jobs:retry_overdue"].condition_since == (NOW - timedelta(minutes=1))
    assert by_key["retentions:deadline_overdue"].condition_since == (
        NOW - timedelta(minutes=3)
    )
    assert by_key["retentions:audio_deletion_retry_overdue"].count == 10
    severities = [alert.severity for alert in snapshot.alerts]
    first_warning = severities.index(AlertSeverity.WARNING)
    assert all(
        severity is AlertSeverity.CRITICAL for severity in severities[:first_warning]
    )
    assert all(
        severity is AlertSeverity.WARNING for severity in severities[first_warning:]
    )


@pytest.mark.parametrize("invalid", [0, -1, float("inf"), float("nan"), True])
def test_observability_thresholds_require_positive_finite_seconds(
    invalid: float | bool,
) -> None:
    with pytest.raises((TypeError, ValueError)):
        ObservabilityThresholds.from_seconds(
            worker_stale_after=invalid,  # type: ignore[arg-type]
            pending_alert_after=900,
            retry_overdue_grace=60,
            retention_deadline_grace=120,
        )


def _thresholds() -> ObservabilityThresholds:
    return ObservabilityThresholds.from_seconds(
        worker_stale_after=45,
        pending_alert_after=900,
        retry_overdue_grace=60,
        retention_deadline_grace=120,
    )


def _aggregate(count: int, condition_since: datetime | None) -> AlertAggregate:
    return AlertAggregate(count=count, condition_since=condition_since)


def _empty_jobs() -> JobAlertSummary:
    empty = _aggregate(0, None)
    return JobAlertSummary(
        pending_overdue=empty,
        retry_overdue=empty,
        failed=empty,
        blocked=empty,
    )


def _empty_retentions() -> RetentionAlertSummary:
    empty = _aggregate(0, None)
    return RetentionAlertSummary(
        deadline_overdue=empty,
        glpi_check_blocked=empty,
        deletion_blocked=empty,
        glpi_retry_overdue=empty,
        deletion_retry_overdue=empty,
    )


def _healthy_runtimes() -> tuple[WorkerRuntimeSnapshot, ...]:
    return tuple(
        _runtime(
            service_name,
            state=WorkerRuntimeState.RUNNING,
            heartbeat_at=NOW,
        )
        for service_name in DEPLOYED_WORKER_SERVICES
    )


def _runtime(
    service_name: WorkerServiceName,
    *,
    state: WorkerRuntimeState,
    heartbeat_at: datetime,
    error: tuple[str, str] | None = None,
) -> WorkerRuntimeSnapshot:
    return WorkerRuntimeSnapshot(
        service_name=service_name,
        instance_id=INSTANCE_ID,
        state=state,
        last_heartbeat_at=heartbeat_at,
        current_job_uuid=JOB_UUID,
        current_ingestion_uuid=None,
        last_error_category=error[0] if error is not None else None,
        last_error_code=error[1] if error is not None else None,
        updated_at=heartbeat_at,
    )
