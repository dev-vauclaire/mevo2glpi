"""Dependency composition for the retention worker use case."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from uuid import UUID

import httpx
from sqlalchemy.orm import Session

from mevo2glpi_common.backoff import ExponentialBackoffPolicy
from mevo2glpi_glpi import GlpiClient

from .retention.lifecycle import RetentionJobLifecycleService
from .retention.policies import RetentionCheckPolicy
from .retention.service import RetentionWorker
from .retention.stages import CrashSafeAudioPurger, GlpiCheckStage
from .settings import RetentionWorkerSettings


def _no_op() -> None:
    return None


def _no_op_job_started(_job_uuid: UUID) -> None:
    return None


class RetentionWorkerConfigurationError(ValueError):
    """Report invalid runtime configuration without echoing its values."""


def build_retention_worker(
    settings: RetentionWorkerSettings,
    *,
    http_client: httpx.Client,
    session_factory: Callable[[], Session],
    ownership_guard: Callable[[], None] = _no_op,
    on_job_started: Callable[[UUID], None] = _no_op_job_started,
) -> RetentionWorker:
    """Compose one worker without taking ownership of runtime resources."""
    if not isinstance(settings, RetentionWorkerSettings):
        raise TypeError("settings must be RetentionWorkerSettings")
    if not isinstance(http_client, httpx.Client):
        raise TypeError("http_client must be an httpx.Client")
    if not callable(session_factory):
        raise TypeError("session_factory must be callable")
    if not callable(ownership_guard):
        raise TypeError("ownership_guard must be callable")
    if not callable(on_job_started):
        raise TypeError("on_job_started must be callable")

    audio_root = _resolve_existing_directory(settings.audio_root)
    check_policy = RetentionCheckPolicy(
        check_interval=timedelta(hours=settings.retention_check_interval_hours)
    )
    lifecycle = RetentionJobLifecycleService(
        session_factory=session_factory,
        check_policy=check_policy,
        ownership_guard=ownership_guard,
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
    return RetentionWorker(
        lifecycle=lifecycle,
        glpi_check_stage=GlpiCheckStage(
            glpi_client,
            ownership_guard=ownership_guard,
        ),
        audio_purger=CrashSafeAudioPurger(
            audio_root,
            ownership_guard=ownership_guard,
        ),
        retry_policy=ExponentialBackoffPolicy(
            initial_seconds=settings.retention_retry_initial_seconds,
            maximum_seconds=settings.retention_retry_max_seconds,
        ),
        check_policy=check_policy,
        poll_interval_seconds=settings.retention_poll_interval_seconds,
        system_probe_interval_seconds=(
            settings.retention_system_probe_interval_seconds
        ),
        on_job_started=on_job_started,
    )


def _resolve_existing_directory(directory: object) -> Path:
    if not isinstance(directory, Path):
        raise RetentionWorkerConfigurationError("audio root configuration is invalid")
    expanded = directory.expanduser()
    try:
        if expanded.is_symlink():
            raise RetentionWorkerConfigurationError(
                "audio root must not be a symbolic link"
            )
        resolved = expanded.resolve(strict=True)
    except RetentionWorkerConfigurationError:
        raise
    except (OSError, RuntimeError):
        raise RetentionWorkerConfigurationError(
            "audio root must exist and be accessible"
        ) from None
    if not resolved.is_dir():
        raise RetentionWorkerConfigurationError("audio root must be a directory")
    return resolved


__all__ = [
    "RetentionWorkerConfigurationError",
    "build_retention_worker",
]
