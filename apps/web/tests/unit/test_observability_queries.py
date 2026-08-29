"""Read-boundary tests for the observability service."""

from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

import pytest

import glpi_ticketing_web.services.observability.queries as query_module
from glpi_ticketing_database import (
    AlertAggregate,
    JobAlertSummary,
    RetentionAlertSummary,
    WorkerRuntimeSnapshot,
    WorkerRuntimeState,
    WorkerServiceName,
)
from glpi_ticketing_web.services import (
    GetObservability,
    ObservabilityThresholds,
    ObservabilityUnavailableError,
)

NOW = datetime(2026, 8, 29, 12, tzinfo=UTC)
INSTANCE_ID = UUID("e4b68965-df78-4d98-9c99-7ae9b32056a4")


class FakeSession:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_observability_query_uses_one_clock_tick_and_strict_cutoffs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeSession()
    calls: dict[str, dict[str, datetime]] = {}

    class RuntimeRepository:
        def __init__(self, actual_session: FakeSession) -> None:
            assert actual_session is session

        def list_all(self) -> tuple[WorkerRuntimeSnapshot, ...]:
            return tuple(
                _runtime(service_name, NOW) for service_name in WorkerServiceName
            )

    class JobRepository:
        def __init__(self, actual_session: FakeSession) -> None:
            assert actual_session is session

        def get_alert_summary(self, **kwargs: datetime) -> JobAlertSummary:
            calls["jobs"] = kwargs
            return _empty_jobs()

    class RetentionRepository:
        def __init__(self, actual_session: FakeSession) -> None:
            assert actual_session is session

        def get_alert_summary(self, **kwargs: datetime) -> RetentionAlertSummary:
            calls["retentions"] = kwargs
            return _empty_retentions()

    monkeypatch.setattr(
        query_module,
        "WorkerRuntimeStatusRepository",
        RuntimeRepository,
    )
    monkeypatch.setattr(query_module, "TicketCreationJobRepository", JobRepository)
    monkeypatch.setattr(query_module, "AudioRetentionRepository", RetentionRepository)
    service = GetObservability(
        lambda: session,  # type: ignore[arg-type]
        thresholds=_thresholds(),
        clock=lambda: NOW,
    )

    snapshot = service.execute()

    assert snapshot.observed_at == NOW
    assert calls == {
        "jobs": {
            "pending_before": NOW - timedelta(minutes=15),
            "retry_before": NOW - timedelta(seconds=60),
        },
        "retentions": {
            "retry_before": NOW - timedelta(seconds=60),
            "deadline_before": NOW - timedelta(seconds=120),
        },
    }
    assert session.closed


def test_observability_query_closes_session_and_hides_repository_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "postgresql://user:secret@database/private"
    session = FakeSession()

    class FailingRuntimeRepository:
        def __init__(self, _session: FakeSession) -> None:
            pass

        def list_all(self) -> tuple[WorkerRuntimeSnapshot, ...]:
            raise RuntimeError(secret)

    monkeypatch.setattr(
        query_module,
        "WorkerRuntimeStatusRepository",
        FailingRuntimeRepository,
    )
    service = GetObservability(
        lambda: session,  # type: ignore[arg-type]
        thresholds=_thresholds(),
        clock=lambda: NOW,
    )

    with pytest.raises(ObservabilityUnavailableError) as captured_error:
        service.execute()

    assert secret not in str(captured_error.value)
    assert session.closed


def test_observability_query_normalizes_aware_clock_to_utc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeSession()
    local_now = NOW.astimezone(timezone(timedelta(hours=2)))

    class RuntimeRepository:
        def __init__(self, _session: FakeSession) -> None:
            pass

        def list_all(self) -> tuple[WorkerRuntimeSnapshot, ...]:
            return ()

    class JobRepository:
        def __init__(self, _session: FakeSession) -> None:
            pass

        def get_alert_summary(self, **_kwargs: datetime) -> JobAlertSummary:
            return _empty_jobs()

    class RetentionRepository:
        def __init__(self, _session: FakeSession) -> None:
            pass

        def get_alert_summary(self, **_kwargs: datetime) -> RetentionAlertSummary:
            return _empty_retentions()

    monkeypatch.setattr(
        query_module,
        "WorkerRuntimeStatusRepository",
        RuntimeRepository,
    )
    monkeypatch.setattr(query_module, "TicketCreationJobRepository", JobRepository)
    monkeypatch.setattr(query_module, "AudioRetentionRepository", RetentionRepository)
    service = GetObservability(
        lambda: session,  # type: ignore[arg-type]
        thresholds=_thresholds(),
        clock=lambda: local_now,
    )

    snapshot = service.execute()

    assert snapshot.observed_at == NOW


def _thresholds() -> ObservabilityThresholds:
    return ObservabilityThresholds.from_seconds(
        worker_stale_after=45,
        pending_alert_after=900,
        retry_overdue_grace=60,
        retention_deadline_grace=120,
    )


def _empty_jobs() -> JobAlertSummary:
    empty = AlertAggregate(count=0, condition_since=None)
    return JobAlertSummary(
        pending_overdue=empty,
        retry_overdue=empty,
        failed=empty,
        blocked=empty,
    )


def _empty_retentions() -> RetentionAlertSummary:
    empty = AlertAggregate(count=0, condition_since=None)
    return RetentionAlertSummary(
        deadline_overdue=empty,
        glpi_check_blocked=empty,
        deletion_blocked=empty,
        glpi_retry_overdue=empty,
        deletion_retry_overdue=empty,
    )


def _runtime(
    service_name: WorkerServiceName,
    heartbeat_at: datetime,
) -> WorkerRuntimeSnapshot:
    return WorkerRuntimeSnapshot(
        service_name=service_name,
        instance_id=INSTANCE_ID,
        state=WorkerRuntimeState.RUNNING,
        last_heartbeat_at=heartbeat_at,
        current_job_uuid=None,
        last_error_category=None,
        last_error_code=None,
        updated_at=heartbeat_at,
    )
