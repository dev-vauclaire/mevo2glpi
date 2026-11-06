from __future__ import annotations

import logging

import pytest

from mevo2glpi_openai_transcription_simulator.application import (
    OpenAiTranscriptionSimulatorExitCode,
    build_server,
    run_cli,
)
from mevo2glpi_openai_transcription_simulator.settings import (
    OpenAiTranscriptionSimulatorSettings,
)


def _settings() -> OpenAiTranscriptionSimulatorSettings:
    return OpenAiTranscriptionSimulatorSettings(
        openai_transcription_simulator_host="127.0.0.1",
        openai_transcription_simulator_port=9878,
        openai_transcription_simulator_api_key="synthetic-openai-key",
        _env_file=None,
    )


def test_build_server_disables_access_logging() -> None:
    server = build_server(_settings())

    assert server.config.host == "127.0.0.1"
    assert server.config.port == 9878
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

    assert exit_code is OpenAiTranscriptionSimulatorExitCode.SUCCESS
    assert server.started is True


def test_invalid_configuration_does_not_log_secret(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "CHANGE_ME_WITH_A_LONG_DOCUMENTATION_PLACEHOLDER"

    def invalid_settings() -> OpenAiTranscriptionSimulatorSettings:
        return OpenAiTranscriptionSimulatorSettings(
            openai_transcription_simulator_api_key=secret,
            _env_file=None,
        )

    with caplog.at_level(logging.INFO):
        exit_code = run_cli(settings_factory=invalid_settings)

    assert exit_code is OpenAiTranscriptionSimulatorExitCode.CONFIGURATION_ERROR
    assert secret not in caplog.text
