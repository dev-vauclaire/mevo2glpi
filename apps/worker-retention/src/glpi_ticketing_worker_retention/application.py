"""Executable lifecycle of the retention worker application."""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractContextManager
from enum import IntEnum
from threading import Event
from typing import Protocol

from pydantic import ValidationError

from .composition import RetentionWorkerConfigurationError
from .retention.runner import run_forever
from .runtime import (
    RetentionWorkerAlreadyRunningError,
    RuntimeRetentionWorkerProtocol,
    retention_worker_runtime,
    shutdown_signal_handlers,
)
from .settings import RetentionWorkerSettings

LOGGER = logging.getLogger(__name__)


class RetentionWorkerExitCode(IntEnum):
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
        settings: RetentionWorkerSettings,
        *,
        wait_once: Callable[[float], object],
        should_stop: Callable[[], bool],
    ) -> AbstractContextManager[RuntimeRetentionWorkerProtocol]: ...


def run_application(
    settings: RetentionWorkerSettings,
    stop_event: Event,
    *,
    runtime_factory: RuntimeFactoryProtocol = retention_worker_runtime,
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
    settings_factory: Callable[[], RetentionWorkerSettings] = (RetentionWorkerSettings),
    application_runner: Callable[
        [RetentionWorkerSettings, Event], None
    ] = run_application,
    signal_context_factory: Callable[
        [Event], AbstractContextManager[None]
    ] = shutdown_signal_handlers,
) -> RetentionWorkerExitCode:
    """Load configuration and run the worker with sanitized diagnostics."""
    try:
        settings = settings_factory()
    except ValidationError:
        LOGGER.error("retention worker configuration is invalid")
        return RetentionWorkerExitCode.CONFIGURATION_ERROR
    except Exception as error:  # pragma: no cover - defensive startup boundary
        LOGGER.error(
            "retention worker configuration loading failed error_type=%s",
            type(error).__name__,
        )
        return RetentionWorkerExitCode.UNEXPECTED_ERROR

    stop_event = Event()
    release_identity = settings.release_identity
    LOGGER.info(
        "retention worker starting app_environment=%s app_release=%s app_commit_sha=%s",
        release_identity.environment.value,
        release_identity.release,
        release_identity.commit_sha,
    )
    try:
        with signal_context_factory(stop_event):
            application_runner(settings, stop_event)
    except RetentionWorkerConfigurationError:
        LOGGER.error("retention worker runtime configuration is invalid")
        return RetentionWorkerExitCode.CONFIGURATION_ERROR
    except RetentionWorkerAlreadyRunningError:
        LOGGER.error("retention worker singleton lock is already held")
        return RetentionWorkerExitCode.SINGLETON_CONFLICT
    except KeyboardInterrupt:
        LOGGER.info("retention worker interrupted")
        return RetentionWorkerExitCode.INTERRUPTED
    except Exception as error:  # pragma: no cover - defensive process boundary
        LOGGER.error(
            "retention worker stopped unexpectedly error_type=%s",
            type(error).__name__,
        )
        return RetentionWorkerExitCode.UNEXPECTED_ERROR

    LOGGER.info("retention worker stopped")
    return RetentionWorkerExitCode.SUCCESS


def main() -> int:
    """Console-script entry point."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    return int(run_cli())


__all__ = [
    "RetentionWorkerExitCode",
    "RuntimeFactoryProtocol",
    "main",
    "run_application",
    "run_cli",
]
