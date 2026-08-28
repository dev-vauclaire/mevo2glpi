"""Dependency composition for the automation worker use case."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from uuid import UUID

import httpx
from pydantic import HttpUrl
from sqlalchemy.orm import Session

from .glpi import GlpiClient, GlpiTicketDefaults
from .settings import WorkerSettings
from .transcription import TranscriptionClient, TranscriptionPoller
from .worker import (
    AudioInspector,
    AutomationJobLifecycleService,
    AutomationWorker,
    JobTransitionService,
    RetentionSchedulePolicy,
    RetryBackoffPolicy,
    TicketCreationStage,
    TranscriptionStage,
)


def _never_stop() -> bool:
    return False


def _no_op() -> None:
    return None


class WorkerConfigurationError(ValueError):
    """Report an invalid runtime configuration without echoing its values."""


def build_public_audio_url(public_base_url: HttpUrl, audio_uuid: UUID) -> str:
    """Build the stable public audio route stored in a GLPI ticket."""
    if not isinstance(audio_uuid, UUID):
        raise TypeError("audio_uuid must be a UUID")
    return f"{str(public_base_url).rstrip('/')}/audios/{audio_uuid}"


def build_automation_worker(
    settings: WorkerSettings,
    *,
    http_client: httpx.Client,
    session_factory: Callable[[], Session],
    transcription_sleep: Callable[[float], None] = time.sleep,
    transcription_should_stop: Callable[[], bool] = _never_stop,
    ownership_guard: Callable[[], None] = _no_op,
) -> AutomationWorker:
    """Compose the worker use case without owning runtime resources.

    The caller retains ownership of the HTTP client and database session
    factory. Keeping resource ownership outside this function makes both the
    composition and the process lifecycle independently testable.
    """
    if not isinstance(settings, WorkerSettings):
        raise TypeError("settings must be WorkerSettings")
    if not isinstance(http_client, httpx.Client):
        raise TypeError("http_client must be an httpx.Client")
    if not callable(session_factory):
        raise TypeError("session_factory must be callable")
    if not callable(transcription_sleep):
        raise TypeError("transcription_sleep must be callable")
    if not callable(transcription_should_stop):
        raise TypeError("transcription_should_stop must be callable")
    if not callable(ownership_guard):
        raise TypeError("ownership_guard must be callable")

    audio_root = _resolve_existing_directory(settings.audio_root)
    lifecycle = AutomationJobLifecycleService(
        session_factory=session_factory,
        ownership_guard=ownership_guard,
    )

    def record_transcription_submission(
        audio_uuid: UUID,
        provider_job_id: str,
    ) -> None:
        lifecycle.record_transcription_submitted(
            audio_uuid,
            provider_job_id,
            datetime.now(UTC),
        )

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
        on_job_submitted=record_transcription_submission,
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
        ownership_guard=ownership_guard,
    )
    retry_policy = RetryBackoffPolicy(
        initial_seconds=settings.worker_retry_initial_seconds,
        maximum_seconds=settings.worker_retry_max_seconds,
    )
    retention_policy = RetentionSchedulePolicy(
        completed_ttl=timedelta(days=settings.completed_retention_days),
        failed_ttl=timedelta(days=settings.failed_retention_days),
        blocked_ttl=timedelta(days=settings.blocked_retention_days),
        first_completed_check_after=timedelta(
            hours=settings.retention_first_check_hours
        ),
    )
    transitions = JobTransitionService(
        lifecycle=lifecycle,
        retry_policy=retry_policy,
        retention_policy=retention_policy,
        system_probe_interval_seconds=settings.worker_system_probe_interval_seconds,
    )

    return AutomationWorker(
        lifecycle=lifecycle,
        audio_inspector=AudioInspector(audio_root),
        transcription_stage=TranscriptionStage(
            transcriber=transcriber,
            lifecycle=lifecycle,
            transitions=transitions,
            ownership_guard=ownership_guard,
        ),
        ticket_creation_stage=TicketCreationStage(
            glpi_client=glpi_client,
            lifecycle=lifecycle,
            transitions=transitions,
            audio_url_for=partial(build_public_audio_url, settings.public_base_url),
            ownership_guard=ownership_guard,
        ),
        transitions=transitions,
        idle_wait_seconds=settings.worker_idle_interval_seconds,
    )


def _resolve_existing_directory(directory: object) -> Path:
    if not isinstance(directory, Path):
        raise WorkerConfigurationError("audio root configuration is invalid")
    try:
        resolved_directory = directory.expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        raise WorkerConfigurationError(
            "audio root must exist and be accessible"
        ) from None
    if not resolved_directory.is_dir():
        raise WorkerConfigurationError("audio root must be a directory")
    return resolved_directory


__all__ = [
    "WorkerConfigurationError",
    "build_automation_worker",
    "build_public_audio_url",
]
