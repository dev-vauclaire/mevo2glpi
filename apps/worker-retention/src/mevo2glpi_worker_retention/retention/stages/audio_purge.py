"""Crash-safe filesystem adapter for one exact persisted storage key."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from mevo2glpi_common.audio_purge import AudioPurgeResult, purge_audio


def _no_op() -> None:
    return None


class CrashSafeAudioPurger:
    """Bind the shared purge primitive to the configured canonical volume."""

    def __init__(
        self,
        audio_root: Path,
        *,
        purge: Callable[[Path, str], AudioPurgeResult] = purge_audio,
        ownership_guard: Callable[[], None] = _no_op,
    ) -> None:
        if not isinstance(audio_root, Path):
            raise TypeError("audio_root must be a Path")
        if not callable(purge):
            raise TypeError("purge must be callable")
        if not callable(ownership_guard):
            raise TypeError("ownership_guard must be callable")
        self._audio_root = audio_root
        self._purge = purge
        self._ownership_guard = ownership_guard

    def purge(self, storage_key: str) -> AudioPurgeResult:
        """Purge one key and fence the operation with singleton ownership."""
        self._ownership_guard()
        try:
            return self._purge(self._audio_root, storage_key)
        finally:
            self._ownership_guard()


__all__ = ["CrashSafeAudioPurger"]
