"""Stable, sanitized outcomes exposed by the automation worker."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from uuid import UUID

from glpi_ticketing_common import audio_files

from ..glpi.models import TicketId

_SAFE_IDENTIFIER = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


class RunStatus(StrEnum):
    """Externally observable result of one worker iteration."""

    IDLE = "idle"
    RETRY = "retry"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"


class ProcessingStage(StrEnum):
    """Pipeline stage associated with a non-successful outcome."""

    TRANSCRIPTION = "transcription"
    TICKETING = "ticketing"
    FINALIZATION = "finalization"


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkerDirectories:
    """Directories participating in the audio lifecycle."""

    pending: Path
    processing: Path
    completed: Path
    failed: Path

    def __post_init__(self) -> None:
        paths = {
            field_name: _validate_path(getattr(self, field_name), field_name)
            for field_name in ("pending", "processing", "completed", "failed")
        }
        resolved_paths = {path.resolve(strict=False) for path in paths.values()}
        if len(resolved_paths) != len(paths):
            raise ValueError("worker directories must be distinct")


@dataclass(frozen=True, slots=True, kw_only=True)
class FailureDetails:
    """Small failure descriptor safe to expose outside the worker.

    Free-form messages and remote response contents are intentionally excluded.
    Both values must be stable identifiers chosen by the application.
    """

    category: str
    technical_code: str

    def __post_init__(self) -> None:
        _validate_safe_identifier(self.category, "category")
        _validate_safe_identifier(self.technical_code, "technical_code")


@dataclass(frozen=True, slots=True, kw_only=True)
class IdleOutcome:
    """Report that no audio is currently available for processing."""

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
    """Report a recoverable failure while retaining the processing audio."""

    audio_uuid: UUID
    processing_path: Path
    stage: ProcessingStage
    failure: FailureDetails
    retry_after_seconds: float
    status: RunStatus = field(init=False, default=RunStatus.RETRY)

    def __post_init__(self) -> None:
        _validate_audio_outcome(
            audio_uuid=self.audio_uuid,
            audio_path=self.processing_path,
            path_field_name="processing_path",
        )
        _validate_stage(self.stage)
        _validate_failure(self.failure)
        object.__setattr__(
            self,
            "retry_after_seconds",
            _validate_positive_seconds(
                self.retry_after_seconds,
                "retry_after_seconds",
            ),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class CompletedOutcome:
    """Report a created or reconciled ticket and a completed audio transition."""

    audio_uuid: UUID
    completed_path: Path
    ticket_id: TicketId
    status: RunStatus = field(init=False, default=RunStatus.COMPLETED)

    def __post_init__(self) -> None:
        _validate_audio_outcome(
            audio_uuid=self.audio_uuid,
            audio_path=self.completed_path,
            path_field_name="completed_path",
        )
        _validate_ticket_id(self.ticket_id)


@dataclass(frozen=True, slots=True, kw_only=True)
class FailedOutcome:
    """Report a terminal per-audio failure published in the failed directory."""

    audio_uuid: UUID
    failed_path: Path
    error_sidecar_path: Path
    stage: ProcessingStage
    failure: FailureDetails
    status: RunStatus = field(init=False, default=RunStatus.FAILED)

    def __post_init__(self) -> None:
        _validate_audio_outcome(
            audio_uuid=self.audio_uuid,
            audio_path=self.failed_path,
            path_field_name="failed_path",
        )
        sidecar_path = _validate_path(
            self.error_sidecar_path,
            "error_sidecar_path",
        )
        expected_sidecar_name = f"{self.audio_uuid}.error.json"
        if sidecar_path.name != expected_sidecar_name:
            raise ValueError(
                f"error_sidecar_path must end with {expected_sidecar_name!r}"
            )
        if sidecar_path.parent.resolve(strict=False) != self.failed_path.parent.resolve(
            strict=False
        ):
            raise ValueError(
                "failed_path and error_sidecar_path must share a directory"
            )
        _validate_stage(self.stage)
        if self.stage is ProcessingStage.FINALIZATION:
            raise ValueError("a completion failure must be blocked, not failed")
        _validate_failure(self.failure)


@dataclass(frozen=True, slots=True, kw_only=True)
class BlockedOutcome:
    """Report work that cannot safely advance without intervention."""

    audio_uuid: UUID
    processing_path: Path | None
    stage: ProcessingStage
    failure: FailureDetails
    ticket_id: TicketId | None = None
    inspection_paths: tuple[Path, ...] = ()
    status: RunStatus = field(init=False, default=RunStatus.BLOCKED)

    def __post_init__(self) -> None:
        if self.processing_path is not None:
            _validate_audio_outcome(
                audio_uuid=self.audio_uuid,
                audio_path=self.processing_path,
                path_field_name="processing_path",
            )
        elif not self.inspection_paths:
            raise ValueError(
                "a block without processing_path must expose inspection_paths"
            )
        _validate_inspection_paths(self.inspection_paths)
        _validate_stage(self.stage)
        _validate_failure(self.failure)
        if self.stage is ProcessingStage.FINALIZATION:
            if self.ticket_id is None:
                raise ValueError("a finalization block must expose ticket_id")
            _validate_ticket_id(self.ticket_id)
        elif self.ticket_id is not None:
            raise ValueError("ticket_id is only valid for a finalization block")


type RunOutcome = (
    IdleOutcome | RetryOutcome | CompletedOutcome | FailedOutcome | BlockedOutcome
)


def _validate_path(value: object, field_name: str) -> Path:
    if not isinstance(value, Path):
        raise TypeError(f"{field_name} must be a Path")
    return value


def _validate_audio_outcome(
    *,
    audio_uuid: object,
    audio_path: object,
    path_field_name: str,
) -> None:
    if not isinstance(audio_uuid, UUID):
        raise TypeError("audio_uuid must be a UUID")
    path = _validate_path(audio_path, path_field_name)
    parsed_name = audio_files.parse_audio_filename(path.name)
    if parsed_name.audio_uuid != audio_uuid:
        raise ValueError(f"{path_field_name} must identify audio_uuid")


def _validate_stage(stage: object) -> None:
    if not isinstance(stage, ProcessingStage):
        raise TypeError("stage must be a ProcessingStage")


def _validate_failure(failure: object) -> None:
    if not isinstance(failure, FailureDetails):
        raise TypeError("failure must be FailureDetails")


def _validate_inspection_paths(paths: object) -> None:
    if not isinstance(paths, tuple):
        raise TypeError("inspection_paths must be a tuple")
    for path in paths:
        _validate_path(path, "inspection_paths item")


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
