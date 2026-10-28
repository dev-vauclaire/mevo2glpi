"""Unit tests for the worker executable boundary."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from threading import Event

import pytest

from glpi_ticketing_common import DeploymentEnvironment
from glpi_ticketing_worker_automation.application import (
    WorkerAlreadyRunningError,
    WorkerConfigurationError,
    WorkerExitCode,
    configure_worker_logging,
    run_application,
    run_cli,
)
from glpi_ticketing_worker_automation.settings import WorkerLogLevel, WorkerSettings
from glpi_ticketing_worker_automation.worker import IdleOutcome

SYNTHETIC_SECRET = "never-log-this-secret"
COMMIT_SHA = "bd88d901f4569ea8ee455c53b20920b0e1140ee2"


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
        wait_calls: list[float] = []

        def run_once(self) -> IdleOutcome:
            self.calls += 1
            stop_event.set()
            return IdleOutcome(wait_seconds=60.0)

        def wait(self, seconds: float) -> None:
            self.wait_calls.append(seconds)

        def checkpoint(self) -> None:
            return None

    worker = StoppingWorker()
    runtime_callbacks: dict[str, object] = {}

    @contextmanager
    def runtime(
        _settings: WorkerSettings,
        *,
        wait_once: object,
        should_stop: object,
    ) -> Iterator[StoppingWorker]:
        runtime_callbacks["wait_once"] = wait_once
        runtime_callbacks["should_stop"] = should_stop
        yield worker

    run_application(settings, stop_event, runtime_factory=runtime)

    assert worker.calls == 1
    assert worker.wait_calls == [60]
    assert callable(runtime_callbacks["wait_once"])
    should_stop = runtime_callbacks["should_stop"]
    assert callable(should_stop)
    assert should_stop()


def test_run_cli_rejects_invalid_configuration_without_logging_secret(
    worker_settings_factory: Callable[..., WorkerSettings],
    caplog: pytest.LogCaptureFixture,
) -> None:
    configured_levels: list[WorkerLogLevel] = []

    def invalid_settings() -> WorkerSettings:
        return worker_settings_factory(glpi_password=f"{SYNTHETIC_SECRET}\n")

    with caplog.at_level(logging.INFO):
        exit_code = run_cli(
            settings_factory=invalid_settings,
            logging_configurator=configured_levels.append,
        )

    assert exit_code is WorkerExitCode.CONFIGURATION_ERROR
    assert configured_levels == []
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
            WorkerExitCode.SINGLETON_CONFLICT,
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


def test_run_cli_logs_the_validated_release_identity(
    worker_settings_factory: Callable[..., WorkerSettings],
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = worker_settings_factory(
        app_environment=DeploymentEnvironment.PRODUCTION,
        app_release="v0.2.0",
        app_commit_sha=COMMIT_SHA,
    )

    with caplog.at_level(logging.INFO):
        exit_code = run_cli(
            settings_factory=lambda: settings,
            application_runner=lambda _settings, _stop_event: None,
            signal_context_factory=_passthrough_signals,
        )

    assert exit_code is WorkerExitCode.SUCCESS
    assert "app_environment=production" in caplog.text
    assert "app_release=v0.2.0" in caplog.text
    assert f"app_commit_sha={COMMIT_SHA}" in caplog.text


def test_run_cli_applies_the_validated_worker_log_level(
    worker_settings_factory: Callable[..., WorkerSettings],
) -> None:
    settings = worker_settings_factory(worker_log_level="DEBUG")
    configured_levels: list[WorkerLogLevel] = []

    def assert_logging_configured(
        _settings: WorkerSettings,
        _stop_event: Event,
    ) -> None:
        assert configured_levels == [WorkerLogLevel.DEBUG]

    assert (
        run_cli(
            settings_factory=lambda: settings,
            application_runner=assert_logging_configured,
            signal_context_factory=_passthrough_signals,
            logging_configurator=configured_levels.append,
        )
        is WorkerExitCode.SUCCESS
    )
    assert configured_levels == [WorkerLogLevel.DEBUG]


def test_configure_worker_logging_does_not_enable_dependency_debug_logs() -> None:
    automation_logger = logging.getLogger("glpi_ticketing_worker_automation")
    glpi_logger = logging.getLogger("glpi_ticketing_glpi")
    httpx_logger = logging.getLogger("httpx")
    httpcore_logger = logging.getLogger("httpcore")
    root_logger = logging.getLogger()
    original_levels = (
        automation_logger.level,
        glpi_logger.level,
        httpx_logger.level,
        httpcore_logger.level,
        root_logger.level,
    )
    try:
        configure_worker_logging(WorkerLogLevel.DEBUG)

        assert automation_logger.level == logging.DEBUG
        assert glpi_logger.level == logging.DEBUG
        assert httpx_logger.level == original_levels[2]
        assert httpcore_logger.level == original_levels[3]
        assert root_logger.level == original_levels[4]
    finally:
        automation_logger.setLevel(original_levels[0])
        glpi_logger.setLevel(original_levels[1])
        httpx_logger.setLevel(original_levels[2])
        httpcore_logger.setLevel(original_levels[3])
        root_logger.setLevel(original_levels[4])


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
