"""Model-specific repositories for the persistent MVP lifecycle."""

from ._alerting import AlertAggregate
from .audio_retention import (
    AudioRetentionRepository,
    AudioRetentionSnapshot,
    InvalidRetentionTransitionError,
    RetentionAlertSummary,
    RetentionCandidate,
    RetentionCandidateKind,
    RetentionNotFoundError,
)
from .mail_ingestion import (
    DiscoveredMail,
    InvalidMailIngestionTransitionError,
    MailIngestionAcquisition,
    MailIngestionNotFoundError,
    MailIngestionRepository,
    MailIngestionSnapshot,
)
from .mail_ingestion_event import (
    MailIngestionEventCursor,
    MailIngestionEventPage,
    MailIngestionEventRepository,
    MailIngestionEventSnapshot,
)
from .ticket_creation_job import (
    InvalidJobTransitionError,
    JobAcquisition,
    JobAlertSummary,
    JobCursor,
    JobMutation,
    JobNotFoundError,
    JobPage,
    JobSnapshot,
    PendingJob,
    TicketCreationJobRepository,
)
from .ticket_creation_job_event import (
    JobEventCursor,
    JobEventPage,
    JobEventSnapshot,
    TicketCreationJobEventRepository,
)
from .worker_runtime_status import (
    StaleWorkerInstanceError,
    WorkerRuntimeSnapshot,
    WorkerRuntimeStatusRepository,
)

__all__ = [
    "AlertAggregate",
    "AudioRetentionRepository",
    "AudioRetentionSnapshot",
    "InvalidJobTransitionError",
    "InvalidMailIngestionTransitionError",
    "InvalidRetentionTransitionError",
    "JobAcquisition",
    "JobAlertSummary",
    "JobCursor",
    "JobEventCursor",
    "JobEventPage",
    "JobEventSnapshot",
    "JobMutation",
    "JobNotFoundError",
    "JobPage",
    "JobSnapshot",
    "MailIngestionAcquisition",
    "MailIngestionEventCursor",
    "MailIngestionEventPage",
    "MailIngestionEventRepository",
    "MailIngestionEventSnapshot",
    "MailIngestionNotFoundError",
    "MailIngestionRepository",
    "MailIngestionSnapshot",
    "PendingJob",
    "RetentionCandidate",
    "RetentionCandidateKind",
    "RetentionAlertSummary",
    "RetentionNotFoundError",
    "StaleWorkerInstanceError",
    "TicketCreationJobEventRepository",
    "TicketCreationJobRepository",
    "WorkerRuntimeSnapshot",
    "WorkerRuntimeStatusRepository",
    "DiscoveredMail",
]
