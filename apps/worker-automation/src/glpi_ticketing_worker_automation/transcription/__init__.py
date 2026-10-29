"""Provider-independent transcription contracts and adapters."""

from .contracts import (
    LegacyTranscriptionClientProtocol,
    TranscriberProtocol,
    TranscriptionBlockedError,
    TranscriptionClientError,
    TranscriptionHttpError,
    TranscriptionProtocolError,
    TranscriptionRetryRequired,
    TranscriptionSubmissionCallbackError,
    TranscriptionSubmissionRejectedError,
    TranscriptionTerminalError,
    TranscriptionTransportError,
)
from .legacy_client import LegacyTranscriptionClient
from .legacy_polling import LegacyPollingTranscriber
from .models import (
    JobSnapshot,
    JobStatus,
    JobUuid,
    ResponseLogEntry,
    Submission,
    TranscriptionErrorCategory,
    TranscriptionOperation,
    TranscriptionProvider,
    TranscriptionResult,
    TranscriptionSegment,
)
from .openai_compatible import OpenAICompatibleTranscriber

__all__ = [
    "JobUuid",
    "JobSnapshot",
    "JobStatus",
    "ResponseLogEntry",
    "Submission",
    "LegacyPollingTranscriber",
    "LegacyTranscriptionClient",
    "LegacyTranscriptionClientProtocol",
    "OpenAICompatibleTranscriber",
    "TranscriptionBlockedError",
    "TranscriptionClientError",
    "TranscriptionHttpError",
    "TranscriptionErrorCategory",
    "TranscriptionOperation",
    "TranscriptionProtocolError",
    "TranscriptionProvider",
    "TranscriptionResult",
    "TranscriptionSegment",
    "TranscriptionRetryRequired",
    "TranscriptionSubmissionCallbackError",
    "TranscriptionSubmissionRejectedError",
    "TranscriptionTerminalError",
    "TranscriptionTransportError",
    "TranscriberProtocol",
]
