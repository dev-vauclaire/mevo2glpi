"""Provider-independent transcription clients."""

from .client import TranscriptionClient
from .contracts import (
    TranscriberProtocol,
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
    "TranscriptionClientError",
    "TranscriptionClientProtocol",
    "TranscriptionHttpError",
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
