"""Executable lifecycle of the automation worker application."""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractContextManager
from enum import IntEnum
from threading import Event
from typing import Protocol

from pydantic import ValidationError

from .composition import WorkerConfigurationError
from .runtime import (
    RuntimeAutomationWorkerProtocol,
    WorkerAlreadyRunningError,
    shutdown_signal_handlers,
    worker_runtime,
)
from .settings import WorkerLogLevel, WorkerSettings
from .worker import run_forever

LOGGER = logging.getLogger(__name__)
_APPLICATION_LOGGER_NAMES = (
    "glpi_ticketing_worker_automation",
    "glpi_ticketing_glpi",
)


class WorkerExitCode(IntEnum):
    """Stable process exit codes exposed to the container runtime."""

    SUCCESS = 0
    UNEXPECTED_ERROR = 1
    CONFIGURATION_ERROR = 2
    SINGLETON_CONFLICT = 3
    INTERRUPTED = 130


class RuntimeFactoryProtocol(Protocol):
    """Create the resource context used by ``run_application``."""

    def __call__(
        self,
        settings: WorkerSettings,
        *,
        wait_once: Callable[[float], object],
        should_stop: Callable[[], bool],
    ) -> AbstractContextManager[RuntimeAutomationWorkerProtocol]: ...


class LoggingConfiguratorProtocol(Protocol):
    """Apply the validated runtime level to application-owned loggers."""

    def __call__(self, level: WorkerLogLevel) -> None: ...


def configure_worker_logging(level: WorkerLogLevel) -> None:
    """Configure only application namespaces, leaving dependencies quiet."""
    if not isinstance(level, WorkerLogLevel):
        raise TypeError("level must be a WorkerLogLevel")
    for logger_name in _APPLICATION_LOGGER_NAMES:
        logging.getLogger(logger_name).setLevel(level.value)


def run_application(
    settings: WorkerSettings,
    stop_event: Event,
    *,
    runtime_factory: RuntimeFactoryProtocol = worker_runtime,
) -> None:
    """Run the composed worker until cooperative shutdown."""

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
    settings_factory: Callable[[], WorkerSettings] = WorkerSettings,
    application_runner: Callable[[WorkerSettings, Event], None] = run_application,
    signal_context_factory: Callable[
        [Event], AbstractContextManager[None]
    ] = shutdown_signal_handlers,
    logging_configurator: LoggingConfiguratorProtocol = configure_worker_logging,
) -> WorkerExitCode:
    """Load configuration and run the worker with sanitized diagnostics."""
    try:
        settings = settings_factory()
    except ValidationError:
        LOGGER.error("worker configuration is invalid")
        return WorkerExitCode.CONFIGURATION_ERROR
    except Exception as error:  # pragma: no cover - defensive startup boundary
        LOGGER.error(
            "worker configuration loading failed error_type=%s",
            type(error).__name__,
        )
        return WorkerExitCode.UNEXPECTED_ERROR

    logging_configurator(settings.worker_log_level)
    stop_event = Event()
    release_identity = settings.release_identity
    LOGGER.info(
        "worker starting app_environment=%s app_release=%s app_commit_sha=%s",
        release_identity.environment.value,
        release_identity.release,
        release_identity.commit_sha,
    )
    try:
        with signal_context_factory(stop_event):
            application_runner(settings, stop_event)
    except WorkerConfigurationError:
        LOGGER.error("worker runtime configuration is invalid")
        return WorkerExitCode.CONFIGURATION_ERROR
    except WorkerAlreadyRunningError:
        LOGGER.error("worker singleton lock is already held")
        return WorkerExitCode.SINGLETON_CONFLICT
    except KeyboardInterrupt:
        LOGGER.info("worker interrupted")
        return WorkerExitCode.INTERRUPTED
    except Exception as error:  # pragma: no cover - tested through a fake runner
        LOGGER.error(
            "worker stopped unexpectedly error_type=%s",
            type(error).__name__,
        )
        return WorkerExitCode.UNEXPECTED_ERROR

    LOGGER.info("worker stopped")
    return WorkerExitCode.SUCCESS


def main() -> int:
    """Console-script entry point."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    return int(run_cli())


__all__ = [
    "LoggingConfiguratorProtocol",
    "RuntimeFactoryProtocol",
    "WorkerExitCode",
    "configure_worker_logging",
    "main",
    "run_application",
    "run_cli",
]
