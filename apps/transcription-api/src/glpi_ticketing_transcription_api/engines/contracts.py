"""Provider-independent inference contract owned by the API application."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class EngineLoadError(RuntimeError):
    """Report an unavailable inference engine without exposing internals."""


class EngineInferenceError(RuntimeError):
    """Report an inference failure without exposing audio or native details."""


@dataclass(frozen=True, slots=True)
class TranscriptionOutput:
    """Minimal provider-independent output returned by an inference engine."""

    text: str
    language: str | None = None

    def __repr__(self) -> str:
        """Never expose transcription content through debug representations."""
        return f"{type(self).__name__}(text=<redacted>, language={self.language!r})"


class TranscriptionEngine(Protocol):
    """Lifecycle and synchronous inference operations required by FastAPI."""

    @property
    def is_ready(self) -> bool:
        """Return whether the model can accept an inference request."""
        ...

    def load(self) -> None:
        """Load and validate the configured model once."""
        ...

    def transcribe(
        self,
        samples: bytes,
        *,
        language: str | None,
    ) -> TranscriptionOutput:
        """Transcribe mono 16 kHz float32 PCM."""
        ...

    def close(self) -> None:
        """Release native resources."""
        ...
