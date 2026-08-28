"""Canonical audio inspection without database or remote side effects."""

from __future__ import annotations

from pathlib import Path

from glpi_ticketing_common.audio_files import (
    InvalidAudioStorageKeyError,
    resolve_audio_storage_path,
)
from glpi_ticketing_database import ErrorScope

from .outcomes import FailureDetails
from .ports import JobSnapshotProtocol

type AudioInspectionResult = Path | FailureDetails


class AudioInspector:
    """Resolve and validate the immutable audio referenced by one job."""

    def __init__(self, audio_root: Path) -> None:
        if not isinstance(audio_root, Path):
            raise TypeError("audio_root must be a Path")
        self._audio_root = audio_root

    def inspect(self, job: JobSnapshotProtocol) -> AudioInspectionResult:
        """Return the canonical path or a sanitized storage failure."""

        try:
            audio_path = resolve_audio_storage_path(
                self._audio_root,
                job.storage_key,
            )
        except InvalidAudioStorageKeyError:
            return _failure(
                scope=ErrorScope.JOB,
                technical_code="invalid_storage_key",
            )
        except (OSError, RuntimeError):
            return _failure(
                scope=ErrorScope.SYSTEM,
                technical_code="audio_root_unavailable",
            )

        try:
            is_symlink = audio_path.is_symlink()
            is_regular = audio_path.is_file()
        except OSError:
            return _failure(
                scope=ErrorScope.JOB,
                technical_code="audio_inspection_failed",
            )
        if is_symlink or not is_regular:
            return _failure(
                scope=ErrorScope.JOB,
                technical_code="audio_missing_or_unsafe",
            )

        try:
            size_bytes = audio_path.stat().st_size
        except OSError:
            return _failure(
                scope=ErrorScope.JOB,
                technical_code="audio_inspection_failed",
            )
        if size_bytes != job.audio_size_bytes:
            return _failure(
                scope=ErrorScope.JOB,
                technical_code="audio_size_mismatch",
            )
        return audio_path


def _failure(*, scope: ErrorScope, technical_code: str) -> FailureDetails:
    return FailureDetails(
        scope=scope,
        category="storage",
        technical_code=technical_code,
    )


__all__ = ["AudioInspectionResult", "AudioInspector"]
