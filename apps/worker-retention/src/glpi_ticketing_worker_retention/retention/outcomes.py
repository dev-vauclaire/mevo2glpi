"""Sanitized outcomes exposed by one retention-worker iteration."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from glpi_ticketing_database import DeletionReason, ErrorScope
from glpi_ticketing_glpi import TicketStatus

_SAFE_IDENTIFIER = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


class RetentionRunStatus(StrEnum):
    """Observable boundary reached by one worker iteration."""

    IDLE = "idle"
    DEPENDENCY_WAITING = "dependency_waiting"
    CHECK_RECOVERED = "check_recovered"
    GLPI_CHECK_RESCHEDULED = "glpi_check_rescheduled"
    RETRY_SCHEDULED = "retry_scheduled"
    SYSTEM_BLOCKED = "system_blocked"
    RETENTION_BLOCKED = "retention_blocked"
    AUDIO_DELETED = "audio_deleted"
    STALE_CANDIDATE = "stale_candidate"


class RetentionOperation(StrEnum):
    """External operation associated with a retention outcome."""

    GLPI_CHECK = "glpi_check"
    AUDIO_PURGE = "audio_purge"


class DependencyCondition(StrEnum):
    """Health state retained while a persisted GLPI probe is not due."""

    DEGRADED = "degraded"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True, kw_only=True)
class RetentionFailure:
    """Small failure descriptor safe to persist, log and expose."""

    scope: ErrorScope
    category: str
    technical_code: str

    def __post_init__(self) -> None:
        if not isinstance(self.scope, ErrorScope):
            raise TypeError("scope must be an ErrorScope")
        _validate_identifier(self.category, "category")
        _validate_identifier(self.technical_code, "technical_code")


@dataclass(frozen=True, slots=True, kw_only=True)
class IdleOutcome:
    """Report that no retention operation is currently eligible."""

    wait_seconds: float
    status: RetentionRunStatus = field(init=False, default=RetentionRunStatus.IDLE)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "wait_seconds",
            _validate_positive_seconds(self.wait_seconds, "wait_seconds"),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class DependencyWaitingOutcome:
    """Wait for the single persisted GLPI probe without fanning out calls."""

    job_uuid: UUID
    failure: RetentionFailure
    condition: DependencyCondition
    wait_seconds: float
    status: RetentionRunStatus = field(
        init=False,
        default=RetentionRunStatus.DEPENDENCY_WAITING,
    )

    def __post_init__(self) -> None:
        _validate_uuid(self.job_uuid)
        _validate_failure(self.failure, scope=ErrorScope.SYSTEM)
        if not isinstance(self.condition, DependencyCondition):
            raise TypeError("condition must be a DependencyCondition")
        object.__setattr__(
            self,
            "wait_seconds",
            _validate_positive_seconds(self.wait_seconds, "wait_seconds"),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class CheckRecoveredOutcome:
    """Report that an interrupted GLPI check was durably requeued."""

    job_uuid: UUID
    status: RetentionRunStatus = field(
        init=False,
        default=RetentionRunStatus.CHECK_RECOVERED,
    )

    def __post_init__(self) -> None:
        _validate_uuid(self.job_uuid)


@dataclass(frozen=True, slots=True, kw_only=True)
class GlpiCheckRescheduledOutcome:
    """Report that a non-closed GLPI ticket was checked and retained."""

    job_uuid: UUID
    ticket_status: TicketStatus
    next_check_at: datetime
    status: RetentionRunStatus = field(
        init=False,
        default=RetentionRunStatus.GLPI_CHECK_RESCHEDULED,
    )

    def __post_init__(self) -> None:
        _validate_uuid(self.job_uuid)
        if not isinstance(self.ticket_status, TicketStatus):
            raise TypeError("ticket_status must be a TicketStatus")
        _validate_aware_datetime(self.next_check_at, "next_check_at")


@dataclass(frozen=True, slots=True, kw_only=True)
class RetryScheduledOutcome:
    """Report a recoverable failure durably scheduled with backoff."""

    job_uuid: UUID
    operation: RetentionOperation
    failure: RetentionFailure
    next_attempt_at: datetime
    status: RetentionRunStatus = field(
        init=False,
        default=RetentionRunStatus.RETRY_SCHEDULED,
    )

    def __post_init__(self) -> None:
        _validate_uuid(self.job_uuid)
        _validate_operation(self.operation)
        _validate_failure(self.failure, scope=ErrorScope.SYSTEM)
        _validate_aware_datetime(self.next_attempt_at, "next_attempt_at")


@dataclass(frozen=True, slots=True, kw_only=True)
class SystemBlockedOutcome:
    """Report a systemic GLPI failure persisted as a future probe."""

    job_uuid: UUID
    operation: RetentionOperation
    failure: RetentionFailure
    next_probe_at: datetime
    status: RetentionRunStatus = field(
        init=False,
        default=RetentionRunStatus.SYSTEM_BLOCKED,
    )

    def __post_init__(self) -> None:
        _validate_uuid(self.job_uuid)
        _validate_operation(self.operation)
        _validate_failure(self.failure, scope=ErrorScope.SYSTEM)
        _validate_aware_datetime(self.next_probe_at, "next_probe_at")


@dataclass(frozen=True, slots=True, kw_only=True)
class RetentionBlockedOutcome:
    """Report a persisted ambiguity scoped to one retention record."""

    job_uuid: UUID
    operation: RetentionOperation
    failure: RetentionFailure
    status: RetentionRunStatus = field(
        init=False,
        default=RetentionRunStatus.RETENTION_BLOCKED,
    )

    def __post_init__(self) -> None:
        _validate_uuid(self.job_uuid)
        _validate_operation(self.operation)
        _validate_failure(self.failure, scope=ErrorScope.JOB)


@dataclass(frozen=True, slots=True, kw_only=True)
class AudioDeletedOutcome:
    """Report that audio bytes and their durable deletion state are complete."""

    job_uuid: UUID
    reason: DeletionReason
    status: RetentionRunStatus = field(
        init=False,
        default=RetentionRunStatus.AUDIO_DELETED,
    )

    def __post_init__(self) -> None:
        _validate_uuid(self.job_uuid)
        if not isinstance(self.reason, DeletionReason):
            raise TypeError("reason must be a DeletionReason")


@dataclass(frozen=True, slots=True, kw_only=True)
class StaleCandidateOutcome:
    """Report a harmless race invalidating a previously selected candidate."""

    job_uuid: UUID
    status: RetentionRunStatus = field(
        init=False,
        default=RetentionRunStatus.STALE_CANDIDATE,
    )

    def __post_init__(self) -> None:
        _validate_uuid(self.job_uuid)


type RetentionRunOutcome = (
    IdleOutcome
    | DependencyWaitingOutcome
    | CheckRecoveredOutcome
    | GlpiCheckRescheduledOutcome
    | RetryScheduledOutcome
    | SystemBlockedOutcome
    | RetentionBlockedOutcome
    | AudioDeletedOutcome
    | StaleCandidateOutcome
)


def _validate_uuid(value: object) -> None:
    if not isinstance(value, UUID):
        raise TypeError("job_uuid must be a UUID")


def _validate_operation(value: object) -> None:
    if not isinstance(value, RetentionOperation):
        raise TypeError("operation must be a RetentionOperation")


def _validate_failure(value: object, *, scope: ErrorScope) -> None:
    if not isinstance(value, RetentionFailure):
        raise TypeError("failure must be a RetentionFailure")
    if value.scope is not scope:
        raise ValueError(f"failure scope must be {scope.value}")


def _validate_identifier(value: object, field_name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if _SAFE_IDENTIFIER.fullmatch(value) is None:
        raise ValueError(
            f"{field_name} must be a lowercase snake_case identifier "
            "of at most 64 characters"
        )


def _validate_positive_seconds(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{field_name} must be a number")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized <= 0:
        raise ValueError(f"{field_name} must be finite and greater than zero")
    return normalized


def _validate_aware_datetime(value: object, field_name: str) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    if value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


__all__ = [
    "AudioDeletedOutcome",
    "CheckRecoveredOutcome",
    "DependencyCondition",
    "DependencyWaitingOutcome",
    "GlpiCheckRescheduledOutcome",
    "IdleOutcome",
    "RetentionBlockedOutcome",
    "RetentionFailure",
    "RetentionOperation",
    "RetentionRunOutcome",
    "RetentionRunStatus",
    "RetryScheduledOutcome",
    "StaleCandidateOutcome",
    "SystemBlockedOutcome",
]
