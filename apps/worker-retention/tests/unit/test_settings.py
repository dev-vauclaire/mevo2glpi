"""Unit tests for retention-worker environment configuration."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import ValidationError

from mevo2glpi_common import DeploymentEnvironment
from mevo2glpi_worker_retention.settings import (
    REJECTED_INGESTION_RETENTION_BATCH_SIZE,
    RetentionWorkerSettings,
)

DATABASE_URL = "postgresql+psycopg://retention:password@database:5432/jobs"
GLPI_REQUIRED_ENVIRONMENT = {
    "GLPI_BASE_URL": "https://glpi.example.test",
    "GLPI_USERNAME": "synthetic-retention-worker",
    "GLPI_PASSWORD": "synthetic-password",
    "GLPI_CLIENT_ID": "synthetic-client-id",
    "GLPI_CLIENT_SECRET": "synthetic-client-secret",
}
COMMIT_SHA = "bd88d901f4569ea8ee455c53b20920b0e1140ee2"


@pytest.fixture(autouse=True)
def configure_required_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide mandatory settings unless a test removes one explicitly."""
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    for name, value in GLPI_REQUIRED_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)


def test_settings_require_database_and_glpi_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for variable_name, field_name in (
        ("DATABASE_URL", "database_url"),
        ("GLPI_BASE_URL", "glpi_base_url"),
        ("GLPI_USERNAME", "glpi_username"),
        ("GLPI_PASSWORD", "glpi_password"),
        ("GLPI_CLIENT_ID", "glpi_client_id"),
        ("GLPI_CLIENT_SECRET", "glpi_client_secret"),
    ):
        monkeypatch.delenv(variable_name)
        with pytest.raises(ValidationError, match=field_name):
            RetentionWorkerSettings(_env_file=None)
        monkeypatch.setenv(
            variable_name,
            DATABASE_URL
            if variable_name == "DATABASE_URL"
            else GLPI_REQUIRED_ENVIRONMENT[variable_name],
        )


