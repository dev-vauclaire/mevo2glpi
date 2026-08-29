from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import create_autospec
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

import glpi_ticketing_database as database_api
from glpi_ticketing_database import (
    StaleWorkerInstanceError,
    WorkerRuntimeState,
    WorkerRuntimeStatus,
    WorkerRuntimeStatusRepository,
    WorkerServiceName,
)

NOW = datetime(2026, 8, 28, 12, tzinfo=UTC)


def test_runtime_repository_upserts_sanitized_heartbeat_without_commit() -> None:
    session = _session()
    session.get.return_value = None
    instance_id = uuid4()
    job_uuid = uuid4()

    snapshot = WorkerRuntimeStatusRepository(session).record_heartbeat(
        service_name=WorkerServiceName.AUTOMATION,
        instance_id=instance_id,
        state=WorkerRuntimeState.DEGRADED,
        now=NOW,
        current_job_uuid=job_uuid,
        error_category="unavailable",
        error_code="http_503",
    )

    assert snapshot.service_name is WorkerServiceName.AUTOMATION
    assert snapshot.state is WorkerRuntimeState.DEGRADED
    assert snapshot.current_job_uuid == job_uuid
    runtime = next(
        entity for entity in _added(session) if isinstance(entity, WorkerRuntimeStatus)
    )
    assert runtime.last_heartbeat_at == NOW
    session.flush.assert_called_once_with()
    session.commit.assert_not_called()
    session.rollback.assert_not_called()
    session.close.assert_not_called()


def test_runtime_repository_rejects_a_superseded_instance() -> None:
    session = _session()
    active_instance_id = uuid4()
    stale_instance_id = uuid4()
    runtime = WorkerRuntimeStatus(
        service_name=WorkerServiceName.AUTOMATION.value,
        instance_id=active_instance_id,
        state=WorkerRuntimeState.RUNNING.value,
        last_heartbeat_at=NOW,
        current_job_uuid=None,
        last_error_category=None,
        last_error_code=None,
        updated_at=NOW,
    )
    session.get.return_value = runtime

    with pytest.raises(StaleWorkerInstanceError, match="superseded"):
        WorkerRuntimeStatusRepository(session).record_heartbeat(
            service_name=WorkerServiceName.AUTOMATION,
            instance_id=stale_instance_id,
            state=WorkerRuntimeState.STOPPED,
            now=NOW + timedelta(seconds=1),
        )

    assert runtime.instance_id == active_instance_id
    assert runtime.state == WorkerRuntimeState.RUNNING.value
    session.flush.assert_not_called()


def test_starting_heartbeat_can_fence_the_previous_instance() -> None:
    session = _session()
    replacement_instance_id = uuid4()
    runtime = WorkerRuntimeStatus(
        service_name=WorkerServiceName.AUTOMATION.value,
        instance_id=uuid4(),
        state=WorkerRuntimeState.RUNNING.value,
        last_heartbeat_at=NOW,
        current_job_uuid=None,
        last_error_category=None,
        last_error_code=None,
        updated_at=NOW,
    )
    session.get.return_value = runtime

    snapshot = WorkerRuntimeStatusRepository(session).record_heartbeat(
        service_name=WorkerServiceName.AUTOMATION,
        instance_id=replacement_instance_id,
        state=WorkerRuntimeState.STARTING,
        now=NOW + timedelta(seconds=1),
    )

    assert snapshot.instance_id == replacement_instance_id
    assert snapshot.state is WorkerRuntimeState.STARTING
    session.flush.assert_called_once_with()


def test_worker_runtime_contracts_are_exported_by_package() -> None:
    assert database_api.WorkerRuntimeStatusRepository is WorkerRuntimeStatusRepository
    assert database_api.StaleWorkerInstanceError is StaleWorkerInstanceError


def test_runtime_repository_lists_typed_snapshots_without_commit() -> None:
    session = _session()
    automation = WorkerRuntimeStatus(
        service_name=WorkerServiceName.AUTOMATION.value,
        instance_id=uuid4(),
        state=WorkerRuntimeState.RUNNING.value,
        last_heartbeat_at=NOW,
        current_job_uuid=None,
        last_error_category=None,
        last_error_code=None,
        updated_at=NOW,
    )
    retention = WorkerRuntimeStatus(
        service_name=WorkerServiceName.RETENTION.value,
        instance_id=uuid4(),
        state=WorkerRuntimeState.DEGRADED.value,
        last_heartbeat_at=NOW - timedelta(seconds=1),
        current_job_uuid=uuid4(),
        last_error_category="unavailable",
        last_error_code="http_503",
        updated_at=NOW - timedelta(seconds=1),
    )
    session.scalars.return_value.all.return_value = [automation, retention]

    snapshots = WorkerRuntimeStatusRepository(session).list_all()

    assert tuple(snapshot.service_name for snapshot in snapshots) == (
        WorkerServiceName.AUTOMATION,
        WorkerServiceName.RETENTION,
    )
    assert snapshots[1].last_error_code == "http_503"
    session.scalars.assert_called_once()
    session.flush.assert_not_called()
    session.commit.assert_not_called()


def _session() -> Session:
    return create_autospec(Session, instance=True)


def _added(session: Session) -> list[object]:
    return [recorded_call.args[0] for recorded_call in session.add.call_args_list]
