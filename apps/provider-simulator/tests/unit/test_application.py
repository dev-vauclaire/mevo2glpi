from __future__ import annotations

import logging
from uuid import UUID

import pytest

from glpi_ticketing_provider_simulator.application import (
    ProviderSimulatorExitCode,
    build_server,
    run_cli,
)
from glpi_ticketing_provider_simulator.settings import ProviderSimulatorSettings

MAILBOX_ID = UUID("10000000-0000-4000-8000-000000000001")


def _settings() -> ProviderSimulatorSettings:
    return ProviderSimulatorSettings(
        provider_simulator_host="127.0.0.1",
        provider_simulator_port=9876,
        provider_simulator_mailbox_id=MAILBOX_ID,
        provider_simulator_admin_token="synthetic-admin-token",
        provider_simulator_transcription_api_key="synthetic-transcription-key",
        _env_file=None,
    )


def test_build_server_disables_access_logging() -> None:
    server = build_server(_settings())

    assert server.config.host == "127.0.0.1"
    assert server.config.port == 9876
    assert server.config.access_log is False
    assert server.config.proxy_headers is False


def test_run_cli_starts_the_composed_server() -> None:
    settings = _settings()

    class FakeServer:
        started = False

        def run(self) -> None:
            self.started = True

    server = FakeServer()
    exit_code = run_cli(
        settings_factory=lambda: settings,
        server_factory=lambda _settings: server,
    )

    assert exit_code is ProviderSimulatorExitCode.SUCCESS
    assert server.started is True


def test_invalid_configuration_does_not_log_secret(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "secret-too-short"

    def invalid_settings() -> ProviderSimulatorSettings:
        return ProviderSimulatorSettings(
            provider_simulator_mailbox_id=MAILBOX_ID,
            provider_simulator_admin_token=secret,
            provider_simulator_transcription_api_key="short",
            _env_file=None,
        )

    with caplog.at_level(logging.INFO):
        exit_code = run_cli(settings_factory=invalid_settings)

    assert exit_code is ProviderSimulatorExitCode.CONFIGURATION_ERROR
    assert secret not in caplog.text
