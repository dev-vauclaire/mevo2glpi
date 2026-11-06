"""Readable orchestration of one persistent audio-to-ticket job."""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from uuid import UUID

from mevo2glpi_database import ErrorScope, JobStage

from .audio import AudioInspector
from .outcomes import FailureDetails, IdleOutcome, RunOutcome
from .ports import JobLifecycleProtocol
from .stages import (
    TicketCreationStage,
    TicketCreationSuccess,
    TranscriptionStage,
    TranscriptionSuccess,
)
from .transitions import JobTransitionService

LOGGER = logging.getLogger(__name__)


class AutomationWorker:
    """Advance one job through audio validation, transcription and ticketing.

    The service only defines the order of the pipeline. Storage inspection,
    provider-specific classifications and persistent transitions live in
    dedicated collaborators, keeping the happy path visible from top to bottom.
    """

    def __init__(
        self,
        *,
        lifecycle: JobLifecycleProtocol,
        audio_inspector: AudioInspector,
        transcription_stage: TranscriptionStage,
        ticket_creation_stage: TicketCreationStage,
        transitions: JobTransitionService,
        idle_wait_seconds: float = 1.0,
        on_job_started: Callable[[UUID], None] | None = None,
    ) -> None:
        if not callable(getattr(lifecycle, "resume_or_claim_next", None)):
            raise TypeError("lifecycle must expose resume_or_claim_next()")
        if not isinstance(audio_inspector, AudioInspector):
            raise TypeError("audio_inspector must be an AudioInspector")
        if not isinstance(transcription_stage, TranscriptionStage):
            raise TypeError("transcription_stage must be a TranscriptionStage")
        if not isinstance(ticket_creation_stage, TicketCreationStage):
            raise TypeError("ticket_creation_stage must be a TicketCreationStage")
        if not isinstance(transitions, JobTransitionService):
            raise TypeError("transitions must be a JobTransitionService")
        if on_job_started is not None and not callable(on_job_started):
            raise TypeError("on_job_started must be callable")

        self._lifecycle = lifecycle
        self._audio_inspector = audio_inspector
        self._transcription_stage = transcription_stage
        self._ticket_creation_stage = ticket_creation_stage
        self._transitions = transitions
        self._idle_wait_seconds = _positive_seconds(
            idle_wait_seconds,
            "idle_wait_seconds",
        )
        self._on_job_started = on_job_started or _no_op

    def run_once(self) -> RunOutcome:
        """Acquire at most one job and advance it to an observable boundary."""

        acquisition = self._lifecycle.resume_or_claim_next(
            self._transitions.current_time()
        )
        if acquisition is None:
            return IdleOutcome(wait_seconds=self._idle_wait_seconds)

        job = acquisition.job
        audio_uuid = _require_uuid(job.uuid)
        self._on_job_started(audio_uuid)
        LOGGER.info("audio processing started audio_uuid=%s", audio_uuid)

        if (
            job.glpi_ticket_id is None
            and acquisition.originating_phone_number_error_code is not None
        ):
            return self._transitions.block(
                audio_uuid,
                stage=JobStage.TRANSCRIPTION,
                failure=FailureDetails(
                    scope=ErrorScope.SYSTEM,
                    category="data_protection",
                    technical_code=(acquisition.originating_phone_number_error_code),
                ),
                automatic_probe=False,
            )

        source_received_at = acquisition.source_received_at
        if job.glpi_ticket_id is None and source_received_at is None:
            return self._transitions.block(
                audio_uuid,
                stage=JobStage.TRANSCRIPTION,
                failure=FailureDetails(
                    scope=ErrorScope.JOB,
                    category="data_integrity",
                    technical_code="source_received_at_missing",
                ),
            )

        audio = self._audio_inspector.inspect(job)
        if isinstance(audio, FailureDetails):
            stage = (
                JobStage.FINALIZATION
                if job.glpi_ticket_id is not None
                else JobStage.TRANSCRIPTION
            )
            return self._transitions.block(
                audio_uuid,
                stage=stage,
                failure=audio,
                ticket_id=job.glpi_ticket_id,
            )

        if job.glpi_ticket_id is not None:
            return self._transitions.complete(audio_uuid, job.glpi_ticket_id)

        assert source_received_at is not None

        transcription = self._transcription_stage.execute(job, audio)
        if not isinstance(transcription, TranscriptionSuccess):
            return transcription

        ticket = self._ticket_creation_stage.execute(
            job,
            transcription=transcription.transcription,
            opened_at=source_received_at,
            originating_phone_number=acquisition.originating_phone_number,
        )
        if not isinstance(ticket, TicketCreationSuccess):
            return ticket

        return self._transitions.complete(audio_uuid, ticket.ticket_id)


def _require_uuid(value: object) -> UUID:
    if not isinstance(value, UUID):
        raise TypeError("persisted job uuid must be a UUID")
    return value


def _positive_seconds(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{field_name} must be a number")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized <= 0:
        raise ValueError(f"{field_name} must be finite and greater than zero")
    return normalized


def _no_op(_audio_uuid: UUID) -> None:
    return None


__all__ = ["AutomationWorker"]
