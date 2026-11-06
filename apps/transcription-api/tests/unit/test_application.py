from __future__ import annotations

import logging

import pytest

from mevo2glpi_transcription_api.application import (
    TranscriptionApiExitCode,
    build_server,
    run_application,
    run_cli,
)
from mevo2glpi_transcription_api.settings import TranscriptionSettings

API_KEY = "synthetic-transcription-key"


def _settings(**overrides: object) -> TranscriptionSettings:
    values: dict[str, object] = {"transcription_api_key": API_KEY}
    values.update(overrides)
    return TranscriptionSettings(**values, _env_file=None)


def test_build_server_uses_configured_private_http_runtime() -> None:
    server = build_server(
        _settings(transcription_host="0.0.0.0", transcription_port=9876)
    )

    assert server.config.host == "0.0.0.0"
    assert server.config.port == 9876
    assert server.config.proxy_headers is False
    assert server.config.forwarded_allow_ips == []


def test_run_application_starts_server() -> None:
    settings = _settings()

    class FakeServer:
        started = False

        def run(self) -> None:
            self.started = True

    server = FakeServer()
    run_application(settings, server_factory=lambda _settings: server)
    assert server.started is True


def test_run_cli_rejects_invalid_configuration_without_secret(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "short-secret"

    def invalid_settings() -> TranscriptionSettings:
        return TranscriptionSettings(
            transcription_api_key=secret,
            _env_file=None,
        )

    with caplog.at_level(logging.INFO):
        exit_code = run_cli(settings_factory=invalid_settings)

    assert exit_code is TranscriptionApiExitCode.CONFIGURATION_ERROR
    assert secret not in caplog.text


def test_run_cli_returns_success_for_clean_shutdown() -> None:
    settings = _settings()
    observed: list[TranscriptionSettings] = []

    exit_code = run_cli(
        settings_factory=lambda: settings,
        application_runner=observed.append,
    )

    assert exit_code is TranscriptionApiExitCode.SUCCESS
    assert observed == [settings]
