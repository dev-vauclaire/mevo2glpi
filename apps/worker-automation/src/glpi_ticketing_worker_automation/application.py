"""Composition root and executable lifecycle for the automation worker."""

from __future__ import annotations

import logging
import signal
import time
from collections.abc import Callable, Generator, Iterator
from contextlib import AbstractContextManager, contextmanager
from enum import IntEnum
from functools import partial
from pathlib import Path
from threading import Event
from types import FrameType
from typing import Protocol
from uuid import UUID

import httpx
from pydantic import HttpUrl, ValidationError

from .glpi import GlpiClient, GlpiTicketDefaults
from .settings import WorkerSettings
from .transcription import TranscriptionClient, TranscriptionPoller
from .worker import (
    AudioProcessor,
    WorkerBlockedError,
    WorkerDirectories,
    run_forever,
)

LOGGER = logging.getLogger(__name__)


def _never_stop() -> bool:
    return False


class WorkerExitCode(IntEnum):
    """Stable process exit codes exposed to the container runtime."""

    SUCCESS = 0
    UNEXPECTED_ERROR = 1
    CONFIGURATION_ERROR = 2
    BLOCKED = 3
    INTERRUPTED = 130


class WorkerConfigurationError(ValueError):
    """Report an invalid runtime configuration without echoing its values."""


class _RuntimeFactory(Protocol):
    def __call__(
        self,
        settings: WorkerSettings,
        *,
        transcription_sleep: Callable[[float], None],
        transcription_should_stop: Callable[[], bool],
    ) -> AbstractContextManager[AudioProcessor]:
        """Return a context owning one fully composed processor."""
        ...


def build_public_audio_url(public_base_url: HttpUrl, audio_uuid: UUID) -> str:
    """Build the stable public audio route stored in a GLPI ticket."""
    if not isinstance(audio_uuid, UUID):
        raise TypeError("audio_uuid must be a UUID")
    return f"{str(public_base_url).rstrip('/')}/audios/{audio_uuid}"


def build_processor(
    settings: WorkerSettings,
    *,
    http_client: httpx.Client,
    transcription_sleep: Callable[[float], None] = time.sleep,
    transcription_should_stop: Callable[[], bool] = _never_stop,
) -> AudioProcessor:
    """Compose an ``AudioProcessor`` without starting its scheduling loop.

    The caller owns ``http_client``. This separation lets tests and one-shot tools
    exercise the real processor while controlling the surrounding resources.
    """
    if not isinstance(settings, WorkerSettings):
        raise TypeError("settings must be WorkerSettings")
    if not isinstance(http_client, httpx.Client):
        raise TypeError("http_client must be an httpx.Client")
    if not callable(transcription_sleep):
        raise TypeError("transcription_sleep must be callable")
    if not callable(transcription_should_stop):
        raise TypeError("transcription_should_stop must be callable")

    directories = _resolve_worker_directories(settings)
    transcription_client = TranscriptionClient(
        base_url=str(settings.legacy_transcription_base_url),
        api_key=settings.legacy_transcription_api_key,
        http_client=http_client,
        timeout=settings.transcription_http_timeout_seconds,
    )
    transcriber = TranscriptionPoller(
        client=transcription_client,
        poll_interval_seconds=settings.transcription_poll_interval_seconds,
        job_timeout_seconds=settings.transcription_job_timeout_seconds,
        sleep=transcription_sleep,
        should_stop=transcription_should_stop,
    )
    glpi_client = GlpiClient(
        base_url=str(settings.glpi_base_url),
        username=settings.glpi_username,
        password=settings.glpi_password,
        client_id=settings.glpi_client_id,
        client_secret=settings.glpi_client_secret,
        ticket_defaults=GlpiTicketDefaults(
            title=settings.glpi_ticket_title,
            ticket_type=settings.glpi_ticket_type,
            urgency=settings.glpi_ticket_urgency,
            impact=settings.glpi_ticket_impact,
        ),
        http_client=http_client,
        timeout=settings.glpi_http_timeout_seconds,
    )
    audio_url_for = partial(build_public_audio_url, settings.public_base_url)

    return AudioProcessor(
        directories=directories,
        transcriber=transcriber,
        glpi_client=glpi_client,
        audio_url_for=audio_url_for,
        idle_wait_seconds=settings.worker_scan_interval_seconds,
        transcription_retry_interval_seconds=(
            settings.transcription_poll_interval_seconds
        ),
        glpi_retry_interval_seconds=settings.glpi_retry_interval_seconds,
    )


