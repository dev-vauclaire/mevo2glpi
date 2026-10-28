"""Shared fixtures for worker application unit tests."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from glpi_ticketing_worker_automation.settings import WorkerSettings


@pytest.fixture
def worker_settings_factory(
    tmp_path: Path,
) -> Callable[..., WorkerSettings]:
    """Build valid settings while allowing one test to override a field."""

    def build(**overrides: object) -> WorkerSettings:
        audio_root = tmp_path / "audios"
        audio_root.mkdir(parents=True, exist_ok=True)
        values: dict[str, object] = {
            "public_base_url": "https://web.example.test/base/",
            "database_url": (
                "postgresql+psycopg://worker:synthetic-password@postgres/automation"
            ),
            "audio_root": audio_root,
            "database_connect_timeout_seconds": 7,
            "worker_idle_interval_seconds": 1.25,
            "worker_retry_initial_seconds": 3.5,
            "worker_retry_max_seconds": 30.0,
            "worker_system_probe_interval_seconds": 45.0,
            "completed_retention_days": 91,
            "failed_retention_days": 31,
            "blocked_retention_days": 92,
            "retention_check_interval_hours": 7,
            "legacy_transcription_base_url": ("https://transcribe.example.test/prefix"),
            "legacy_transcription_api_key": "synthetic-transcription-key",
            "transcription_http_timeout_seconds": 4.5,
            "transcription_poll_interval_seconds": 2.5,
            "transcription_job_timeout_seconds": 120.0,
            "glpi_base_url": "https://glpi.example.test/prefix",
            "glpi_username": "synthetic-worker",
            "glpi_password": "synthetic-password",
            "glpi_client_id": "synthetic-client-id",
            "glpi_client_secret": "synthetic-client-secret",
            "glpi_http_timeout_seconds": 5.5,
            "glpi_ticket_title": "Demande audio",
            "glpi_ticket_type": 2,
            "glpi_ticket_request_source_id": 42,
            "glpi_ticket_urgency": 4,
            "glpi_ticket_impact": 3,
        }
        values.update(overrides)
        return WorkerSettings(**values, _env_file=None)

    return build
