"""Executable lifecycle of the Transcribe AI staging simulator."""

from __future__ import annotations

import logging
from collections.abc import Callable
from enum import IntEnum
from typing import Protocol

import uvicorn
from pydantic import ValidationError

from .settings import TranscribeAiSimulatorSettings
from .web import create_app

LOGGER = logging.getLogger(__name__)


class TranscribeAiSimulatorExitCode(IntEnum):
    """Stable process exit codes for container orchestration."""

    SUCCESS = 0
    UNEXPECTED_ERROR = 1
    CONFIGURATION_ERROR = 2
    INTERRUPTED = 130


class _Server(Protocol):
    def run(self) -> None:
        """Serve until a standard shutdown signal is received."""
        ...


def build_server(settings: TranscribeAiSimulatorSettings) -> uvicorn.Server:
    """Compose one single-process server without HTTP access-body logging."""
    if not isinstance(settings, TranscribeAiSimulatorSettings):
        raise TypeError("settings must be a TranscribeAiSimulatorSettings")
    config = uvicorn.Config(
        create_app(settings),
        host=str(settings.transcribe_ai_simulator_host),
        port=settings.transcribe_ai_simulator_port,
        log_level="info",
        access_log=False,
        proxy_headers=False,
    )
    return uvicorn.Server(config)


def run_cli(
    *,
    settings_factory: Callable[[], TranscribeAiSimulatorSettings] = (
        TranscribeAiSimulatorSettings
    ),
    server_factory: Callable[[TranscribeAiSimulatorSettings], _Server] = build_server,
) -> TranscribeAiSimulatorExitCode:
    """Load sanitized settings and run the internal staging server."""
    try:
        settings = settings_factory()
    except ValidationError:
        LOGGER.error("Transcribe AI simulator configuration is invalid")
        return TranscribeAiSimulatorExitCode.CONFIGURATION_ERROR
    except Exception as error:  # pragma: no cover - defensive startup boundary
        LOGGER.error(
            "Transcribe AI simulator configuration loading failed error_type=%s",
            type(error).__name__,
        )
        return TranscribeAiSimulatorExitCode.UNEXPECTED_ERROR

    LOGGER.info("Transcribe AI simulator starting")
    try:
        server_factory(settings).run()
    except KeyboardInterrupt:
        LOGGER.info("Transcribe AI simulator interrupted")
        return TranscribeAiSimulatorExitCode.INTERRUPTED
    except Exception as error:  # pragma: no cover - defensive runtime boundary
        LOGGER.error(
            "Transcribe AI simulator stopped unexpectedly error_type=%s",
            type(error).__name__,
        )
        return TranscribeAiSimulatorExitCode.UNEXPECTED_ERROR
    LOGGER.info("Transcribe AI simulator stopped")
    return TranscribeAiSimulatorExitCode.SUCCESS


def main() -> int:
    """Console-script entry point."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    return int(run_cli())


__all__ = [
    "TranscribeAiSimulatorExitCode",
    "build_server",
    "main",
    "run_cli",
]
