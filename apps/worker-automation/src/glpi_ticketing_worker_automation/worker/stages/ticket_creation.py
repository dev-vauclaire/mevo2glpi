"""GLPI ticket creation stage and its client error classification."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from glpi_ticketing_database import ErrorScope, JobStage

from ...glpi import (
    GlpiClientError,
    GlpiClientProtocol,
    GlpiDuplicateExternalIdError,
    GlpiTicketInput,
    GlpiTicketRejectedError,
)
from ..outcomes import FailureDetails, RunOutcome
from ..ports import JobLifecycleProtocol, JobSnapshotProtocol
from ..transitions import JobTransitionService

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TicketCreationSuccess:
    """Ticket identifier persisted at the GLPI success boundary."""

    ticket_id: int


type TicketCreationStageResult = TicketCreationSuccess | RunOutcome


class TicketCreationStage:
    """Ensure one idempotent GLPI ticket and persist its identifier."""

    def __init__(
        self,
        *,
        glpi_client: GlpiClientProtocol,
        lifecycle: JobLifecycleProtocol,
        transitions: JobTransitionService,
        audio_url_for: Callable[[UUID], str],
        ownership_guard: Callable[[], None] | None = None,
    ) -> None:
        if not callable(getattr(glpi_client, "ensure_ticket", None)):
            raise TypeError("glpi_client must expose ensure_ticket()")
        if not callable(getattr(lifecycle, "record_ticket_confirmed", None)):
            raise TypeError("lifecycle must expose record_ticket_confirmed()")
        if not isinstance(transitions, JobTransitionService):
            raise TypeError("transitions must be a JobTransitionService")
        if not callable(audio_url_for):
            raise TypeError("audio_url_for must be callable")
        if ownership_guard is not None and not callable(ownership_guard):
            raise TypeError("ownership_guard must be callable")

        self._glpi_client = glpi_client
        self._lifecycle = lifecycle
        self._transitions = transitions
        self._audio_url_for = audio_url_for
        self._ownership_guard = ownership_guard or _no_op

    def execute(
        self,
        job: JobSnapshotProtocol,
        *,
        transcription: str,
    ) -> TicketCreationStageResult:
        """Return a persisted ticket ID or an observable failure boundary."""

        audio_uuid = job.uuid
        try:
            audio_url = self._audio_url_for(audio_uuid)
            ticket_input = GlpiTicketInput(
                audio_uuid=audio_uuid,
                transcription=transcription,
                audio_url=audio_url,
            )
        except (TypeError, ValueError):
            return self._transitions.block(
                audio_uuid,
                stage=JobStage.GLPI_CREATION,
                failure=FailureDetails(
                    scope=ErrorScope.SYSTEM,
                    category="configuration",
                    technical_code="invalid_audio_url",
                ),
                automatic_probe=False,
            )

        try:
            ticket_id = self._guarded_external_call(
                lambda: self._glpi_client.ensure_ticket(ticket_input)
            )
        except GlpiClientError as error:
            if error.retryable or error.outcome_unknown:
                return self._transitions.retry(
                    job,
                    stage=JobStage.GLPI_CREATION,
                    category=error.category.value,
                    technical_code=error.technical_code,
                )
            if isinstance(error, GlpiTicketRejectedError):
                return self._transitions.fail(
                    audio_uuid,
                    stage=JobStage.GLPI_CREATION,
                    category=error.category.value,
                    technical_code=error.technical_code,
                )
            scope = _glpi_block_scope(error)
            return self._transitions.block(
                audio_uuid,
                stage=JobStage.GLPI_CREATION,
                failure=FailureDetails(
                    scope=scope,
                    category=error.category.value,
                    technical_code=error.technical_code,
                ),
                automatic_probe=scope is ErrorScope.JOB,
            )

        if not _valid_ticket_id(ticket_id):
            return self._transitions.block(
                audio_uuid,
                stage=JobStage.GLPI_CREATION,
                failure=FailureDetails(
                    scope=ErrorScope.SYSTEM,
                    category="protocol",
                    technical_code="invalid_ticket_id",
                ),
                automatic_probe=False,
            )

        normalized_ticket_id = int(ticket_id)
        self._lifecycle.record_ticket_confirmed(
            audio_uuid,
            normalized_ticket_id,
            self._transitions.current_time(),
        )
        LOGGER.info(
            "ticket ensured audio_uuid=%s ticket_id=%s",
            audio_uuid,
            normalized_ticket_id,
        )
        return TicketCreationSuccess(ticket_id=normalized_ticket_id)

    def _guarded_external_call(self, operation: Callable[[], object]) -> object:
        self._ownership_guard()
        try:
            return operation()
        finally:
            # See TranscriptionStage: no local write or next remote call is
            # allowed after the singleton ownership check fails.
            self._ownership_guard()


def _glpi_block_scope(error: GlpiClientError) -> ErrorScope:
    if isinstance(error, GlpiDuplicateExternalIdError):
        return ErrorScope.JOB
    return ErrorScope.SYSTEM


def _valid_ticket_id(ticket_id: object) -> bool:
    return (
        not isinstance(ticket_id, bool) and isinstance(ticket_id, int) and ticket_id > 0
    )


def _no_op() -> None:
    return None


__all__ = [
    "TicketCreationStage",
    "TicketCreationStageResult",
    "TicketCreationSuccess",
]
