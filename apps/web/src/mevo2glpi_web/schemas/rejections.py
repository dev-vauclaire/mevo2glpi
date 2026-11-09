"""Sanitized HTTP contracts for rejected mail ingestions."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class RejectedIngestionResponse(BaseModel):
    """One rejected message without Exchange source identifiers."""

    model_config = ConfigDict(frozen=True)
    ingestion_uuid: UUID
    received_at: datetime
    rejected_at: datetime
    rejection_category: str
    rejection_code: str


class RejectedIngestionListResponse(BaseModel):
    """One keyset page of rejected messages."""

    model_config = ConfigDict(frozen=True)
    items: tuple[RejectedIngestionResponse, ...]
    next_cursor: str | None


__all__ = ["RejectedIngestionListResponse", "RejectedIngestionResponse"]
