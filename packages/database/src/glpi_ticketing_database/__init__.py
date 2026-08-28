"""Shared persistence contracts for the GLPI ticketing MVP."""

from .engine import (
    DatabaseConfigurationError,
    create_database_engine,
    create_session_factory,
)
from .models import (
    AudioExtension,
    AudioRetention,
    Base,
    DeletionReason,
    ErrorScope,
    JobEventActor,
    JobStage,
    JobStatus,
    RetentionPolicy,
    RetentionStatus,
    TicketCreationJob,
    TicketCreationJobEvent,
    WorkerRuntimeState,
    WorkerRuntimeStatus,
    WorkerServiceName,
)
from .repositories import JobRepository, JobSnapshot, PendingJob

__all__ = [
    "AudioExtension",
    "AudioRetention",
    "Base",
    "DatabaseConfigurationError",
    "DeletionReason",
    "ErrorScope",
    "JobEventActor",
    "JobRepository",
    "JobSnapshot",
    "JobStage",
    "JobStatus",
    "RetentionPolicy",
    "RetentionStatus",
    "PendingJob",
    "TicketCreationJob",
    "TicketCreationJobEvent",
    "WorkerRuntimeState",
    "WorkerRuntimeStatus",
    "WorkerServiceName",
    "create_database_engine",
    "create_session_factory",
]
