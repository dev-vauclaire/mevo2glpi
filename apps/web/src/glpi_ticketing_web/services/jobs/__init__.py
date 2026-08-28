"""Job use cases grouped by responsibility."""

from .downloads import GetDownloadableAudio
from .ingestion import SubmitAudioJob
from .models import (
    AudioPurgedError,
    AudioStorageConflictError,
    AudioStorageInvariantError,
    AudioStorageUnavailableError,
    DownloadableAudio,
    EmptyAudioError,
    InvalidCursorError,
    JobEventListPage,
    JobEventView,
    JobListPage,
    JobPersistenceUnavailableError,
    JobQueryUnavailableError,
    JobRetryConflictError,
    JobView,
    RetriedJob,
    SubmittedJob,
    UnsupportedAudioUploadError,
    UploadTooLargeError,
)
from .queries import GetAudioJob, ListAudioJobEvents, ListAudioJobs
from .retry import RetryAudioJob

__all__ = [
    "AudioPurgedError",
    "AudioStorageConflictError",
    "AudioStorageInvariantError",
    "AudioStorageUnavailableError",
    "DownloadableAudio",
    "EmptyAudioError",
    "GetAudioJob",
    "GetDownloadableAudio",
    "InvalidCursorError",
    "JobEventListPage",
    "JobEventView",
    "JobListPage",
    "JobPersistenceUnavailableError",
    "JobQueryUnavailableError",
    "JobRetryConflictError",
    "JobView",
    "ListAudioJobEvents",
    "ListAudioJobs",
    "RetriedJob",
    "RetryAudioJob",
    "SubmitAudioJob",
    "SubmittedJob",
    "UnsupportedAudioUploadError",
    "UploadTooLargeError",
]
