"""Executable lifecycle of the Microsoft Graph staging simulator."""

from __future__ import annotations

import logging
from collections.abc import Callable
from enum import IntEnum
from typing import Protocol

import uvicorn
from pydantic import ValidationError

from .settings import MicrosoftGraphSimulatorSettings
from .web import create_app

LOGGER = logging.getLogger(__name__)


class MicrosoftGraphSimulatorExitCode(IntEnum):
    """Stable process exit codes for container orchestration."""

    SUCCESS = 0
    UNEXPECTED_ERROR = 1
    CONFIGURATION_ERROR = 2
    INTERRUPTED = 130


class _Server(Protocol):
    def run(self) -> None:
        """Serve until a standard shutdown signal is received."""
        ...


def build_server(settings: MicrosoftGraphSimulatorSettings) -> uvicorn.Server:
    """Compose one single-process server without HTTP access-body logging."""
    if not isinstance(settings, MicrosoftGraphSimulatorSettings):
        raise TypeError("settings must be a MicrosoftGraphSimulatorSettings")
    config = uvicorn.Config(
        create_app(settings),
        host=str(settings.microsoft_graph_simulator_host),
        port=settings.microsoft_graph_simulator_port,
        log_level="info",
        access_log=False,
        proxy_headers=False,
    )
    return uvicorn.Server(config)


def run_cli(
    *,
    settings_factory: Callable[[], MicrosoftGraphSimulatorSettings] = (
        MicrosoftGraphSimulatorSettings
    ),
    server_factory: Callable[[MicrosoftGraphSimulatorSettings], _Server] = build_server,
) -> MicrosoftGraphSimulatorExitCode:
    """Load sanitized settings and run the internal staging server."""
    try:
        settings = settings_factory()
    except ValidationError:
        LOGGER.error("Microsoft Graph simulator configuration is invalid")
        return MicrosoftGraphSimulatorExitCode.CONFIGURATION_ERROR
    except Exception as error:  # pragma: no cover - defensive startup boundary
        LOGGER.error(
            "Microsoft Graph simulator configuration loading failed error_type=%s",
            type(error).__name__,
        )
        return MicrosoftGraphSimulatorExitCode.UNEXPECTED_ERROR

    LOGGER.info("Microsoft Graph simulator starting")
    try:
        server_factory(settings).run()
    except KeyboardInterrupt:
        LOGGER.info("Microsoft Graph simulator interrupted")
        return MicrosoftGraphSimulatorExitCode.INTERRUPTED
    except Exception as error:  # pragma: no cover - defensive runtime boundary
        LOGGER.error(
            "Microsoft Graph simulator stopped unexpectedly error_type=%s",
            type(error).__name__,
        )
        return MicrosoftGraphSimulatorExitCode.UNEXPECTED_ERROR
    LOGGER.info("Microsoft Graph simulator stopped")
    return MicrosoftGraphSimulatorExitCode.SUCCESS


def main() -> int:
    """Console-script entry point."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    return int(run_cli())


__all__ = [
    "MicrosoftGraphSimulatorExitCode",
    "build_server",
    "main",
    "run_cli",
]
