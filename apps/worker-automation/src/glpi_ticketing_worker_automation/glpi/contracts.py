"""Stable interface and sanitized failures for the GLPI adapter."""

from __future__ import annotations

from typing import Protocol

from .models import (
    GlpiErrorCategory,
    GlpiOperation,
    GlpiTicketInput,
    TicketId,
)


class GlpiClientProtocol(Protocol):
    """Expose idempotent ticket creation to the worker."""

    def ensure_ticket(self, ticket: GlpiTicketInput) -> TicketId:
        """Return the existing ticket or create it exactly once when possible."""
        ...


class GlpiClientError(Exception):
    """Base class for failures safe to log without remote response bodies."""

    def __init__(
        self,
        *,
        operation: GlpiOperation,
        category: GlpiErrorCategory,
        technical_code: str,
        retryable: bool,
        outcome_unknown: bool,
    ) -> None:
        self.operation = operation
        self.category = category
        self.technical_code = technical_code
        self.retryable = retryable
        self.outcome_unknown = outcome_unknown
        super().__init__(
            f"GLPI {operation.value} failed ({category.value}:{technical_code})"
        )


class GlpiTransportError(GlpiClientError):
    """Raised when no usable HTTP response was received."""

    def __init__(self, operation: GlpiOperation) -> None:
        super().__init__(
            operation=operation,
            category=GlpiErrorCategory.UNAVAILABLE,
            technical_code="transport_error",
            retryable=True,
            outcome_unknown=operation is GlpiOperation.CREATE_TICKET,
        )


class GlpiHttpError(GlpiClientError):
    """Raised when GLPI returns an unexpected HTTP status."""

    def __init__(
        self,
        *,
        operation: GlpiOperation,
        category: GlpiErrorCategory,
        status_code: int,
        retryable: bool,
        outcome_unknown: bool = False,
    ) -> None:
        self.status_code = status_code
        super().__init__(
            operation=operation,
            category=category,
            technical_code=f"http_{status_code}",
            retryable=retryable,
            outcome_unknown=outcome_unknown,
        )


class GlpiProtocolError(GlpiClientError):
    """Raised when a response does not satisfy the pinned API contract."""

    def __init__(
        self,
        operation: GlpiOperation,
        technical_code: str,
        *,
        outcome_unknown: bool = False,
    ) -> None:
        super().__init__(
            operation=operation,
            category=GlpiErrorCategory.PROTOCOL,
            technical_code=technical_code,
            retryable=outcome_unknown,
            outcome_unknown=outcome_unknown,
        )


class GlpiDuplicateExternalIdError(GlpiClientError):
    """Raised when GLPI already contains multiple tickets for one audio UUID."""

    def __init__(self) -> None:
        super().__init__(
            operation=GlpiOperation.FIND_TICKET,
            category=GlpiErrorCategory.CONFLICT,
            technical_code="duplicate_external_id",
            retryable=False,
            outcome_unknown=False,
        )


class GlpiCreateOutcomeUnknownError(GlpiClientError):
    """Raised when ticket creation may have succeeded without a usable response."""

    def __init__(
        self,
        *,
        category: GlpiErrorCategory,
        technical_code: str,
        status_code: int | None = None,
    ) -> None:
        self.status_code = status_code
        super().__init__(
            operation=GlpiOperation.CREATE_TICKET,
            category=category,
            technical_code=technical_code,
            retryable=True,
            outcome_unknown=True,
        )
