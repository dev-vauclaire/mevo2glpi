from __future__ import annotations

import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from glpi_ticketing_web.application import (
    WebExitCode,
    build_application,
    build_server,
    run_application,
    run_cli,
)
from glpi_ticketing_web.settings import WebSettings

DATABASE_URL = "postgresql+psycopg://web:password@127.0.0.1:5432/jobs"


def _settings(tmp_path: Path, **overrides: object) -> WebSettings:
    audio_root = tmp_path / "audios"
    audio_root.mkdir(parents=True, exist_ok=True)
    values: dict[str, object] = {
        "audio_root": audio_root,
        "database_url": DATABASE_URL,
        "web_host": "127.0.0.1",
        "web_port": 8765,
    }
    values.update(overrides)
    return WebSettings(**values, _env_file=None)


def test_build_application_wires_settings_into_fastapi(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    application = build_application(settings)

    config = application.state.web_config
    assert config.audio_root == settings.audio_root.resolve()
    assert config.release_identity == settings.release_identity
    assert not hasattr(config, "max_upload_bytes")


def test_production_disables_documentation_without_removing_application_routes(
    tmp_path: Path,
) -> None:
    settings = _settings(
        tmp_path,
        app_environment="production",
        app_release="v0.5.0",
        app_commit_sha="a" * 40,
    )
    application = build_application(settings)

    with TestClient(application) as client:
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert client.get(path).status_code == 404
        for path in ("/", "/health", "/api/version"):
            assert client.get(path).status_code == 200

    registered_paths = set(application.openapi()["paths"])
    assert "/api/jobs" in registered_paths
    assert "/api/jobs/{audio_uuid}" in registered_paths
    assert "/audios/{audio_uuid}" in registered_paths


@pytest.mark.parametrize(
    "identity",
    [
        pytest.param({}, id="development"),
        pytest.param(
            {
                "app_environment": "staging",
                "app_release": "main",
                "app_commit_sha": "b" * 40,
            },
            id="staging",
        ),
    ],
)
def test_non_production_environments_expose_documentation(
    tmp_path: Path,
    identity: dict[str, object],
) -> None:
    application = build_application(_settings(tmp_path, **identity))

    with TestClient(application) as client:
        assert client.get("/docs").status_code == 200
        assert client.get("/redoc").status_code == 200
        assert client.get("/openapi.json").status_code == 200


def test_build_server_uses_configured_bind_address(tmp_path: Path) -> None:
    settings = _settings(tmp_path, web_host="0.0.0.0", web_port=9876)

    server = build_server(settings)

    assert server.config.host == "0.0.0.0"
    assert server.config.port == 9876
    assert server.config.proxy_headers is False
    assert server.config.forwarded_allow_ips == []
    assert server.config.app.state.web_config.audio_root == (
        settings.audio_root.resolve()
    )


def test_build_server_only_trusts_proxy_headers_when_configured(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, web_trust_proxy_headers=True)

    server = build_server(settings)

    assert server.config.proxy_headers is True
    assert server.config.forwarded_allow_ips == "*"


def test_run_application_starts_composed_server(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    observed_settings: list[WebSettings] = []

    class FakeServer:
        started = False

        def run(self) -> None:
            self.started = True

    server = FakeServer()

    def server_factory(factory_settings: WebSettings) -> FakeServer:
        observed_settings.append(factory_settings)
        return server

    run_application(settings, server_factory=server_factory)

    assert observed_settings == [settings]
    assert server.started


def test_run_cli_returns_success_after_server_stops(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    observed_settings: list[WebSettings] = []

    exit_code = run_cli(
        settings_factory=lambda: settings,
        application_runner=observed_settings.append,
    )

    assert exit_code is WebExitCode.SUCCESS
    assert observed_settings == [settings]


def test_run_cli_logs_only_non_sensitive_release_identity(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = _settings(
        tmp_path,
        app_environment="production",
        app_release="v0.2.0",
        app_commit_sha="bd88d901f4569ea8ee455c53b20920b0e1140ee2",
    )

    with caplog.at_level(logging.INFO):
        exit_code = run_cli(
            settings_factory=lambda: settings,
            application_runner=lambda _settings: None,
        )

    assert exit_code is WebExitCode.SUCCESS
    assert "app_environment=production" in caplog.text
    assert "app_release=v0.2.0" in caplog.text
    assert "app_commit_sha=bd88d901f4569ea8ee455c53b20920b0e1140ee2" in caplog.text
    assert "postgresql+psycopg" not in caplog.text


def test_run_cli_does_not_log_invalid_database_url(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "never-log-this-database-password"

    def invalid_settings() -> WebSettings:
        return WebSettings(
            database_url=f"postgresql://web:{secret}@database/jobs",
            _env_file=None,
        )

    with caplog.at_level(logging.INFO):
        exit_code = run_cli(settings_factory=invalid_settings)

    assert exit_code is WebExitCode.CONFIGURATION_ERROR
    assert secret not in caplog.text
    assert "web configuration is invalid" in caplog.text


def test_run_cli_rejects_missing_audio_root_without_logging_its_path(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = _settings(tmp_path)
    missing_directory = settings.audio_root
    missing_directory.rmdir()

    with caplog.at_level(logging.INFO):
        exit_code = run_cli(settings_factory=lambda: settings)

    assert exit_code is WebExitCode.CONFIGURATION_ERROR
    assert str(missing_directory) not in caplog.text
    assert "web runtime configuration is invalid" in caplog.text


def test_run_cli_does_not_log_unexpected_exception_message(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = _settings(tmp_path)
    secret = "never-log-this-runtime-value"

    def fail(_settings: WebSettings) -> None:
        raise RuntimeError(secret)

    with caplog.at_level(logging.INFO):
        exit_code = run_cli(
            settings_factory=lambda: settings,
            application_runner=fail,
        )

    assert exit_code is WebExitCode.UNEXPECTED_ERROR
    assert secret not in caplog.text
    assert "error_type=RuntimeError" in caplog.text


def test_run_cli_returns_130_for_unhandled_keyboard_interrupt(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    def interrupt(_settings: WebSettings) -> None:
        raise KeyboardInterrupt

    exit_code = run_cli(
        settings_factory=lambda: settings,
        application_runner=interrupt,
    )

    assert exit_code is WebExitCode.INTERRUPTED
