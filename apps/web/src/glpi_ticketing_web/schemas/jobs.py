"""Stable HTTP schemas for the job API."""

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class JobState(StrEnum):
    """States explicitly exposed by the public HTTP contract."""

    PENDING = "pending"
    PROCESSING = "processing"
    RETRY_SCHEDULED = "retry_scheduled"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"


class JobStage(StrEnum):
    """Pipeline stages explicitly exposed by the public HTTP contract."""

    TRANSCRIPTION = "transcription"
    GLPI_CREATION = "glpi_creation"
    FINALIZATION = "finalization"


class JobAcceptedResponse(BaseModel):
    """Representation returned after a durable audio submission."""

    model_config = ConfigDict(frozen=True)

    audio_uuid: UUID
    state: JobState


class JobDetailResponse(BaseModel):
    """Minimal job representation targeted by a creation Location header."""

    model_config = ConfigDict(frozen=True)

    audio_uuid: UUID
    state: JobState
    stage: JobStage | None


__all__ = ["JobAcceptedResponse", "JobDetailResponse", "JobStage", "JobState"]
