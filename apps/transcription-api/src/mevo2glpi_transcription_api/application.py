"""Composition root and executable lifecycle for the transcription API."""

from __future__ import annotations

import logging
from collections.abc import Callable
from enum import IntEnum
from typing import Protocol

import uvicorn
from fastapi import FastAPI
from pydantic import ValidationError

from .api import create_app
from .audio import FFmpegNormalizer
from .engines import TranscribeCppEngine, TranscriptionEngine
from .settings import TranscriptionSettings

LOGGER = logging.getLogger(__name__)


class TranscriptionApiExitCode(IntEnum):
    SUCCESS = 0
    UNEXPECTED_ERROR = 1
    CONFIGURATION_ERROR = 2
    INTERRUPTED = 130


class _Server(Protocol):
    def run(self) -> None: ...


class _ServerFactory(Protocol):
    def __call__(self, settings: TranscriptionSettings) -> _Server: ...


def build_application(
    settings: TranscriptionSettings,
    *,
    engine: TranscriptionEngine | None = None,
) -> FastAPI:
    """Compose the HTTP application without loading the model prematurely."""
    if not isinstance(settings, TranscriptionSettings):
        raise TypeError("settings must be TranscriptionSettings")
    inference_engine = engine or TranscribeCppEngine(
        model_path=settings.transcription_model_path,
        cpu_threads=settings.transcription_cpu_threads,
    )
    normalizer = FFmpegNormalizer(
        executable=settings.transcription_ffmpeg_path,
        timeout_seconds=settings.transcription_ffmpeg_timeout_seconds,
        max_audio_seconds=settings.transcription_max_audio_seconds,
    )
    return create_app(
        engine=inference_engine,
        normalizer=normalizer,
        api_key=settings.transcription_api_key,
        model_name=settings.transcription_model_name,
        max_upload_bytes=settings.transcription_max_upload_bytes,
    )


def build_server(settings: TranscriptionSettings) -> uvicorn.Server:
    config = uvicorn.Config(
        build_application(settings),
        host=settings.transcription_host,
        port=settings.transcription_port,
        log_level=settings.transcription_log_level,
        proxy_headers=False,
        forwarded_allow_ips=[],
    )
    return uvicorn.Server(config)


def run_application(
    settings: TranscriptionSettings,
    *,
    server_factory: _ServerFactory = build_server,
) -> None:
    server_factory(settings).run()


def run_cli(
    *,
    settings_factory: Callable[[], TranscriptionSettings] = TranscriptionSettings,
    application_runner: Callable[[TranscriptionSettings], None] = run_application,
) -> TranscriptionApiExitCode:
    try:
        settings = settings_factory()
    except ValidationError:
        LOGGER.error("transcription API configuration is invalid")
        return TranscriptionApiExitCode.CONFIGURATION_ERROR
    except Exception as error:  # pragma: no cover - defensive startup boundary
        LOGGER.error(
            "transcription API configuration loading failed error_type=%s",
            type(error).__name__,
        )
        return TranscriptionApiExitCode.UNEXPECTED_ERROR

    logging.getLogger("mevo2glpi_transcription_api").setLevel(
        settings.transcription_log_level.upper()
    )
    LOGGER.info(
        "transcription API starting model=%s cpu_threads=%s",
        settings.transcription_model_name,
        settings.transcription_cpu_threads,
    )
    try:
        application_runner(settings)
    except KeyboardInterrupt:
        LOGGER.info("transcription API interrupted")
        return TranscriptionApiExitCode.INTERRUPTED
    except Exception as error:  # pragma: no cover - defensive runtime boundary
        LOGGER.error(
            "transcription API stopped unexpectedly error_type=%s",
            type(error).__name__,
        )
        return TranscriptionApiExitCode.UNEXPECTED_ERROR
    LOGGER.info("transcription API stopped")
    return TranscriptionApiExitCode.SUCCESS


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    return int(run_cli())


__all__ = [
    "TranscriptionApiExitCode",
    "build_application",
    "build_server",
    "main",
    "run_application",
    "run_cli",
]