def test_settings_expose_safe_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for variable_name in (
        "AUDIO_ROOT",
        "DATABASE_CONNECT_TIMEOUT_SECONDS",
        "WORKER_HEARTBEAT_INTERVAL_SECONDS",
        "RETENTION_POLL_INTERVAL_SECONDS",
        "RETENTION_RETRY_INITIAL_SECONDS",
        "RETENTION_RETRY_MAX_SECONDS",
        "RETENTION_SYSTEM_PROBE_INTERVAL_SECONDS",
        "RETENTION_CHECK_INTERVAL_HOURS",
        "REJECTED_INGESTION_RETENTION_DAYS",
        "GLPI_HTTP_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(variable_name, raising=False)

    settings = RetentionWorkerSettings(_env_file=None)

    assert settings.audio_root == Path("/data/audios")
    assert settings.database_connect_timeout_seconds == 5
    assert settings.worker_heartbeat_interval_seconds == 10.0
    assert settings.retention_poll_interval_seconds == 60.0
    assert settings.retention_retry_initial_seconds == 5.0
    assert settings.retention_retry_max_seconds == 300.0
    assert settings.retention_system_probe_interval_seconds == 60.0
    assert settings.retention_check_interval_hours == 6
    assert settings.rejected_ingestion_retention_days == 90
    assert REJECTED_INGESTION_RETENTION_BATCH_SIZE == 100
    assert settings.glpi_http_timeout_seconds == 10.0


def test_settings_read_environment_overrides(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    audio_root = tmp_path / "audio"
    overrides = {
        "APP_ENVIRONMENT": "production",
        "APP_RELEASE": "v0.2.0",
        "APP_COMMIT_SHA": COMMIT_SHA,
        "AUDIO_ROOT": str(audio_root),
        "DATABASE_CONNECT_TIMEOUT_SECONDS": "9",
        "WORKER_HEARTBEAT_INTERVAL_SECONDS": "8.5",
        "RETENTION_POLL_INTERVAL_SECONDS": "12.5",
        "RETENTION_RETRY_INITIAL_SECONDS": "4",
        "RETENTION_RETRY_MAX_SECONDS": "121",
        "RETENTION_SYSTEM_PROBE_INTERVAL_SECONDS": "46",
        "RETENTION_CHECK_INTERVAL_HOURS": "7",
        "REJECTED_INGESTION_RETENTION_DAYS": "120",
        "GLPI_HTTP_TIMEOUT_SECONDS": "11.5",
    }
    for name, value in overrides.items():
        monkeypatch.setenv(name, value)

    settings = RetentionWorkerSettings(_env_file=None)

    assert settings.release_identity.environment is DeploymentEnvironment.PRODUCTION
    assert settings.release_identity.release == "v0.2.0"
    assert settings.release_identity.commit_sha == COMMIT_SHA
    assert settings.audio_root == audio_root
    assert settings.database_connect_timeout_seconds == 9
    assert settings.worker_heartbeat_interval_seconds == 8.5
    assert settings.retention_poll_interval_seconds == 12.5
    assert settings.retention_retry_initial_seconds == 4.0
    assert settings.retention_retry_max_seconds == 121.0
    assert settings.retention_system_probe_interval_seconds == 46.0
    assert settings.retention_check_interval_hours == 7
    assert settings.rejected_ingestion_retention_days == 120
    assert settings.glpi_http_timeout_seconds == 11.5


def test_removed_first_check_variable_cannot_override_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RETENTION_CHECK_INTERVAL_HOURS", raising=False)
    monkeypatch.setenv("RETENTION_FIRST_CHECK_HOURS", "23")

    settings = RetentionWorkerSettings(_env_file=None)

    assert settings.retention_check_interval_hours == 6
    assert not hasattr(settings, "retention_first_check_hours")


@pytest.mark.parametrize(
    "field_name",
    [
        "retention_poll_interval_seconds",
        "worker_heartbeat_interval_seconds",
        "retention_retry_initial_seconds",
        "retention_retry_max_seconds",
        "retention_system_probe_interval_seconds",
        "glpi_http_timeout_seconds",
    ],
)
@pytest.mark.parametrize(
    "invalid_value",
    [True, 0, -1, float("inf"), float("nan")],
)
def test_duration_settings_must_be_positive_and_finite(
    retention_settings_factory: Callable[..., RetentionWorkerSettings],
    field_name: str,
    invalid_value: float,
) -> None:
    with pytest.raises(ValidationError):
        retention_settings_factory(**{field_name: invalid_value})


@pytest.mark.parametrize(
    "field_name",
    [
        "database_connect_timeout_seconds",
        "retention_check_interval_hours",
        "rejected_ingestion_retention_days",
    ],
)
@pytest.mark.parametrize("invalid_value", [True, 0, -1])
def test_integer_durations_reject_booleans_and_non_positive_values(
    retention_settings_factory: Callable[..., RetentionWorkerSettings],
    field_name: str,
    invalid_value: object,
) -> None:
    with pytest.raises(ValidationError):
        retention_settings_factory(**{field_name: invalid_value})


def test_removed_rejected_ingestion_tuning_variables_are_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REJECTED_INGESTION_RETENTION_MODE", "report")
    monkeypatch.setenv("REJECTED_INGESTION_RETENTION_BATCH_SIZE", "1")
    monkeypatch.setenv("REJECTED_INGESTION_RETENTION_SCAN_INTERVAL_HOURS", "24")

    settings = RetentionWorkerSettings(_env_file=None)

    assert not hasattr(settings, "rejected_ingestion_retention_mode")
    assert not hasattr(settings, "rejected_ingestion_retention_batch_size")
    assert not hasattr(settings, "rejected_ingestion_retention_scan_interval_hours")
    assert REJECTED_INGESTION_RETENTION_BATCH_SIZE == 100


def test_retry_initial_delay_cannot_exceed_cap(
    retention_settings_factory: Callable[..., RetentionWorkerSettings],
) -> None:
    with pytest.raises(ValidationError, match="must not exceed"):
        retention_settings_factory(
            retention_retry_initial_seconds=31,
            retention_retry_max_seconds=30,
        )


@pytest.mark.parametrize(
    "invalid_url",
    ["", "not-a-url", "postgresql://worker:password@database/jobs", "sqlite:///x"],
)
def test_database_url_requires_psycopg_without_exposing_value(
    retention_settings_factory: Callable[..., RetentionWorkerSettings],
    invalid_url: str,
) -> None:
    with pytest.raises(ValidationError) as captured_error:
        retention_settings_factory(database_url=invalid_url)

    if invalid_url:
        assert invalid_url not in str(captured_error.value)


@pytest.mark.parametrize(
    "invalid_url",
    [
        "not-an-http-url",
        "ftp://glpi.example.test",
        "https://user:password@glpi.example.test",
        "https://glpi.example.test?source=retention",
        "https://glpi.example.test#fragment",
    ],
)
def test_glpi_url_rejects_ambiguous_or_non_http_values(
    retention_settings_factory: Callable[..., RetentionWorkerSettings],
    invalid_url: str,
) -> None:
    with pytest.raises(ValidationError):
        retention_settings_factory(glpi_base_url=invalid_url)


@pytest.mark.parametrize("field_name", ["glpi_username", "glpi_client_id"])
@pytest.mark.parametrize("invalid_value", ["", " padded", "line\nbreak"])
def test_glpi_identifiers_must_be_trimmed_and_printable(
    retention_settings_factory: Callable[..., RetentionWorkerSettings],
    field_name: str,
    invalid_value: str,
) -> None:
    with pytest.raises(ValidationError):
        retention_settings_factory(**{field_name: invalid_value})


@pytest.mark.parametrize("field_name", ["glpi_password", "glpi_client_secret"])
@pytest.mark.parametrize("invalid_value", ["", " padded", "line\nbreak"])
def test_glpi_secrets_must_be_trimmed_and_printable(
    retention_settings_factory: Callable[..., RetentionWorkerSettings],
    field_name: str,
    invalid_value: str,
) -> None:
    with pytest.raises(ValidationError):
        retention_settings_factory(**{field_name: invalid_value})


def test_settings_repr_hides_all_secrets(
    retention_settings_factory: Callable[..., RetentionWorkerSettings],
) -> None:
    database_password = "never-print-database-password"
    glpi_password = "never-print-glpi-password"
    client_secret = "never-print-client-secret"
    settings = retention_settings_factory(
        database_url=(f"postgresql+psycopg://worker:{database_password}@postgres/jobs"),
        glpi_password=glpi_password,
        glpi_client_secret=client_secret,
    )

    settings_repr = repr(settings)

    assert database_password not in settings_repr
    assert glpi_password not in settings_repr
    assert client_secret not in settings_repr
