"""Unit tests for the retention-worker executable boundary."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from threading import Event

import pytest

from glpi_ticketing_worker_retention.application import (
    RetentionWorkerAlreadyRunningError,
    RetentionWorkerConfigurationError,
    RetentionWorkerExitCode,
    run_application,
    run_cli,
)
from glpi_ticketing_worker_retention.retention import IdleOutcome
from glpi_ticketing_worker_retention.settings import RetentionWorkerSettings

SYNTHETIC_SECRET = "never-log-this-retention-secret"


@contextmanager
def _passthrough_signals(_stop_event: Event) -> Iterator[None]:
    yield


def test_run_application_uses_runtime_and_stops_without_real_sleep(
    retention_settings_factory: Callable[..., RetentionWorkerSettings],
) -> None:
    settings = retention_settings_factory()
    stop_event = Event()
    received_settings: list[RetentionWorkerSettings] = []
    wait_calls: list[float] = []

    class StoppingWorker:
        calls = 0

        def run_once(self) -> IdleOutcome:
            self.calls += 1
            return IdleOutcome(wait_seconds=60.0)

        def wait(self, seconds: float) -> None:
            wait_calls.append(seconds)
            stop_event.set()

    worker = StoppingWorker()

    @contextmanager
    def runtime(
        runtime_settings: RetentionWorkerSettings,
        *,
        wait_once: Callable[[float], object],
        should_stop: Callable[[], bool],
    ) -> Iterator[StoppingWorker]:
        assert callable(wait_once)
        assert callable(should_stop)
        received_settings.append(runtime_settings)
        yield worker

    run_application(settings, stop_event, runtime_factory=runtime)

    assert received_settings == [settings]
    assert worker.calls == 1
    assert wait_calls == [60.0]
    assert stop_event.is_set()


def test_run_cli_rejects_invalid_configuration_without_logging_secret(
    retention_settings_factory: Callable[..., RetentionWorkerSettings],
    caplog: pytest.LogCaptureFixture,
) -> None:
    def invalid_settings() -> RetentionWorkerSettings:
        return retention_settings_factory(glpi_password=f"{SYNTHETIC_SECRET}\n")

    with caplog.at_level(logging.INFO):
        exit_code = run_cli(settings_factory=invalid_settings)

    assert exit_code is RetentionWorkerExitCode.CONFIGURATION_ERROR
    assert "retention worker configuration is invalid" in caplog.text
    assert SYNTHETIC_SECRET not in caplog.text


def test_run_cli_sanitizes_unexpected_configuration_loading_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def failing_settings() -> RetentionWorkerSettings:
        raise RuntimeError(SYNTHETIC_SECRET)

    with caplog.at_level(logging.INFO):
        exit_code = run_cli(settings_factory=failing_settings)

    assert exit_code is RetentionWorkerExitCode.UNEXPECTED_ERROR
    assert "error_type=RuntimeError" in caplog.text
    assert SYNTHETIC_SECRET not in caplog.text


@pytest.mark.parametrize(
    ("error_factory", "expected_code", "expected_log"),
    [
        (
            lambda: RetentionWorkerConfigurationError(SYNTHETIC_SECRET),
            RetentionWorkerExitCode.CONFIGURATION_ERROR,
            "retention worker runtime configuration is invalid",
        ),
        (
            lambda: RetentionWorkerAlreadyRunningError(SYNTHETIC_SECRET),
            RetentionWorkerExitCode.SINGLETON_CONFLICT,
            "retention worker singleton lock is already held",
        ),
        (
            lambda: RuntimeError(SYNTHETIC_SECRET),
            RetentionWorkerExitCode.UNEXPECTED_ERROR,
            "error_type=RuntimeError",
        ),
    ],
)
def test_run_cli_classifies_runtime_errors_without_logging_messages(
    retention_settings_factory: Callable[..., RetentionWorkerSettings],
    caplog: pytest.LogCaptureFixture,
    error_factory: Callable[[], Exception],
    expected_code: RetentionWorkerExitCode,
    expected_log: str,
) -> None:
    settings = retention_settings_factory()

    def failing_runner(
        _settings: RetentionWorkerSettings,
        _stop_event: Event,
    ) -> None:
        raise error_factory()

    with caplog.at_level(logging.INFO):
        exit_code = run_cli(
            settings_factory=lambda: settings,
            application_runner=failing_runner,
            signal_context_factory=_passthrough_signals,
        )

    assert exit_code is expected_code
    assert expected_log in caplog.text
    assert SYNTHETIC_SECRET not in caplog.text


def test_run_cli_returns_success_after_cooperative_shutdown(
    retention_settings_factory: Callable[..., RetentionWorkerSettings],
) -> None:
    settings = retention_settings_factory()

    @contextmanager
    def request_shutdown(stop_event: Event) -> Iterator[None]:
        stop_event.set()
        yield

    def assert_stopped(
        received_settings: RetentionWorkerSettings,
        stop_event: Event,
    ) -> None:
        assert received_settings is settings
        assert stop_event.is_set()

    assert (
        run_cli(
            settings_factory=lambda: settings,
            application_runner=assert_stopped,
            signal_context_factory=request_shutdown,
        )
        is RetentionWorkerExitCode.SUCCESS
    )


def test_run_cli_returns_130_for_keyboard_interrupt(
    retention_settings_factory: Callable[..., RetentionWorkerSettings],
) -> None:
    settings = retention_settings_factory()

    def interrupt(
        _settings: RetentionWorkerSettings,
        _stop_event: Event,
    ) -> None:
        raise KeyboardInterrupt

    assert (
        run_cli(
            settings_factory=lambda: settings,
            application_runner=interrupt,
            signal_context_factory=_passthrough_signals,
        )
        is RetentionWorkerExitCode.INTERRUPTED
    )
