"""HTTP-independent values exposed by rejected-mail use cases."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


class RejectedIngestionQueryUnavailableError(RuntimeError):
    """Report a sanitized failure to read rejected mail ingestions."""


@dataclass(frozen=True, slots=True, kw_only=True)
class RejectedIngestionView:
    """Sanitized terminal rejection suitable for the web boundary."""

    ingestion_uuid: UUID
    rejection_category: str
    rejection_code: str
    received_at: datetime
    rejected_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class RejectedIngestionListPage:
    """One sanitized keyset page of rejected mail ingestions."""

    items: tuple[RejectedIngestionView, ...]
    next_cursor: str | None


__all__ = [
    "RejectedIngestionListPage",
    "RejectedIngestionQueryUnavailableError",
    "RejectedIngestionView",
]
