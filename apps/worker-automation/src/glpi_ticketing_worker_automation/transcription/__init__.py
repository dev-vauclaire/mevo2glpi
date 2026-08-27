"""Provider-independent transcription clients."""

from .client import TranscriptionClient
from .contracts import (
    TranscriberProtocol,
    TranscriptionBlockedError,
    TranscriptionClientError,
    TranscriptionClientProtocol,
    TranscriptionHttpError,
    TranscriptionProtocolError,
    TranscriptionRetryRequired,
    TranscriptionSubmissionRejectedError,
    TranscriptionTerminalError,
    TranscriptionTransportError,
)
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
)
from .poller import TranscriptionPoller

__all__ = [
    "JobUuid",
    "JobSnapshot",
    "JobStatus",
    "ResponseLogEntry",
    "Submission",
    "TranscriptionClient",
    "TranscriptionBlockedError",
    "TranscriptionClientError",
    "TranscriptionClientProtocol",
    "TranscriptionHttpError",
    "TranscriptionErrorCategory",
    "TranscriptionOperation",
    "TranscriptionPoller",
    "TranscriptionProtocolError",
    "TranscriptionProvider",
    "TranscriptionResult",
    "TranscriptionRetryRequired",
    "TranscriptionSubmissionRejectedError",
    "TranscriptionTerminalError",
    "TranscriptionTransportError",
    "TranscriberProtocol",
]