@contextmanager
def worker_runtime(
    settings: WorkerSettings,
    *,
    http_client_factory: Callable[[], httpx.Client] = httpx.Client,
    transcription_sleep: Callable[[float], None] = time.sleep,
    transcription_should_stop: Callable[[], bool] = _never_stop,
) -> Iterator[AudioProcessor]:
    """Own the shared HTTP client and expose one composed processor."""
    http_client = http_client_factory()
    try:
        yield build_processor(
            settings,
            http_client=http_client,
            transcription_sleep=transcription_sleep,
            transcription_should_stop=transcription_should_stop,
        )
    finally:
        http_client.close()


def run_application(
    settings: WorkerSettings,
    stop_event: Event,
    *,
    runtime_factory: _RuntimeFactory = worker_runtime,
) -> None:
    """Run a composed processor until shutdown or an explicit block."""

    def wait_for_work(seconds: float) -> None:
        stop_event.wait(seconds)

    with runtime_factory(
        settings,
        transcription_sleep=wait_for_work,
        transcription_should_stop=stop_event.is_set,
    ) as processor:
        run_forever(
            processor,
            sleep=wait_for_work,
            should_stop=stop_event.is_set,
        )


@contextmanager
def shutdown_signal_handlers(stop_event: Event) -> Generator[None]:
    """Turn SIGINT and SIGTERM into a successful cooperative shutdown.

    Managed signals therefore lead to exit code 0. A raw ``KeyboardInterrupt``
    escaping outside these handlers retains the conventional exit code 130.
    """

    def request_shutdown(_signal_number: int, _frame: FrameType | None) -> None:
        stop_event.set()

    handled_signals = (signal.SIGINT, signal.SIGTERM)
    previous_handlers = {
        handled_signal: signal.signal(handled_signal, request_shutdown)
        for handled_signal in handled_signals
    }
    try:
        yield
    finally:
        for handled_signal, previous_handler in previous_handlers.items():
            signal.signal(handled_signal, previous_handler)


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
    except WorkerBlockedError as error:
        outcome = error.outcome
        LOGGER.error(
            "worker stopped because progress is blocked "
            "audio_uuid=%s stage=%s category=%s code=%s",
            outcome.audio_uuid,
            outcome.stage.value,
            outcome.failure.category,
            outcome.failure.technical_code,
        )
        return WorkerExitCode.BLOCKED
    except KeyboardInterrupt:
        LOGGER.info("worker interrupted")
        return WorkerExitCode.INTERRUPTED
    except Exception as error:  # pragma: no cover - exercised through a fake runner
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


def _resolve_worker_directories(settings: WorkerSettings) -> WorkerDirectories:
    resolved_directories = {
        field_name: _resolve_existing_directory(getattr(settings, settings_name))
        for field_name, settings_name in (
            ("pending", "audio_pending_dir"),
            ("processing", "audio_processing_dir"),
            ("completed", "audio_completed_dir"),
            ("failed", "audio_failed_dir"),
        )
    }
    try:
        return WorkerDirectories(**resolved_directories)
    except (TypeError, ValueError):
        raise WorkerConfigurationError(
            "audio directories must resolve to distinct directories"
        ) from None


def _resolve_existing_directory(directory: object) -> Path:
    if not isinstance(directory, Path):
        raise WorkerConfigurationError("audio directory configuration is invalid")
    try:
        resolved_directory = directory.expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        raise WorkerConfigurationError(
            "audio directories must exist and be accessible"
        ) from None
    if not resolved_directory.is_dir():
        raise WorkerConfigurationError("audio paths must be directories")
    return resolved_directory


__all__ = [
    "WorkerExitCode",
    "WorkerConfigurationError",
    "build_processor",
    "build_public_audio_url",
    "main",
    "run_application",
    "run_cli",
    "shutdown_signal_handlers",
    "worker_runtime",
]
