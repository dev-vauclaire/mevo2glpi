"""Dependency composition for the Exchange publisher use case."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from uuid import UUID

import httpx2
from sqlalchemy.orm import Session

from glpi_ticketing_common.backoff import ExponentialBackoffPolicy

from .graph import (
    GraphClient,
    GraphTokenProviderProtocol,
    MsalCertificateTokenProvider,
    SyntheticGraphTokenProvider,
)
from .publisher import PublisherLifecycleService, PublisherWorker
from .settings import PublisherGraphAuthMode, PublisherSettings


def _no_op() -> None:
    return None


def _no_op_ingestion_started(_ingestion_uuid: UUID) -> None:
    return None


class PublisherConfigurationError(ValueError):
    """Report invalid runtime configuration without exposing configured values."""


def build_publisher_worker(
    settings: PublisherSettings,
    *,
    http_client: httpx2.Client,
    session_factory: Callable[[], Session],
    ownership_guard: Callable[[], None] = _no_op,
    on_ingestion_started: Callable[[UUID], None] = _no_op_ingestion_started,
) -> PublisherWorker:
    """Compose one publisher while leaving resource ownership to the runtime."""
    if not isinstance(settings, PublisherSettings):
        raise TypeError("settings must be PublisherSettings")
    if not callable(getattr(http_client, "request", None)) or not callable(
        getattr(http_client, "stream", None)
    ):
        raise TypeError("http_client must expose request and stream")
    if not callable(session_factory):
        raise TypeError("session_factory must be callable")
    if not callable(ownership_guard):
        raise TypeError("ownership_guard must be callable")
    if not callable(on_ingestion_started):
        raise TypeError("on_ingestion_started must be callable")

    audio_root = _resolve_existing_audio_root(settings.audio_root)
    token_provider = _build_graph_token_provider(settings)
    graph = GraphClient(
        mailbox_id=settings.publisher_mailbox_id,
        http_client=http_client,
        token_provider=token_provider,
        timeout=settings.publisher_http_timeout_seconds,
        base_url=str(settings.publisher_graph_base_url),
        page_size=settings.publisher_graph_page_size,
        max_attachment_bytes=settings.publisher_max_attachment_bytes,
    )
    lifecycle = PublisherLifecycleService(
        session_factory=session_factory,
        ownership_guard=ownership_guard,
    )
    return PublisherWorker(
        mailbox_id=settings.publisher_mailbox_id,
        graph=graph,
        lifecycle=lifecycle,
        audio_root=audio_root,
        retry_policy=ExponentialBackoffPolicy(
            initial_seconds=settings.publisher_retry_initial_seconds,
            maximum_seconds=settings.publisher_retry_max_seconds,
        ),
        poll_interval_seconds=settings.publisher_poll_interval_seconds,
        system_probe_interval_seconds=settings.publisher_system_probe_interval_seconds,
        maximum_attachment_bytes=settings.publisher_max_attachment_bytes,
        ownership_guard=ownership_guard,
        on_ingestion_started=on_ingestion_started,
    )


def _build_graph_token_provider(
    settings: PublisherSettings,
) -> GraphTokenProviderProtocol:
    if settings.publisher_graph_auth_mode is PublisherGraphAuthMode.SYNTHETIC:
        return SyntheticGraphTokenProvider()

    tenant_id = settings.publisher_entra_tenant_id
    client_id = settings.publisher_entra_client_id
    private_key_path = settings.publisher_entra_private_key_pem_path
    certificate_path = settings.publisher_entra_certificate_pem_path
    if (
        tenant_id is None
        or client_id is None
        or private_key_path is None
        or certificate_path is None
    ):
        raise PublisherConfigurationError(
            "certificate Graph authentication configuration is incomplete"
        )
    return MsalCertificateTokenProvider.from_pem(
        tenant_id=tenant_id,
        client_id=client_id,
        private_key_pem_path=private_key_path,
        certificate_pem_path=certificate_path,
        passphrase=settings.publisher_entra_private_key_passphrase,
        timeout_seconds=settings.publisher_http_timeout_seconds,
    )


def _resolve_existing_audio_root(directory: object) -> Path:
    if not isinstance(directory, Path):
        raise PublisherConfigurationError("audio root configuration is invalid")
    expanded = directory.expanduser()
    try:
        if expanded.is_symlink():
            raise PublisherConfigurationError("audio root must not be a symbolic link")
        resolved = expanded.resolve(strict=True)
    except PublisherConfigurationError:
        raise
    except (OSError, RuntimeError):
        raise PublisherConfigurationError(
            "audio root must exist and be accessible"
        ) from None
    if not resolved.is_dir():
        raise PublisherConfigurationError("audio root must be a directory")
    return resolved


__all__ = ["PublisherConfigurationError", "build_publisher_worker"]
