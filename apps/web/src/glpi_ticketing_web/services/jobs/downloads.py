"""Controlled access to completed canonical audio files."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol
from uuid import UUID

from glpi_ticketing_common.audio_files import (
    InvalidAudioStorageKeyError,
    build_audio_storage_key,
    resolve_audio_storage_path,
)
from glpi_ticketing_database import JobStatus, RetentionStatus

from ._audio_storage import open_audio_stream as _open_audio_stream
from .models import (
    AudioPurgedError,
    AudioStorageInvariantError,
    AudioStorageUnavailableError,
    DownloadableAudio,
    JobView,
)


class _AudioJobReader(Protocol):
    def execute(self, audio_uuid: UUID) -> JobView | None:
        """Return one sanitized job view."""


class GetDownloadableAudio:
    """Open a completed audio through its canonical UUID-based storage key."""

    def __init__(self, audio_root: Path, get_audio_job: _AudioJobReader) -> None:
        resolved_audio_root = Path(audio_root).expanduser().resolve(strict=True)
        if not resolved_audio_root.is_dir():
            raise NotADirectoryError(resolved_audio_root)
        self._audio_root = resolved_audio_root
        self._get_audio_job = get_audio_job

    def execute(self, audio_uuid: UUID) -> DownloadableAudio | None:
        """Return completed bytes, distinguish purged and inconsistent state."""
        job = self._get_audio_job.execute(audio_uuid)
        if job is None or job.state is not JobStatus.COMPLETED:
            return None
        if job.retention_status in {
            RetentionStatus.DELETING,
            RetentionStatus.DELETED,
        }:
            raise AudioPurgedError("Audio has been purged")
        if job.retention_status is None:
            raise AudioStorageInvariantError(
                "Completed audio has no retention lifecycle"
            )

        try:
            storage_key = build_audio_storage_key(
                job.audio_uuid,
                job.audio_extension.value,
            )
            audio_path = resolve_audio_storage_path(self._audio_root, storage_key)
            audio_stream, size_bytes = _open_audio_stream(audio_path)
        except (FileNotFoundError, InvalidAudioStorageKeyError) as error:
            raise AudioStorageInvariantError(
                "Audio storage invariant violated"
            ) from error
        except OSError as error:
            raise AudioStorageUnavailableError(
                "Audio storage is temporarily unavailable"
            ) from error

        if size_bytes != job.audio_size_bytes:
            audio_stream.close()
            raise AudioStorageInvariantError(
                "Audio size does not match persisted metadata"
            )
        return DownloadableAudio(
            uuid=job.audio_uuid,
            extension=job.audio_extension,
            size_bytes=size_bytes,
            _stream=audio_stream,
        )


__all__ = ["GetDownloadableAudio"]
