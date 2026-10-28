"""Persistent worker orchestration and sanitized outcomes."""

from glpi_ticketing_database import ErrorScope, JobStage

from .audio import AudioInspector
from .lifecycle import AutomationJobAcquisition, AutomationJobLifecycleService
from .outcomes import (
    BlockedOutcome,
    CompletedOutcome,
    FailedOutcome,
    FailureDetails,
    IdleOutcome,
    InterruptedOutcome,
    RetryOutcome,
    RunOutcome,
    RunStatus,
    SystemBlockedOutcome,
)
from .policies import RetentionSchedulePolicy, RetryBackoffPolicy
from .ports import AutomationWorkerProtocol
from .runner import run_forever
from .service import AutomationWorker
from .stages import TicketCreationStage, TranscriptionStage
from .transitions import JobTransitionService

__all__ = [
    "AudioInspector",
    "AutomationWorker",
    "AutomationWorkerProtocol",
    "AutomationJobAcquisition",
    "AutomationJobLifecycleService",
    "BlockedOutcome",
    "CompletedOutcome",
    "ErrorScope",
    "FailedOutcome",
    "FailureDetails",
    "IdleOutcome",
    "InterruptedOutcome",
    "JobTransitionService",
    "JobStage",
    "RetentionSchedulePolicy",
    "RetryBackoffPolicy",
    "RetryOutcome",
    "RunOutcome",
    "RunStatus",
    "SystemBlockedOutcome",
    "TicketCreationStage",
    "TranscriptionStage",
    "run_forever",
]
