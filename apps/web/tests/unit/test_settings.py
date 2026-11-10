from pathlib import Path

import pytest
from pydantic import ValidationError

from mevo2glpi_common.release_identity import DeploymentEnvironment
from mevo2glpi_web.settings import WebSettings

DATABASE_URL = "postgresql+psycopg://web:password@database:5432/jobs"
SESSION_SECRET = "synthetic-test-session-secret-at-least-32-characters"


def _settings(**overrides: object) -> WebSettings:
    values: dict[str, object] = {
        "database_url": DATABASE_URL,
        "web_auth_mode": "local",
        "web_auth_session_secret": SESSION_SECRET,
        "web_auth_cookie_secure": False,
    }
    values.update(overrides)
    return WebSettings(**values, _env_file=None)


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
        "WEB_AUTH_MODE",
        "WEB_AUTH_SESSION_SECRET",
        "WEB_AUTH_SESSION_MAX_AGE_SECONDS",
        "WEB_AUTH_COOKIE_SECURE",
        "WEB_AUTH_REQUIRED_ROLE",
        "WEB_AUTH_ROLES_CLAIM",
        "WEB_AUTH_ISSUER_URL",
        "WEB_AUTH_CLIENT_ID",
        "WEB_AUTH_CLIENT_SECRET",
        "WEB_AUTH_REDIRECT_URI",
        "WORKER_HEARTBEAT_INTERVAL_SECONDS",
        "DASHBOARD_WORKER_STALE_AFTER_SECONDS",
        "DASHBOARD_PENDING_ALERT_AFTER_SECONDS",
        "DASHBOARD_RETRY_OVERDUE_GRACE_SECONDS",
        "DASHBOARD_RETENTION_DEADLINE_GRACE_SECONDS",
        "APP_ENVIRONMENT",
        "APP_RELEASE",
        "APP_COMMIT_SHA",
    ):
        monkeypatch.delenv(variable_name, raising=False)
        monkeypatch.delenv(variable_name.lower(), raising=False)

    with pytest.raises(ValidationError) as captured_error:
        WebSettings(_env_file=None)
    assert "database_url" in str(captured_error.value)
    assert "web_auth_mode" in str(captured_error.value)
    assert "web_auth_session_secret" in str(captured_error.value)

    settings = _settings()
    assert settings.audio_root == Path("/data/audios")
    assert settings.database_url.get_secret_value() == DATABASE_URL
    assert settings.web_host == "127.0.0.1"
    assert settings.web_port == 8000
    assert settings.web_trust_proxy_headers is False
    assert settings.web_auth_mode.value == "local"
    assert settings.web_auth_session_secret.get_secret_value() == SESSION_SECRET
    assert settings.web_auth_session_max_age_seconds == 1800
    assert settings.web_auth_cookie_secure is False
    assert settings.web_auth_required_role == "mevo2glpi-access"
    assert settings.web_auth_roles_claim == "roles"
    assert settings.web_auth_issuer_url is None
    assert not hasattr(settings, "max_upload_bytes")
    assert settings.worker_heartbeat_interval_seconds == 10.0
    assert settings.dashboard_worker_stale_after_seconds == 45.0
    assert settings.dashboard_pending_alert_after_seconds == 900.0
    assert settings.dashboard_retry_overdue_grace_seconds == 60.0
    assert settings.dashboard_retention_deadline_grace_seconds == 120.0
    assert settings.app_environment is DeploymentEnvironment.DEVELOPMENT
    assert settings.app_release == "development"
    assert settings.app_commit_sha == "unknown"
    assert settings.release_identity.short_commit_sha == "unknown"
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
    monkeypatch.setenv("WEB_AUTH_MODE", "local")
    monkeypatch.setenv("WEB_AUTH_SESSION_SECRET", SESSION_SECRET)
    monkeypatch.setenv("WEB_AUTH_SESSION_MAX_AGE_SECONDS", "1200")
    monkeypatch.setenv("WEB_AUTH_COOKIE_SECURE", "true")
    monkeypatch.setenv("WORKER_HEARTBEAT_INTERVAL_SECONDS", "8.5")
    monkeypatch.setenv("DASHBOARD_WORKER_STALE_AFTER_SECONDS", "35.5")
    monkeypatch.setenv("DASHBOARD_PENDING_ALERT_AFTER_SECONDS", "600")
    monkeypatch.setenv("DASHBOARD_RETRY_OVERDUE_GRACE_SECONDS", "30")
    monkeypatch.setenv("DASHBOARD_RETENTION_DEADLINE_GRACE_SECONDS", "90")
    monkeypatch.setenv("APP_ENVIRONMENT", "staging")
    monkeypatch.setenv("APP_RELEASE", "main")
    monkeypatch.setenv(
        "APP_COMMIT_SHA",
        "BD88D901F4569EA8EE455C53B20920B0E1140EE2",
    )

    settings = WebSettings(_env_file=None)

    assert settings.audio_root == audio_root
    assert settings.database_url.get_secret_value() == DATABASE_URL
    assert settings.web_host == "0.0.0.0"
    assert settings.web_port == 8080
    assert settings.web_trust_proxy_headers is True
    assert settings.web_auth_mode.value == "local"
    assert settings.web_auth_session_max_age_seconds == 1200
    assert settings.web_auth_cookie_secure is True
    assert settings.worker_heartbeat_interval_seconds == 8.5
    assert settings.dashboard_worker_stale_after_seconds == 35.5
    assert settings.dashboard_pending_alert_after_seconds == 600
    assert settings.dashboard_retry_overdue_grace_seconds == 30
    assert settings.dashboard_retention_deadline_grace_seconds == 90
    assert settings.app_environment is DeploymentEnvironment.STAGING
    assert settings.app_release == "main"
    assert settings.app_commit_sha == "BD88D901F4569EA8EE455C53B20920B0E1140EE2"
    assert settings.release_identity.commit_sha == (
        "bd88d901f4569ea8ee455c53b20920b0e1140ee2"
    )


