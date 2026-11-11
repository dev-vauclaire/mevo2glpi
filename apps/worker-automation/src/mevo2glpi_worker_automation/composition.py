"""Dependency composition for the automation worker use case."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from functools import partial
from pathlib import Path
from uuid import UUID

import httpx
from pydantic import HttpUrl
from sqlalchemy.orm import Session

from mevo2glpi_common import (
    OriginatingPhoneNumberCipher,
    OriginatingPhoneNumberKeyError,
)
from mevo2glpi_glpi import GlpiClient, GlpiTicketDefaults

from .settings import WorkerSettings
from .title_generation import DefaultTitleGenerator
from .transcription import (
    OpenAICompatibleTranscriber,
    TranscriberProtocol,
)
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


def _no_op() -> None:
    return None


def _no_op_job_started(_audio_uuid: UUID) -> None:
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
    ownership_guard: Callable[[], None] = _no_op,
    on_job_started: Callable[[UUID], None] = _no_op_job_started,
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
    if not callable(ownership_guard):
        raise TypeError("ownership_guard must be callable")
    if not callable(on_job_started):
        raise TypeError("on_job_started must be callable")

    audio_root = _resolve_existing_directory(settings.audio_root)
    try:
        originating_phone_number_cipher = (
            OriginatingPhoneNumberCipher.from_base64_key_file(
                settings.originating_phone_number_key_path
            )
        )
    except OriginatingPhoneNumberKeyError:
        raise WorkerConfigurationError(
            "originating phone number key configuration is invalid"
        ) from None
    lifecycle = AutomationJobLifecycleService(
        session_factory=session_factory,
        ownership_guard=ownership_guard,
        originating_phone_number_cipher=originating_phone_number_cipher,
    )

    transcriber = _build_transcriber(
        settings,
        http_client=http_client,
    )
    glpi_client = GlpiClient(
        base_url=str(settings.glpi_base_url),
        username=settings.glpi_username,
        password=settings.glpi_password,
        client_id=settings.glpi_client_id,
        client_secret=settings.glpi_client_secret,
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
            hours=settings.retention_check_interval_hours
        ),
    )
    transitions = JobTransitionService(
        lifecycle=lifecycle,
        retry_policy=retry_policy,
        retention_policy=retention_policy,
        maximum_consecutive_retries=settings.worker_max_consecutive_retries,
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
            ticket_defaults=GlpiTicketDefaults(
                title=settings.glpi_ticket_title,
                ticket_type=settings.glpi_ticket_type,
                request_source_id=settings.glpi_ticket_request_source_id,
                urgency=settings.glpi_ticket_urgency,
                impact=settings.glpi_ticket_impact,
            ),
            title_generator=DefaultTitleGenerator(settings.glpi_ticket_title),
            ownership_guard=ownership_guard,
        ),
        transitions=transitions,
        idle_wait_seconds=settings.worker_idle_interval_seconds,
        on_job_started=on_job_started,
    )


def _build_transcriber(
    settings: WorkerSettings,
    *,
    http_client: httpx.Client,
) -> TranscriberProtocol:
    """Build the only supported synchronous transcription adapter."""
    base_url = settings.openai_compatible_transcription_base_url
    api_key = settings.openai_compatible_transcription_api_key
    model = settings.openai_compatible_transcription_model
    if base_url is None or api_key is None or model is None:
        raise WorkerConfigurationError(
            "OpenAI-compatible transcription provider configuration is incomplete"
        )
    return OpenAICompatibleTranscriber(
        base_url=str(base_url),
        api_key=api_key,
        model=model,
        language=settings.openai_compatible_transcription_language,
        http_client=http_client,
        timeout=settings.transcription_http_timeout_seconds,
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
