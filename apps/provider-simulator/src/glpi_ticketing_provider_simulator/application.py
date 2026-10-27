"""Executable lifecycle of the staging provider simulator."""

from __future__ import annotations

import logging
from collections.abc import Callable
from enum import IntEnum
from typing import Protocol

import uvicorn
from pydantic import ValidationError

from .settings import ProviderSimulatorSettings
from .web import create_app

LOGGER = logging.getLogger(__name__)


class ProviderSimulatorExitCode(IntEnum):
    """Stable process exit codes for container orchestration."""

    SUCCESS = 0
    UNEXPECTED_ERROR = 1
    CONFIGURATION_ERROR = 2
    INTERRUPTED = 130


class _Server(Protocol):
    def run(self) -> None:
        """Serve until a standard shutdown signal is received."""
        ...


def build_server(settings: ProviderSimulatorSettings) -> uvicorn.Server:
    """Compose one single-process server without HTTP access-body logging."""
    if not isinstance(settings, ProviderSimulatorSettings):
        raise TypeError("settings must be a ProviderSimulatorSettings")
    config = uvicorn.Config(
        create_app(settings),
        host=str(settings.provider_simulator_host),
        port=settings.provider_simulator_port,
        log_level="info",
        access_log=False,
        proxy_headers=False,
    )
    return uvicorn.Server(config)


def run_cli(
    *,
    settings_factory: Callable[[], ProviderSimulatorSettings] = (
        ProviderSimulatorSettings
    ),
    server_factory: Callable[[ProviderSimulatorSettings], _Server] = build_server,
) -> ProviderSimulatorExitCode:
    """Load sanitized settings and run the internal staging server."""
    try:
        settings = settings_factory()
    except ValidationError:
        LOGGER.error("provider simulator configuration is invalid")
        return ProviderSimulatorExitCode.CONFIGURATION_ERROR
    except Exception as error:  # pragma: no cover - defensive startup boundary
        LOGGER.error(
            "provider simulator configuration loading failed error_type=%s",
            type(error).__name__,
        )
        return ProviderSimulatorExitCode.UNEXPECTED_ERROR

    LOGGER.info("provider simulator starting")
    try:
        server_factory(settings).run()
    except KeyboardInterrupt:
        LOGGER.info("provider simulator interrupted")
        return ProviderSimulatorExitCode.INTERRUPTED
    except Exception as error:  # pragma: no cover - defensive runtime boundary
        LOGGER.error(
            "provider simulator stopped unexpectedly error_type=%s",
            type(error).__name__,
        )
        return ProviderSimulatorExitCode.UNEXPECTED_ERROR
    LOGGER.info("provider simulator stopped")
    return ProviderSimulatorExitCode.SUCCESS


def main() -> int:
    """Console-script entry point."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    return int(run_cli())


__all__ = [
    "ProviderSimulatorExitCode",
    "build_server",
    "main",
    "run_cli",
]
