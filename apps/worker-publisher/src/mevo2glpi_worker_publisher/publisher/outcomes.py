"""Sanitized outcomes exposed by the Exchange publisher worker."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from mevo2glpi_database import MailIngestionErrorScope, MailIngestionStage

_SAFE_IDENTIFIER = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


class PublisherRunStatus(StrEnum):
    """Observable boundary reached by one publisher iteration."""

    IDLE = "idle"
    DISCOVERY_RETRY = "discovery_retry"
    RETRY_SCHEDULED = "retry_scheduled"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    SYSTEM_BLOCKED = "system_blocked"


@dataclass(frozen=True, slots=True, kw_only=True)
class PublisherFailure:
    """Content-free failure descriptor safe for persistence and logs."""

    scope: MailIngestionErrorScope
    category: str
    technical_code: str

    def __post_init__(self) -> None:
        if not isinstance(self.scope, MailIngestionErrorScope):
            raise TypeError("scope must be a MailIngestionErrorScope")
        _validate_identifier(self.category, "category")
        _validate_identifier(self.technical_code, "technical_code")


@dataclass(frozen=True, slots=True, kw_only=True)
class IdleOutcome:
    """Report that no persisted message is currently eligible."""

    wait_seconds: float
    status: PublisherRunStatus = field(init=False, default=PublisherRunStatus.IDLE)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "wait_seconds",
            _positive_seconds(self.wait_seconds, "wait_seconds"),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class DiscoveryRetryOutcome:
    """Report that Inbox discovery failed before an ingestion was selected."""

    failure: PublisherFailure
    wait_seconds: float
    status: PublisherRunStatus = field(
        init=False,
        default=PublisherRunStatus.DISCOVERY_RETRY,
    )

    def __post_init__(self) -> None:
        _validate_failure(self.failure, MailIngestionErrorScope.SYSTEM)
        object.__setattr__(
            self,
            "wait_seconds",
            _positive_seconds(self.wait_seconds, "wait_seconds"),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class RetryScheduledOutcome:
    """Report a recoverable failure durably scheduled in PostgreSQL."""

    ingestion_uuid: UUID
    stage: MailIngestionStage
    failure: PublisherFailure
    next_attempt_at: datetime
    status: PublisherRunStatus = field(
        init=False,
        default=PublisherRunStatus.RETRY_SCHEDULED,
    )

    def __post_init__(self) -> None:
        _validate_uuid(self.ingestion_uuid)
        _validate_stage(self.stage)
        _validate_failure(self.failure)
        _validate_aware_datetime(self.next_attempt_at, "next_attempt_at")


@dataclass(frozen=True, slots=True, kw_only=True)
class CompletedOutcome:
    """Report a published job and a message confirmed in Processed."""

    ingestion_uuid: UUID
    job_uuid: UUID
    status: PublisherRunStatus = field(
        init=False,
        default=PublisherRunStatus.COMPLETED,
    )

    def __post_init__(self) -> None:
        _validate_uuid(self.ingestion_uuid)
        _validate_uuid(self.job_uuid)


@dataclass(frozen=True, slots=True, kw_only=True)
class RejectedOutcome:
    """Report a message rejected locally then confirmed in Rejected."""

    ingestion_uuid: UUID
    failure: PublisherFailure
    status: PublisherRunStatus = field(
        init=False,
        default=PublisherRunStatus.FAILED,
    )

    def __post_init__(self) -> None:
        _validate_uuid(self.ingestion_uuid)
        _validate_failure(self.failure, MailIngestionErrorScope.MESSAGE)


@dataclass(frozen=True, slots=True, kw_only=True)
class BlockedOutcome:
    """Report an ambiguity isolated to one message."""

    ingestion_uuid: UUID
    stage: MailIngestionStage
    failure: PublisherFailure
    status: PublisherRunStatus = field(
        init=False,
        default=PublisherRunStatus.BLOCKED,
    )

    def __post_init__(self) -> None:
        _validate_uuid(self.ingestion_uuid)
        _validate_stage(self.stage)
        _validate_failure(self.failure, MailIngestionErrorScope.MESSAGE)


@dataclass(frozen=True, slots=True, kw_only=True)
class SystemBlockedOutcome:
    """Latch consumption after a non-recoverable systemic failure."""

    failure: PublisherFailure
    probe_after_seconds: float
    ingestion_uuid: UUID | None = None
    stage: MailIngestionStage | None = None
    automatic_probe: bool = False
    status: PublisherRunStatus = field(
        init=False,
        default=PublisherRunStatus.SYSTEM_BLOCKED,
    )

    def __post_init__(self) -> None:
        _validate_failure(self.failure, MailIngestionErrorScope.SYSTEM)
        object.__setattr__(
            self,
            "probe_after_seconds",
            _positive_seconds(self.probe_after_seconds, "probe_after_seconds"),
        )
        if (self.ingestion_uuid is None) != (self.stage is None):
            raise ValueError("ingestion_uuid and stage must be provided together")
        if self.ingestion_uuid is not None:
            _validate_uuid(self.ingestion_uuid)
        if self.stage is not None:
            _validate_stage(self.stage)
        if not isinstance(self.automatic_probe, bool):
            raise TypeError("automatic_probe must be a boolean")


type PublisherRunOutcome = (
    IdleOutcome
    | DiscoveryRetryOutcome
    | RetryScheduledOutcome
    | CompletedOutcome
    | RejectedOutcome
    | BlockedOutcome
    | SystemBlockedOutcome
)


def _validate_identifier(value: object, field_name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if _SAFE_IDENTIFIER.fullmatch(value) is None:
        raise ValueError(
            f"{field_name} must be a lowercase snake_case identifier "
            "of at most 64 characters"
        )


def _validate_uuid(value: object) -> None:
    if not isinstance(value, UUID):
        raise TypeError("ingestion and job identifiers must be UUID values")


def _validate_stage(value: object) -> None:
    if not isinstance(value, MailIngestionStage):
        raise TypeError("stage must be a MailIngestionStage")


def _validate_failure(
    value: object,
    expected_scope: MailIngestionErrorScope | None = None,
) -> None:
    if not isinstance(value, PublisherFailure):
        raise TypeError("failure must be a PublisherFailure")
    if expected_scope is not None and value.scope is not expected_scope:
        raise ValueError(f"failure scope must be {expected_scope.value}")


def _positive_seconds(value: object, field_name: str) -> float:
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
    "BlockedOutcome",
    "CompletedOutcome",
    "DiscoveryRetryOutcome",
    "IdleOutcome",
    "PublisherFailure",
    "PublisherRunOutcome",
    "PublisherRunStatus",
    "RejectedOutcome",
    "RetryScheduledOutcome",
    "SystemBlockedOutcome",
]
