"""GLPI ticket inspection and application-facing failure classification."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from mevo2glpi_database import ErrorScope
from mevo2glpi_glpi import (
    GlpiClientError,
    GlpiTicketNotFoundError,
    GlpiTicketReaderProtocol,
    GlpiTicketSnapshot,
)

from ..outcomes import RetentionFailure
from ..ports import GlpiCheckWork


def _no_op() -> None:
    return None


class GlpiCheckFailureDisposition(StrEnum):
    """Persistence decision associated with a sanitized GLPI failure."""

    RETRYABLE = "retryable"
    JOB_BLOCKED = "job_blocked"
    SYSTEM_BLOCKED = "system_blocked"


@dataclass(frozen=True, slots=True, kw_only=True)
class GlpiCheckSuccess:
    """Validated GLPI snapshot correctly correlated to one job UUID."""

    snapshot: GlpiTicketSnapshot


@dataclass(frozen=True, slots=True, kw_only=True)
class GlpiCheckFailure:
    """Sanitized GLPI failure and its application disposition."""

    failure: RetentionFailure
    disposition: GlpiCheckFailureDisposition


type GlpiCheckResult = GlpiCheckSuccess | GlpiCheckFailure


class GlpiCheckStage:
    """Read one ticket by primary identifier and verify exact correlation."""

    def __init__(
        self,
        ticket_reader: GlpiTicketReaderProtocol,
        *,
        ownership_guard: Callable[[], None] = _no_op,
    ) -> None:
        if not callable(getattr(ticket_reader, "get_ticket", None)):
            raise TypeError("ticket_reader must expose get_ticket()")
        if not callable(ownership_guard):
            raise TypeError("ownership_guard must be callable")
        self._ticket_reader = ticket_reader
        self._ownership_guard = ownership_guard

    def execute(self, work: GlpiCheckWork) -> GlpiCheckResult:
        """Return a validated snapshot or a response-free failure contract."""
        if not isinstance(work, GlpiCheckWork):
            raise TypeError("work must be a GlpiCheckWork")
        self._ownership_guard()
        try:
            try:
                snapshot = self._ticket_reader.get_ticket(work.ticket_id)
            except GlpiTicketNotFoundError as error:
                return _failure_from_error(
                    error,
                    scope=ErrorScope.JOB,
                    disposition=GlpiCheckFailureDisposition.JOB_BLOCKED,
                )
            except GlpiClientError as error:
                if error.retryable:
                    return _failure_from_error(
                        error,
                        scope=ErrorScope.SYSTEM,
                        disposition=GlpiCheckFailureDisposition.RETRYABLE,
                    )
                return _failure_from_error(
                    error,
                    scope=ErrorScope.SYSTEM,
                    disposition=GlpiCheckFailureDisposition.SYSTEM_BLOCKED,
                )
        finally:
            self._ownership_guard()

        if snapshot.external_id != str(work.job_uuid):
            return GlpiCheckFailure(
                failure=RetentionFailure(
                    scope=ErrorScope.JOB,
                    category="conflict",
                    technical_code="external_id_mismatch",
                ),
                disposition=GlpiCheckFailureDisposition.JOB_BLOCKED,
            )
        return GlpiCheckSuccess(snapshot=snapshot)


def _failure_from_error(
    error: GlpiClientError,
    *,
    scope: ErrorScope,
    disposition: GlpiCheckFailureDisposition,
) -> GlpiCheckFailure:
    return GlpiCheckFailure(
        failure=RetentionFailure(
            scope=scope,
            category=error.category.value,
            technical_code=error.technical_code,
        ),
        disposition=disposition,
    )


__all__ = [
    "GlpiCheckFailure",
    "GlpiCheckFailureDisposition",
    "GlpiCheckResult",
    "GlpiCheckStage",
    "GlpiCheckSuccess",
]
