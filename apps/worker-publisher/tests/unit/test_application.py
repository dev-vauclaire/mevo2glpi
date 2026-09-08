from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from threading import Event

import pytest

from glpi_ticketing_worker_publisher.application import (
    PublisherAlreadyRunningError,
    PublisherExitCode,
    configure_publisher_logging,
    run_cli,
)
from glpi_ticketing_worker_publisher.settings import PublisherLogLevel

SYNTHETIC_SECRET = "never-log-publisher-secret"


@contextmanager
def _signals(_stop_event: Event) -> Iterator[None]:
    yield


class _Settings:
    publisher_log_level = PublisherLogLevel.DEBUG


def test_debug_logging_keeps_http_urls_disabled() -> None:
    configure_publisher_logging(PublisherLogLevel.DEBUG)

    assert logging.getLogger("glpi_ticketing_worker_publisher").level == logging.DEBUG
    assert logging.getLogger("httpx2").level == logging.WARNING
    assert logging.getLogger("httpcore2").level == logging.WARNING


def test_configuration_loading_error_never_logs_its_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def fail() -> _Settings:
        raise RuntimeError(SYNTHETIC_SECRET)

    with caplog.at_level(logging.INFO):
        result = run_cli(settings_factory=fail)  # type: ignore[arg-type]

    assert result is PublisherExitCode.UNEXPECTED_ERROR
    assert "error_type=RuntimeError" in caplog.text
    assert SYNTHETIC_SECRET not in caplog.text


def test_singleton_conflict_has_a_stable_exit_code_without_error_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def fail(_settings: object, _stop_event: Event) -> None:
        raise PublisherAlreadyRunningError(SYNTHETIC_SECRET)

    with caplog.at_level(logging.INFO):
        result = run_cli(
            settings_factory=lambda: _Settings(),  # type: ignore[arg-type,return-value]
            application_runner=fail,  # type: ignore[arg-type]
            signal_context_factory=_signals,
        )

    assert result is PublisherExitCode.SINGLETON_CONFLICT
    assert SYNTHETIC_SECRET not in caplog.text
