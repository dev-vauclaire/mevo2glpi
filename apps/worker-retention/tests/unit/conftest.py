"""Shared fixtures for retention-worker unit tests."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from glpi_ticketing_worker_retention.settings import RetentionWorkerSettings


@pytest.fixture
def retention_settings_factory(
    tmp_path: Path,
) -> Callable[..., RetentionWorkerSettings]:
    """Build valid settings while allowing one field to be overridden."""

    def build(**overrides: object) -> RetentionWorkerSettings:
        audio_root = tmp_path / "audios"
        audio_root.mkdir(parents=True, exist_ok=True)
        values: dict[str, object] = {
            "database_url": (
                "postgresql+psycopg://retention:synthetic-password@postgres/jobs"
            ),
            "audio_root": audio_root,
            "database_connect_timeout_seconds": 7,
            "worker_heartbeat_interval_seconds": 10.0,
            "retention_poll_interval_seconds": 61.0,
            "retention_retry_initial_seconds": 3.5,
            "retention_retry_max_seconds": 30.0,
            "retention_system_probe_interval_seconds": 47.0,
            "retention_check_interval_hours": 8,
            "glpi_base_url": "https://glpi.example.test/prefix",
            "glpi_username": "synthetic-retention-worker",
            "glpi_password": "synthetic-password",
            "glpi_client_id": "synthetic-client-id",
            "glpi_client_secret": "synthetic-client-secret",
            "glpi_http_timeout_seconds": 5.5,
        }
        values.update(overrides)
        return RetentionWorkerSettings(**values, _env_file=None)

    return build
