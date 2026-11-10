"""SQLAlchemy models shared by the MVP applications.

Importing this package registers every mapped table on the single shared
``Base.metadata`` object. Audio bytes and transcription contents deliberately
remain outside PostgreSQL.
"""

from .audio_retention import AudioRetention
from .base import NAMING_CONVENTION, Base
from .enums import (
    AudioExtension,
    DeletionReason,
    ErrorScope,
    JobEventActor,
    JobEventType,
    JobStage,
    JobStatus,
    MailIngestionDisposition,
    MailIngestionErrorScope,
    MailIngestionEventType,
    MailIngestionStage,
    MailIngestionStatus,
    RetentionPolicy,
    RetentionStatus,
    WorkerRuntimeState,
    WorkerServiceName,
)
from .mail_ingestion import (
    ORIGINATING_PHONE_NUMBER_CIPHERTEXT_MAX_BYTES,
    MailIngestion,
)
from .mail_ingestion_event import MailIngestionEvent
from .ticket_creation_job import TicketCreationJob
from .ticket_creation_job_event import TicketCreationJobEvent
from .web_user import WebUser
from .worker_runtime_status import WorkerRuntimeStatus

__all__ = [
    "AudioExtension",
    "AudioRetention",
    "Base",
    "DeletionReason",
    "ErrorScope",
    "JobEventActor",
    "JobEventType",
    "JobStage",
    "JobStatus",
    "MailIngestion",
    "MailIngestionDisposition",
    "MailIngestionErrorScope",
    "MailIngestionEvent",
    "MailIngestionEventType",
    "MailIngestionStage",
    "MailIngestionStatus",
    "NAMING_CONVENTION",
    "ORIGINATING_PHONE_NUMBER_CIPHERTEXT_MAX_BYTES",
    "RetentionPolicy",
    "RetentionStatus",
    "TicketCreationJob",
    "TicketCreationJobEvent",
    "WorkerRuntimeState",
    "WorkerRuntimeStatus",
    "WorkerServiceName",
    "WebUser",
]
