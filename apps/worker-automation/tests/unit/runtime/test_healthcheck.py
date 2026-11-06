"""Tests for the automation worker's healthcheck adapter."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.exc import OperationalError

import mevo2glpi_worker_automation.runtime.healthcheck as healthcheck_module
from mevo2glpi_common.runtime_healthcheck import (
    AudioAccessMode,
    HealthcheckCode,
    HealthcheckFailure,
    HealthcheckSettings,
)
from mevo2glpi_database import (
    WorkerRuntimeSnapshot,
    WorkerRuntimeState,
    WorkerServiceName,
)

DATABASE_URL = "postgresql+psycopg://worker:secret@database/jobs"
NOW = datetime(2026, 8, 29, 12, tzinfo=UTC)


def _settings(tmp_path: Path, database_url: str = DATABASE_URL) -> HealthcheckSettings:
    return HealthcheckSettings(database_url=database_url, audio_root=tmp_path)


def test_probe_selects_read_only_storage_and_automation_reader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, object, object]] = []

    def capture(
        settings: HealthcheckSettings,
        *,
        heartbeat_reader: object,
        audio_access_mode: object,
    ) -> None:
        calls.append((settings, heartbeat_reader, audio_access_mode))

    monkeypatch.setattr(healthcheck_module, "probe_worker_health", capture)
    settings = _settings(tmp_path)

    healthcheck_module.probe_health(settings)

    assert calls == [
        (settings, healthcheck_module._read_heartbeat, AudioAccessMode.READ_ONLY)
    ]


def test_reader_selects_only_automation_heartbeat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeEngine:
        disposed = False

        def dispose(self) -> None:
            self.disposed = True

    class FakeSession:
        def __enter__(self) -> FakeSession:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    def snapshot(service_name: WorkerServiceName) -> WorkerRuntimeSnapshot:
        return WorkerRuntimeSnapshot(
            service_name=service_name,
            instance_id=uuid4(),
            state=WorkerRuntimeState.RUNNING,
            last_heartbeat_at=NOW,
            current_job_uuid=None,
            current_ingestion_uuid=None,
            last_error_category=None,
            last_error_code=None,
            updated_at=NOW,
        )

    snapshots = (
        snapshot(WorkerServiceName.RETENTION),
        snapshot(WorkerServiceName.AUTOMATION),
    )
    engine = FakeEngine()
    monkeypatch.setattr(
        healthcheck_module,
        "create_database_engine",
        lambda *_args, **_kwargs: engine,
    )
    monkeypatch.setattr(
        healthcheck_module,
        "create_session_factory",
        lambda _engine: FakeSession,
    )
    monkeypatch.setattr(
        healthcheck_module,
        "WorkerRuntimeStatusRepository",
        lambda _session: type(
            "FakeRepository", (), {"list_all": lambda _self: snapshots}
        )(),
    )

    result = healthcheck_module._read_heartbeat(_settings(tmp_path))

    assert result is not None
    assert result.state == WorkerRuntimeState.RUNNING.value
    assert result.last_heartbeat_at == NOW
    assert engine.disposed is True


def test_invalid_database_driver_is_sanitized(tmp_path: Path) -> None:
    invalid_url = "postgresql://worker:very-secret@database/jobs"

    with pytest.raises(HealthcheckFailure) as captured_error:
        healthcheck_module._read_heartbeat(_settings(tmp_path, invalid_url))

    assert captured_error.value.code is HealthcheckCode.INVALID_CONFIGURATION
    assert invalid_url not in str(captured_error.value)


def test_database_unavailability_is_sanitized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(*_args: object, **_kwargs: object) -> object:
        raise OperationalError("SELECT secret", {}, RuntimeError(DATABASE_URL))

    monkeypatch.setattr(healthcheck_module, "create_database_engine", unavailable)

    with pytest.raises(HealthcheckFailure) as captured_error:
        healthcheck_module._read_heartbeat(_settings(tmp_path))

    assert captured_error.value.code is HealthcheckCode.DATABASE_UNAVAILABLE
    assert DATABASE_URL not in str(captured_error.value)