def test_web_settings_reject_inconsistent_release_identity_without_echoing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invalid_sha = "never-log-this-invalid-sha"
    monkeypatch.setenv("APP_ENVIRONMENT", "production")
    monkeypatch.setenv("APP_RELEASE", "v0.2.0")
    monkeypatch.setenv("APP_COMMIT_SHA", invalid_sha)

    with pytest.raises(ValidationError) as captured_error:
        _settings(web_auth_cookie_secure=True)

    assert invalid_sha not in str(captured_error.value)


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
        _settings(database_url=invalid_url)

    if invalid_url:
        assert invalid_url not in str(captured_error.value)


@pytest.mark.parametrize("invalid_port", [0, 65536, True])
def test_web_port_must_be_in_tcp_range(invalid_port: int | bool) -> None:
    with pytest.raises(ValidationError):
        _settings(web_port=invalid_port)


@pytest.mark.parametrize(
    "invalid_host",
    ["", "localhost", " 127.0.0.1", "127.0.0.1\nforged-log"],
)
def test_web_host_must_be_an_ip_address(invalid_host: str) -> None:
    with pytest.raises(ValidationError):
        _settings(web_host=invalid_host)


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
        _settings(**{field_name: invalid_duration})


@pytest.mark.parametrize("stale_after", [10, 9.999])
def test_stale_threshold_must_exceed_heartbeat_interval(
    stale_after: float,
) -> None:
    with pytest.raises(ValidationError):
        _settings(
            worker_heartbeat_interval_seconds=10,
            dashboard_worker_stale_after_seconds=stale_after,
        )


def test_web_authentication_mode_is_required_and_has_no_disabled_value() -> None:
    with pytest.raises(ValidationError):
        WebSettings(
            database_url=DATABASE_URL,
            web_auth_session_secret=SESSION_SECRET,
            _env_file=None,
        )

    with pytest.raises(ValidationError):
        WebSettings(
            database_url=DATABASE_URL,
            web_auth_mode="disabled",
            web_auth_session_secret=SESSION_SECRET,
            _env_file=None,
        )


def test_oidc_mode_requires_complete_generic_provider_configuration() -> None:
    with pytest.raises(ValidationError, match="OIDC authentication"):
        _settings(web_auth_mode="oidc")

    settings = _settings(
        web_auth_mode="oidc",
        web_auth_issuer_url="http://localhost:18084/realms/test",
        web_auth_client_id="mevo2glpi-web",
        web_auth_client_secret="synthetic-client-secret",
        web_auth_redirect_uri="http://127.0.0.1:8000/auth/oidc/callback",
    )

    assert str(settings.web_auth_issuer_url).rstrip("/") == (
        "http://localhost:18084/realms/test"
    )


def test_non_development_authentication_requires_secure_https_configuration() -> None:
    with pytest.raises(ValidationError, match="COOKIE_SECURE"):
        _settings(app_environment="staging", web_auth_cookie_secure=False)

    with pytest.raises(ValidationError, match="HTTPS"):
        _settings(
            app_environment="staging",
            web_auth_cookie_secure=True,
            web_auth_mode="oidc",
            web_auth_issuer_url="http://identity.example.test/realm",
            web_auth_client_id="mevo2glpi-web",
            web_auth_client_secret="synthetic-client-secret",
            web_auth_redirect_uri="http://dashboard.example.test/auth/oidc/callback",
        )


def test_local_mode_rejects_unused_oidc_secrets() -> None:
    with pytest.raises(ValidationError, match="must be unset"):
        _settings(web_auth_client_secret="unused-secret")
