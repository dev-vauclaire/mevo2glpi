"""Provider-independent transcription clients."""

from .client import TranscriptionClient
from .contracts import (
    TranscriptionClientError,
    TranscriptionClientProtocol,
    TranscriptionHttpError,
    TranscriptionProtocolError,
    TranscriptionSubmissionRejectedError,
    TranscriptionTransportError,
)
from .models import (
    JobSnapshot,
    JobStatus,
    JobUuid,
    ResponseLogEntry,
    Submission,
    TranscriptionProvider,
)
from .poller import (
    TranscriptionPoller,
    TranscriptionProcessingFailed,
    TranscriptionResult,
    TranscriptionRetryRequired,
)

__all__ = [
    "JobUuid",
    "JobSnapshot",
    "JobStatus",
    "ResponseLogEntry",
    "Submission",
    "TranscriptionClient",
    "TranscriptionClientError",
    "TranscriptionClientProtocol",
    "TranscriptionHttpError",
    "TranscriptionPoller",
    "TranscriptionProcessingFailed",
    "TranscriptionProtocolError",
    "TranscriptionProvider",
    "TranscriptionResult",
    "TranscriptionRetryRequired",
    "TranscriptionSubmissionRejectedError",
    "TranscriptionTransportError",
]
