from __future__ import annotations

from pathlib import Path

import pytest

import mevo2glpi_worker_publisher.runtime.healthcheck as healthcheck_module
from mevo2glpi_common.runtime_healthcheck import (
    AudioAccessMode,
    HealthcheckSettings,
)


def test_probe_requires_read_write_audio_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[AudioAccessMode] = []

    def probe(
        _settings: HealthcheckSettings,
        *,
        heartbeat_reader: object,
        audio_access_mode: AudioAccessMode,
    ) -> None:
        assert heartbeat_reader is healthcheck_module._read_heartbeat
        captured.append(audio_access_mode)

    monkeypatch.setattr(healthcheck_module, "probe_worker_health", probe)

    healthcheck_module.probe_health(
        HealthcheckSettings(
            database_url="postgresql+psycopg://publisher:secret@database/jobs",
            audio_root=tmp_path,
        )
    )

    assert captured == [AudioAccessMode.READ_WRITE]


def test_healthcheck_uses_the_publisher_heartbeat_interval_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+psycopg://publisher:secret@database/jobs",
    )
    monkeypatch.setenv("PUBLISHER_HEARTBEAT_INTERVAL_SECONDS", "17")
    monkeypatch.delenv("WORKER_HEARTBEAT_INTERVAL_SECONDS", raising=False)

    settings = healthcheck_module._settings_from_environment()

    assert settings.heartbeat_interval_seconds == 17.0
