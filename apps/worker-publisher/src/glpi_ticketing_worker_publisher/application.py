"""Executable lifecycle of the Microsoft 365 publisher worker."""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractContextManager
from enum import IntEnum
from threading import Event
from typing import Protocol

from pydantic import ValidationError

from .composition import PublisherConfigurationError
from .publisher import run_forever
from .runtime import (
    PublisherAlreadyRunningError,
    RuntimePublisherWorkerProtocol,
    publisher_runtime,
    shutdown_signal_handlers,
)
from .settings import PublisherLogLevel, PublisherSettings

LOGGER = logging.getLogger(__name__)
_APPLICATION_LOGGER_NAMES = ("glpi_ticketing_worker_publisher",)
_QUIET_DEPENDENCY_LOGGER_NAMES = ("httpx2", "httpcore2", "httpcore")


class PublisherExitCode(IntEnum):
    """Stable process exit codes exposed to the container runtime."""

    SUCCESS = 0
    UNEXPECTED_ERROR = 1
    CONFIGURATION_ERROR = 2
    SINGLETON_CONFLICT = 3
    INTERRUPTED = 130


class RuntimeFactoryProtocol(Protocol):
    """Create the publisher resource context."""

    def __call__(
        self,
        settings: PublisherSettings,
        *,
        wait_once: Callable[[float], object],
        should_stop: Callable[[], bool],
    ) -> AbstractContextManager[RuntimePublisherWorkerProtocol]: ...


def configure_publisher_logging(level: PublisherLogLevel) -> None:
    """Configure application-owned loggers without enabling dependency payloads."""
    if not isinstance(level, PublisherLogLevel):
        raise TypeError("level must be a PublisherLogLevel")
    for logger_name in _APPLICATION_LOGGER_NAMES:
        logging.getLogger(logger_name).setLevel(level.value)
    # Graph URLs contain immutable message and attachment identifiers. Keep
    # transport request logging disabled even when publisher diagnostics are
    # intentionally raised to DEBUG.
    for logger_name in _QUIET_DEPENDENCY_LOGGER_NAMES:
        logging.getLogger(logger_name).setLevel(logging.WARNING)


def run_application(
    settings: PublisherSettings,
    stop_event: Event,
    *,
    runtime_factory: RuntimeFactoryProtocol = publisher_runtime,
) -> None:
    """Run the composed publisher until cooperative shutdown."""

    def wait_for_work(seconds: float) -> None:
        stop_event.wait(seconds)

    with runtime_factory(
        settings,
        wait_once=wait_for_work,
        should_stop=stop_event.is_set,
    ) as worker:
        run_forever(
            worker,
            sleep=worker.wait,
            should_stop=stop_event.is_set,
        )


def run_cli(
    *,
    settings_factory: Callable[[], PublisherSettings] = PublisherSettings,
    application_runner: Callable[[PublisherSettings, Event], None] = run_application,
    signal_context_factory: Callable[
        [Event], AbstractContextManager[None]
    ] = shutdown_signal_handlers,
) -> PublisherExitCode:
    """Load configuration and run with content-free startup diagnostics."""
    try:
        settings = settings_factory()
    except ValidationError:
        LOGGER.error("publisher configuration is invalid")
        return PublisherExitCode.CONFIGURATION_ERROR
    except Exception as error:  # pragma: no cover - defensive startup boundary
        LOGGER.error(
            "publisher configuration loading failed error_type=%s",
            type(error).__name__,
        )
        return PublisherExitCode.UNEXPECTED_ERROR

    configure_publisher_logging(settings.publisher_log_level)
    stop_event = Event()
    release_identity = settings.release_identity
    LOGGER.info(
        "publisher worker starting app_environment=%s app_release=%s app_commit_sha=%s",
        release_identity.environment.value,
        release_identity.release,
        release_identity.commit_sha,
    )
    try:
        with signal_context_factory(stop_event):
            application_runner(settings, stop_event)
    except PublisherConfigurationError:
        LOGGER.error("publisher runtime configuration is invalid")
        return PublisherExitCode.CONFIGURATION_ERROR
    except PublisherAlreadyRunningError:
        LOGGER.error("publisher singleton lock is already held")
        return PublisherExitCode.SINGLETON_CONFLICT
    except KeyboardInterrupt:
        LOGGER.info("publisher worker interrupted")
        return PublisherExitCode.INTERRUPTED
    except Exception as error:  # pragma: no cover - tested through a fake runner
        LOGGER.error(
            "publisher worker stopped unexpectedly error_type=%s",
            type(error).__name__,
        )
        return PublisherExitCode.UNEXPECTED_ERROR

    LOGGER.info("publisher worker stopped")
    return PublisherExitCode.SUCCESS


def main() -> int:
    """Console-script entry point."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    return int(run_cli())


__all__ = [
    "PublisherExitCode",
    "RuntimeFactoryProtocol",
    "configure_publisher_logging",
    "main",
    "run_application",
    "run_cli",
]
