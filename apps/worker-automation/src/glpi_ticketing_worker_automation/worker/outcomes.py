"""Sanitized outcomes exposed by the persistent automation worker."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from glpi_ticketing_database import ErrorScope, JobStage, JobStatus

from ..glpi.models import TicketId

_SAFE_IDENTIFIER = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


class RunStatus(StrEnum):
    """Externally observable boundary reached by one worker iteration."""

    IDLE = "idle"
    INTERRUPTED = "interrupted"
    SYSTEM_BLOCKED = "system_blocked"
    RETRY_SCHEDULED = JobStatus.RETRY_SCHEDULED.value
    COMPLETED = JobStatus.COMPLETED.value
    FAILED = JobStatus.FAILED.value
    BLOCKED = JobStatus.BLOCKED.value


@dataclass(frozen=True, slots=True, kw_only=True)
class FailureDetails:
    """Small failure descriptor safe to persist, log and expose.

    Free-form messages and remote response contents are intentionally excluded.
    """

    scope: ErrorScope
    category: str
    technical_code: str

    def __post_init__(self) -> None:
        if not isinstance(self.scope, ErrorScope):
            raise TypeError("scope must be an ErrorScope")
        _validate_safe_identifier(self.category, "category")
        _validate_safe_identifier(self.technical_code, "technical_code")


@dataclass(frozen=True, slots=True, kw_only=True)
class IdleOutcome:
    """Report that no persisted job is currently eligible."""

    wait_seconds: float
    status: RunStatus = field(init=False, default=RunStatus.IDLE)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "wait_seconds",
            _validate_positive_seconds(self.wait_seconds, "wait_seconds"),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class RetryOutcome:
    """Report a recoverable failure durably scheduled in PostgreSQL."""

    audio_uuid: UUID
    stage: JobStage
    failure: FailureDetails
    next_attempt_at: datetime
    status: RunStatus = field(init=False, default=RunStatus.RETRY_SCHEDULED)

    def __post_init__(self) -> None:
        _validate_audio_uuid(self.audio_uuid)
        _validate_stage(self.stage)
        _validate_failure(self.failure)
        _validate_aware_datetime(self.next_attempt_at, "next_attempt_at")


@dataclass(frozen=True, slots=True, kw_only=True)
class InterruptedOutcome:
    """Report a cooperative shutdown while the job remains processing."""

    audio_uuid: UUID
    stage: JobStage
    status: RunStatus = field(init=False, default=RunStatus.INTERRUPTED)

    def __post_init__(self) -> None:
        _validate_audio_uuid(self.audio_uuid)
        _validate_stage(self.stage)


@dataclass(frozen=True, slots=True, kw_only=True)
class CompletedOutcome:
    """Report a ticket confirmed and a job durably finalized."""

    audio_uuid: UUID
    ticket_id: TicketId
    stage: JobStage = field(init=False, default=JobStage.FINALIZATION)
    status: RunStatus = field(init=False, default=RunStatus.COMPLETED)

    def __post_init__(self) -> None:
        _validate_audio_uuid(self.audio_uuid)
        _validate_ticket_id(self.ticket_id)


@dataclass(frozen=True, slots=True, kw_only=True)
class FailedOutcome:
    """Report a terminal failure scoped exclusively to one job."""

    audio_uuid: UUID
    stage: JobStage
    failure: FailureDetails
    status: RunStatus = field(init=False, default=RunStatus.FAILED)

    def __post_init__(self) -> None:
        _validate_audio_uuid(self.audio_uuid)
        _validate_stage(self.stage)
        _validate_failure(self.failure)
        if self.failure.scope is not ErrorScope.JOB:
            raise ValueError("a failed outcome must be scoped to one job")
        if self.stage is JobStage.FINALIZATION:
            raise ValueError("a finalization failure must be blocked, not failed")


@dataclass(frozen=True, slots=True, kw_only=True)
class BlockedOutcome:
    """Report a persisted ambiguity scoped to one job."""

    audio_uuid: UUID
    stage: JobStage
    failure: FailureDetails
    ticket_id: TicketId | None = None
    status: RunStatus = field(init=False, default=RunStatus.BLOCKED)

    def __post_init__(self) -> None:
        _validate_audio_uuid(self.audio_uuid)
        _validate_stage(self.stage)
        _validate_failure(self.failure)
        if self.failure.scope is not ErrorScope.JOB:
            raise ValueError("a blocked job outcome must be scoped to one job")
        if self.stage is JobStage.FINALIZATION:
            if self.ticket_id is None:
                raise ValueError("a finalization block must expose ticket_id")
            _validate_ticket_id(self.ticket_id)
        elif self.ticket_id is not None:
            raise ValueError("ticket_id is only valid for a finalization block")


@dataclass(frozen=True, slots=True, kw_only=True)
class SystemBlockedOutcome:
    """Suspend consumption while the current job stays processing.

    ``automatic_probe=False`` latches an ambiguous external side effect: the
    runner stays alive for observability but never calls the worker again.
    """

    audio_uuid: UUID
    stage: JobStage
    failure: FailureDetails
    probe_after_seconds: float
    automatic_probe: bool = True
    ticket_id: TicketId | None = None
    status: RunStatus = field(init=False, default=RunStatus.SYSTEM_BLOCKED)

    def __post_init__(self) -> None:
        _validate_audio_uuid(self.audio_uuid)
        _validate_stage(self.stage)
        _validate_failure(self.failure)
        if self.failure.scope is not ErrorScope.SYSTEM:
            raise ValueError("a system blocked outcome must have system scope")
        object.__setattr__(
            self,
            "probe_after_seconds",
            _validate_positive_seconds(
                self.probe_after_seconds,
                "probe_after_seconds",
            ),
        )
        if not isinstance(self.automatic_probe, bool):
            raise TypeError("automatic_probe must be a boolean")
        if self.ticket_id is not None:
            _validate_ticket_id(self.ticket_id)


type RunOutcome = (
    IdleOutcome
    | InterruptedOutcome
    | RetryOutcome
    | CompletedOutcome
    | FailedOutcome
    | BlockedOutcome
    | SystemBlockedOutcome
)


def _validate_audio_uuid(audio_uuid: object) -> None:
    if not isinstance(audio_uuid, UUID):
        raise TypeError("audio_uuid must be a UUID")


def _validate_stage(stage: object) -> None:
    if not isinstance(stage, JobStage):
        raise TypeError("stage must be a JobStage")


def _validate_failure(failure: object) -> None:
    if not isinstance(failure, FailureDetails):
        raise TypeError("failure must be FailureDetails")


def _validate_ticket_id(ticket_id: object) -> None:
    if isinstance(ticket_id, bool) or not isinstance(ticket_id, int):
        raise TypeError("ticket_id must be an integer")
    if ticket_id <= 0:
        raise ValueError("ticket_id must be greater than zero")


def _validate_safe_identifier(value: object, field_name: str) -> None:
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
    normalized_value = float(value)
    if not math.isfinite(normalized_value) or normalized_value <= 0:
        raise ValueError(f"{field_name} must be finite and greater than zero")
    return normalized_value


def _validate_aware_datetime(value: object, field_name: str) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    if value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
