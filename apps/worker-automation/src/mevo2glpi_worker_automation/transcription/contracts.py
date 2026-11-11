"""Stable interface and errors for transcription providers."""

from pathlib import Path
from typing import Protocol

from .models import TranscriptionErrorCategory, TranscriptionResult


class TranscriberProtocol(Protocol):
    """Complete one provider-independent transcription attempt for an audio."""

    def transcribe(self, audio_path: Path) -> TranscriptionResult:
        """Return a result or raise a sanitized retry, terminal or blocked error."""
        ...


class TranscriptionTerminalError(Exception):
    """Report a terminal transcription failure without modifying the audio."""

    def __init__(self, code: str, audio_path: Path) -> None:
        self.code = code
        self.audio_path = audio_path
        super().__init__(f"transcription terminal error with code {code}")


class TranscriptionRetryRequired(Exception):
    """Request a fresh submission while leaving the source audio unchanged."""

    def __init__(self, code: str, audio_path: Path) -> None:
        self.code = code
        self.audio_path = audio_path
        super().__init__(f"transcription retry required with code {code}")


class TranscriptionBlockedError(Exception):
    """Report a systemic failure for which failing one audio would be unsafe."""

    def __init__(
        self,
        *,
        category: TranscriptionErrorCategory,
        code: str,
        audio_path: Path,
        automatic_probe: bool = True,
    ) -> None:
        if not isinstance(automatic_probe, bool):
            raise TypeError("automatic_probe must be a boolean")
        self.category = category
        self.code = code
        self.audio_path = audio_path
        self.automatic_probe = automatic_probe
        super().__init__(
            f"transcription blocked with category {category.value} and code {code}"
        )
