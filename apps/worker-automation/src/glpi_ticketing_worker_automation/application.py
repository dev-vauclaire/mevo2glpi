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
from .settings import WorkerSettings
from .worker import run_forever

LOGGER = logging.getLogger(__name__)


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

    stop_event = Event()
    LOGGER.info("worker starting")
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
    "RuntimeFactoryProtocol",
    "WorkerExitCode",
    "main",
    "run_application",
    "run_cli",
]
