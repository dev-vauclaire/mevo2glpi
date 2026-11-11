"""Provider-independent transcription contracts and adapters."""

from .contracts import (
    TranscriberProtocol,
    TranscriptionBlockedError,
    TranscriptionRetryRequired,
    TranscriptionTerminalError,
)
from .models import (
    TranscriptionErrorCategory,
    TranscriptionResult,
    TranscriptionSegment,
)
from .openai_compatible import OpenAICompatibleTranscriber

__all__ = [
    "OpenAICompatibleTranscriber",
    "TranscriptionBlockedError",
    "TranscriptionErrorCategory",
    "TranscriptionResult",
    "TranscriptionSegment",
    "TranscriptionRetryRequired",
    "TranscriptionTerminalError",
    "TranscriberProtocol",
]
