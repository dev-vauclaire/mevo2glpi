from pathlib import Path

import pytest
from pydantic import ValidationError

from glpi_ticketing_web.settings import DEFAULT_MAX_UPLOAD_BYTES, WebSettings

DATABASE_URL = "postgresql+psycopg://web:password@database:5432/jobs"


def test_web_settings_expose_canonical_storage_and_required_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for variable_name in (
        "AUDIO_ROOT",
        "DATABASE_URL",
        "AUDIO_PENDING_DIR",
        "AUDIO_COMPLETED_DIR",
        "WEB_HOST",
        "WEB_PORT",
        "WEB_TRUST_PROXY_HEADERS",
        "MAX_UPLOAD_BYTES",
        "WORKER_HEARTBEAT_INTERVAL_SECONDS",
        "DASHBOARD_WORKER_STALE_AFTER_SECONDS",
        "DASHBOARD_PENDING_ALERT_AFTER_SECONDS",
        "DASHBOARD_RETRY_OVERDUE_GRACE_SECONDS",
        "DASHBOARD_RETENTION_DEADLINE_GRACE_SECONDS",
    ):
        monkeypatch.delenv(variable_name, raising=False)
        monkeypatch.delenv(variable_name.lower(), raising=False)

    with pytest.raises(ValidationError, match="database_url"):
        WebSettings(_env_file=None)

    settings = WebSettings(database_url=DATABASE_URL, _env_file=None)
    assert settings.audio_root == Path("/data/audios")
    assert settings.database_url.get_secret_value() == DATABASE_URL
    assert settings.web_host == "127.0.0.1"
    assert settings.web_port == 8000
    assert settings.web_trust_proxy_headers is False
    assert settings.max_upload_bytes == DEFAULT_MAX_UPLOAD_BYTES
    assert settings.worker_heartbeat_interval_seconds == 10.0
    assert settings.dashboard_worker_stale_after_seconds == 45.0
    assert settings.dashboard_pending_alert_after_seconds == 900.0
    assert settings.dashboard_retry_overdue_grace_seconds == 60.0
    assert settings.dashboard_retention_deadline_grace_seconds == 120.0
    assert not hasattr(settings, "audio_pending_dir")
    assert not hasattr(settings, "audio_completed_dir")


def test_web_settings_read_environment_overrides(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    audio_root = tmp_path / "audios"
    monkeypatch.setenv("AUDIO_ROOT", str(audio_root))
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("WEB_HOST", "0.0.0.0")
    monkeypatch.setenv("WEB_PORT", "8080")
    monkeypatch.setenv("WEB_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "4096")
    monkeypatch.setenv("WORKER_HEARTBEAT_INTERVAL_SECONDS", "8.5")
    monkeypatch.setenv("DASHBOARD_WORKER_STALE_AFTER_SECONDS", "35.5")
    monkeypatch.setenv("DASHBOARD_PENDING_ALERT_AFTER_SECONDS", "600")
    monkeypatch.setenv("DASHBOARD_RETRY_OVERDUE_GRACE_SECONDS", "30")
    monkeypatch.setenv("DASHBOARD_RETENTION_DEADLINE_GRACE_SECONDS", "90")

    settings = WebSettings(_env_file=None)

    assert settings.audio_root == audio_root
    assert settings.database_url.get_secret_value() == DATABASE_URL
    assert settings.web_host == "0.0.0.0"
    assert settings.web_port == 8080
    assert settings.web_trust_proxy_headers is True
    assert settings.max_upload_bytes == 4096
    assert settings.worker_heartbeat_interval_seconds == 8.5
    assert settings.dashboard_worker_stale_after_seconds == 35.5
    assert settings.dashboard_pending_alert_after_seconds == 600
    assert settings.dashboard_retry_overdue_grace_seconds == 30
    assert settings.dashboard_retention_deadline_grace_seconds == 90


@pytest.mark.parametrize(
    "invalid_url",
    [
        "",
        "not-a-url",
        "postgresql://web:password@database/jobs",
        "sqlite:///jobs.db",
    ],
)
def test_web_database_url_requires_psycopg_without_exposing_its_value(
    invalid_url: str,
) -> None:
    with pytest.raises(ValidationError) as captured_error:
        WebSettings(database_url=invalid_url, _env_file=None)

    if invalid_url:
        assert invalid_url not in str(captured_error.value)


@pytest.mark.parametrize("invalid_limit", [0, -1, True])
def test_web_upload_limit_must_be_a_positive_integer(
    invalid_limit: int | bool,
) -> None:
    with pytest.raises(ValidationError):
        WebSettings(
            database_url=DATABASE_URL,
            max_upload_bytes=invalid_limit,
            _env_file=None,
        )


@pytest.mark.parametrize("invalid_port", [0, 65536, True])
def test_web_port_must_be_in_tcp_range(invalid_port: int | bool) -> None:
    with pytest.raises(ValidationError):
        WebSettings(
            database_url=DATABASE_URL,
            web_port=invalid_port,
            _env_file=None,
        )


@pytest.mark.parametrize(
    "invalid_host",
    ["", "localhost", " 127.0.0.1", "127.0.0.1\nforged-log"],
)
def test_web_host_must_be_an_ip_address(invalid_host: str) -> None:
    with pytest.raises(ValidationError):
        WebSettings(
            database_url=DATABASE_URL,
            web_host=invalid_host,
            _env_file=None,
        )


@pytest.mark.parametrize(
    "field_name",
    [
        "worker_heartbeat_interval_seconds",
        "dashboard_worker_stale_after_seconds",
        "dashboard_pending_alert_after_seconds",
        "dashboard_retry_overdue_grace_seconds",
        "dashboard_retention_deadline_grace_seconds",
    ],
)
@pytest.mark.parametrize("invalid_duration", [0, -1, True, float("inf"), float("nan")])
def test_observability_durations_must_be_positive_finite_numbers(
    field_name: str,
    invalid_duration: float | bool,
) -> None:
    with pytest.raises(ValidationError):
        WebSettings(
            database_url=DATABASE_URL,
            **{field_name: invalid_duration},
            _env_file=None,
        )


@pytest.mark.parametrize("stale_after", [10, 9.999])
def test_stale_threshold_must_exceed_heartbeat_interval(
    stale_after: float,
) -> None:
    with pytest.raises(ValidationError):
        WebSettings(
            database_url=DATABASE_URL,
            worker_heartbeat_interval_seconds=10,
            dashboard_worker_stale_after_seconds=stale_after,
            _env_file=None,
        )
