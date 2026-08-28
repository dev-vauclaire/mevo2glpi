"""Unit tests for the worker executable boundary."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from threading import Event

import pytest

from glpi_ticketing_worker_automation.application import (
    WorkerAlreadyRunningError,
    WorkerConfigurationError,
    WorkerExitCode,
    run_application,
    run_cli,
)
from glpi_ticketing_worker_automation.settings import WorkerSettings
from glpi_ticketing_worker_automation.worker import IdleOutcome

SYNTHETIC_SECRET = "never-log-this-secret"


@contextmanager
def _passthrough_signals(_stop_event: Event) -> Iterator[None]:
    yield


def test_run_application_stops_cooperatively_without_real_sleep(
    worker_settings_factory: Callable[..., WorkerSettings],
) -> None:
    settings = worker_settings_factory()
    stop_event = Event()

    class StoppingWorker:
        calls = 0

        def run_once(self) -> IdleOutcome:
            self.calls += 1
            stop_event.set()
            return IdleOutcome(wait_seconds=60.0)

    worker = StoppingWorker()
    runtime_callbacks: dict[str, object] = {}

    @contextmanager
    def runtime(
        _settings: WorkerSettings,
        *,
        transcription_sleep: object,
        transcription_should_stop: object,
    ) -> Iterator[StoppingWorker]:
        runtime_callbacks["sleep"] = transcription_sleep
        runtime_callbacks["should_stop"] = transcription_should_stop
        yield worker

    run_application(settings, stop_event, runtime_factory=runtime)

    assert worker.calls == 1
    assert callable(runtime_callbacks["sleep"])
    should_stop = runtime_callbacks["should_stop"]
    assert callable(should_stop)
    assert should_stop()


def test_run_cli_rejects_invalid_configuration_without_logging_secret(
    worker_settings_factory: Callable[..., WorkerSettings],
    caplog: pytest.LogCaptureFixture,
) -> None:
    def invalid_settings() -> WorkerSettings:
        return worker_settings_factory(glpi_password=f"{SYNTHETIC_SECRET}\n")

    with caplog.at_level(logging.INFO):
        exit_code = run_cli(settings_factory=invalid_settings)

    assert exit_code is WorkerExitCode.CONFIGURATION_ERROR
    assert "worker configuration is invalid" in caplog.text
    assert SYNTHETIC_SECRET not in caplog.text


@pytest.mark.parametrize(
    ("error_factory", "expected_code", "expected_log"),
    [
        (
            lambda: WorkerConfigurationError(SYNTHETIC_SECRET),
            WorkerExitCode.CONFIGURATION_ERROR,
            "worker runtime configuration is invalid",
        ),
        (
            lambda: WorkerAlreadyRunningError(SYNTHETIC_SECRET),
            WorkerExitCode.BLOCKED,
            "worker singleton lock is already held",
        ),
        (
            lambda: RuntimeError(SYNTHETIC_SECRET),
            WorkerExitCode.UNEXPECTED_ERROR,
            "error_type=RuntimeError",
        ),
    ],
)
def test_run_cli_classifies_runtime_errors_without_logging_messages(
    worker_settings_factory: Callable[..., WorkerSettings],
    caplog: pytest.LogCaptureFixture,
    error_factory: Callable[[], Exception],
    expected_code: WorkerExitCode,
    expected_log: str,
) -> None:
    settings = worker_settings_factory()

    def failing_runner(_settings: WorkerSettings, _stop_event: Event) -> None:
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
    worker_settings_factory: Callable[..., WorkerSettings],
) -> None:
    settings = worker_settings_factory()

    @contextmanager
    def request_shutdown(stop_event: Event) -> Iterator[None]:
        stop_event.set()
        yield

    def assert_stopped(_settings: WorkerSettings, stop_event: Event) -> None:
        assert stop_event.is_set()

    assert (
        run_cli(
            settings_factory=lambda: settings,
            application_runner=assert_stopped,
            signal_context_factory=request_shutdown,
        )
        is WorkerExitCode.SUCCESS
    )


def test_run_cli_returns_130_for_unhandled_keyboard_interrupt(
    worker_settings_factory: Callable[..., WorkerSettings],
) -> None:
    settings = worker_settings_factory()

    def interrupt(_settings: WorkerSettings, _stop_event: Event) -> None:
        raise KeyboardInterrupt

    assert (
        run_cli(
            settings_factory=lambda: settings,
            application_runner=interrupt,
            signal_context_factory=_passthrough_signals,
        )
        is WorkerExitCode.INTERRUPTED
    )
