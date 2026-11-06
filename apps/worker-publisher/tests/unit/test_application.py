from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from threading import Event

import pytest

from mevo2glpi_common import DeploymentEnvironment, ReleaseIdentity
from mevo2glpi_worker_publisher.application import (
    PublisherAlreadyRunningError,
    PublisherExitCode,
    configure_publisher_logging,
    run_cli,
)
from mevo2glpi_worker_publisher.settings import PublisherLogLevel

SYNTHETIC_SECRET = "never-log-publisher-secret"
COMMIT_SHA = "bd88d901f4569ea8ee455c53b20920b0e1140ee2"


@contextmanager
def _signals(_stop_event: Event) -> Iterator[None]:
    yield


class _Settings:
    publisher_log_level = PublisherLogLevel.DEBUG
    release_identity = ReleaseIdentity(
        environment=DeploymentEnvironment.PRODUCTION,
        release="v0.2.0",
        commit_sha=COMMIT_SHA,
    )


def test_debug_logging_keeps_http_urls_disabled() -> None:
    configure_publisher_logging(PublisherLogLevel.DEBUG)

    assert logging.getLogger("mevo2glpi_worker_publisher").level == logging.DEBUG
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


def test_run_cli_logs_the_validated_release_identity(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO):
        result = run_cli(
            settings_factory=lambda: _Settings(),  # type: ignore[arg-type,return-value]
            application_runner=lambda _settings, _stop_event: None,
            signal_context_factory=_signals,
        )

    assert result is PublisherExitCode.SUCCESS
    assert "app_environment=production" in caplog.text
    assert "app_release=v0.2.0" in caplog.text
    assert f"app_commit_sha={COMMIT_SHA}" in caplog.text
