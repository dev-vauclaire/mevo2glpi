"""Composition root and executable lifecycle for the web application."""

from __future__ import annotations

import logging
from collections.abc import Callable
from enum import IntEnum
from typing import Protocol

import uvicorn
from fastapi import FastAPI
from pydantic import ValidationError

from glpi_ticketing_database import (
    DatabaseConfigurationError,
    create_database_engine,
    create_session_factory,
)

from .services import GetAudioJob, GetDownloadableAudio, SubmitAudioJob
from .settings import WebSettings
from .web import create_app

LOGGER = logging.getLogger(__name__)


class WebExitCode(IntEnum):
    """Stable process exit codes exposed to the container runtime."""

    SUCCESS = 0
    UNEXPECTED_ERROR = 1
    CONFIGURATION_ERROR = 2
    INTERRUPTED = 130


class WebConfigurationError(ValueError):
    """Report invalid runtime configuration without exposing its values."""


class _Server(Protocol):
    def run(self) -> None:
        """Serve until Uvicorn handles a standard shutdown signal."""
        ...


class _ServerFactory(Protocol):
    def __call__(self, settings: WebSettings) -> _Server:
        """Return a configured web server."""
        ...


def build_application(settings: WebSettings) -> FastAPI:
    """Compose FastAPI, canonical storage and lazy PostgreSQL connections."""
    if not isinstance(settings, WebSettings):
        raise TypeError("settings must be WebSettings")

    engine = None
    try:
        audio_root = settings.audio_root.expanduser().resolve(strict=True)
        if not audio_root.is_dir():
            raise NotADirectoryError(audio_root)
        engine = create_database_engine(
            settings.database_url.get_secret_value(),
            connect_timeout_seconds=5,
        )
        session_factory = create_session_factory(engine)
        get_audio_job = GetAudioJob(session_factory)
        return create_app(
            audio_root=audio_root,
            submit_audio_job=SubmitAudioJob(
                audio_root=audio_root,
                session_factory=session_factory,
                max_upload_bytes=settings.max_upload_bytes,
            ),
            get_audio_job=get_audio_job,
            get_downloadable_audio=GetDownloadableAudio(
                audio_root,
                get_audio_job,
            ),
            max_upload_bytes=settings.max_upload_bytes,
            shutdown_callback=engine.dispose,
        )
    except (DatabaseConfigurationError, OSError, RuntimeError, ValueError):
        if engine is not None:
            engine.dispose()
        raise WebConfigurationError(
            "web database or audio storage configuration is invalid"
        ) from None


def build_server(settings: WebSettings) -> uvicorn.Server:
    """Create the single-process Uvicorn server used by the POC."""
    application = build_application(settings)
    config = uvicorn.Config(
        application,
        host=settings.web_host,
        port=settings.web_port,
        log_level="info",
        proxy_headers=settings.web_trust_proxy_headers,
        forwarded_allow_ips="*" if settings.web_trust_proxy_headers else [],
    )
    return uvicorn.Server(config)


def run_application(
    settings: WebSettings,
    *,
    server_factory: _ServerFactory = build_server,
) -> None:
    """Run Uvicorn until its standard SIGINT or SIGTERM shutdown."""
    server_factory(settings).run()


def run_cli(
    *,
    settings_factory: Callable[[], WebSettings] = WebSettings,
    application_runner: Callable[[WebSettings], None] = run_application,
) -> WebExitCode:
    """Load configuration and run the web server with sanitized diagnostics."""
    try:
        settings = settings_factory()
    except ValidationError:
        LOGGER.error("web configuration is invalid")
        return WebExitCode.CONFIGURATION_ERROR
    except Exception as error:  # pragma: no cover - defensive startup boundary
        LOGGER.error(
            "web configuration loading failed error_type=%s",
            type(error).__name__,
        )
        return WebExitCode.UNEXPECTED_ERROR

    LOGGER.info("web server starting")
    try:
        application_runner(settings)
    except WebConfigurationError:
        LOGGER.error("web runtime configuration is invalid")
        return WebExitCode.CONFIGURATION_ERROR
    except KeyboardInterrupt:
        LOGGER.info("web server interrupted")
        return WebExitCode.INTERRUPTED
    except Exception as error:  # pragma: no cover - exercised through a fake runner
        LOGGER.error(
            "web server stopped unexpectedly error_type=%s",
            type(error).__name__,
        )
        return WebExitCode.UNEXPECTED_ERROR

    LOGGER.info("web server stopped")
    return WebExitCode.SUCCESS


def main() -> int:
    """Console-script entry point."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    return int(run_cli())


__all__ = [
    "WebConfigurationError",
    "WebExitCode",
    "build_application",
    "build_server",
    "main",
    "run_application",
    "run_cli",
]
