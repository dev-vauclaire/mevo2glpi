"""GLPI ticket creation stage and its client error classification."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from uuid import UUID

from glpi_ticketing_database import ErrorScope, JobStage
from glpi_ticketing_glpi import (
    GlpiClientError,
    GlpiDuplicateExternalIdError,
    GlpiTicketCreateCommand,
    GlpiTicketCreationProtocol,
    GlpiTicketDefaults,
    GlpiTicketRejectedError,
)

from ...title_generation import TitleGeneratorProtocol
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
        glpi_client: GlpiTicketCreationProtocol,
        lifecycle: JobLifecycleProtocol,
        transitions: JobTransitionService,
        audio_url_for: Callable[[UUID], str],
        ticket_defaults: GlpiTicketDefaults,
        title_generator: TitleGeneratorProtocol,
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
        if not isinstance(ticket_defaults, GlpiTicketDefaults):
            raise TypeError("ticket_defaults must be a GlpiTicketDefaults")
        if not callable(getattr(title_generator, "generate", None)):
            raise TypeError("title_generator must expose generate()")
        if ownership_guard is not None and not callable(ownership_guard):
            raise TypeError("ownership_guard must be callable")

        self._glpi_client = glpi_client
        self._lifecycle = lifecycle
        self._transitions = transitions
        self._audio_url_for = audio_url_for
        self._ticket_defaults = ticket_defaults
        self._title_generator = title_generator
        self._ownership_guard = ownership_guard or _no_op

    def execute(
        self,
        job: JobSnapshotProtocol,
        *,
        transcription: str,
        opened_at: datetime,
    ) -> TicketCreationStageResult:
        """Return a persisted ticket ID or an observable failure boundary."""

        audio_uuid = job.uuid
        try:
            audio_url = self._audio_url_for(audio_uuid)
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
            generated_title = self._title_generator.generate(transcription)
            command = GlpiTicketCreateCommand(
                audio_uuid=audio_uuid,
                transcription=transcription,
                audio_url=audio_url,
                opened_at=opened_at,
                defaults=replace(self._ticket_defaults, title=generated_title),
            )
        except (TypeError, ValueError):
            return self._transitions.block(
                audio_uuid,
                stage=JobStage.GLPI_CREATION,
                failure=FailureDetails(
                    scope=ErrorScope.SYSTEM,
                    category="configuration",
                    technical_code="invalid_ticket_command",
                ),
                automatic_probe=False,
            )

        try:
            ticket_id = self._guarded_external_call(
                lambda: self._glpi_client.ensure_ticket(command)
            )
        except GlpiClientError as error:
            if error.retryable or error.outcome_unknown:
                if self._transitions.retry_budget_exhausted(job):
                    return self._transitions.block(
                        audio_uuid,
                        stage=JobStage.GLPI_CREATION,
                        failure=FailureDetails(
                            scope=ErrorScope.JOB,
                            category="retry_limit",
                            technical_code="glpi_retry_exhausted",
                        ),
                    )
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
