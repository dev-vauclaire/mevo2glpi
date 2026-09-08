"""Exercise external outages and automation-worker crash recovery."""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr
from tests.e2e.support import (
    AcceptedJob,
    JobEvent,
    SyntheticAudio,
    count_glpi_tickets,
    get_job_events,
    load_correlated_glpi_ticket,
    open_e2e_database,
    poll_until,
    seed_audio_job,
    wait_for_job_state,
    wait_for_jobs_state,
)
from tests.integration.glpi.plugin import GlpiTestEnvironment
from tests.support.mvp_compose import MvpComposeEnvironment

from glpi_ticketing_database import JobEventType, JobStage, JobStatus
from glpi_ticketing_glpi import (
    GlpiClient,
    GlpiTicketCreateCommand,
    GlpiTicketDefaults,
    TicketLevel,
    TicketType,
)

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(45 * 60)]

JOB_TIMEOUT_SECONDS = 20 * 60.0
ANTI_FAN_OUT_OBSERVATION_SECONDS = 2.0
AUTOMATION_SERVICE = "worker-automation"
TRANSCRIBE_API_SERVICE = "api"
TRANSCRIBE_BATCH_WORKER_SERVICE = "mono-voice-worker"
PRECREATED_TICKET_DEFAULTS = GlpiTicketDefaults(
    title="Synthetic audio request",
    ticket_type=TicketType.REQUEST,
    urgency=TicketLevel.MEDIUM,
    impact=TicketLevel.MEDIUM,
)


class TranscribeAiEnvironment(Protocol):
    """Narrow fixture contract needed by the resilience scenarios."""

    audio_path: Path

    def stop_service(self, service_name: str) -> None: ...

    def start_service(self, service_name: str) -> None: ...


def test_transcribe_outage_gates_pending_fan_out_then_recovers(
    e2e_environment: MvpComposeEnvironment,
    transcribe_ai_environment: TranscribeAiEnvironment,
    glpi_environment: GlpiTestEnvironment,
) -> None:
    """Keep later jobs pending behind one system retry, then drain both."""
    audio = transcribe_ai_environment.audio_path.read_bytes()

    with e2e_environment.client() as client:
        try:
            e2e_environment.stop_service(AUTOMATION_SERVICE)
            transcribe_ai_environment.stop_service(TRANSCRIBE_API_SERVICE)
            first = seed_audio_job(
                e2e_environment,
                SyntheticAudio(filename="transcribe-outage-first.wav", content=audio),
            )
            second = seed_audio_job(
                e2e_environment,
                SyntheticAudio(filename="transcribe-outage-second.wav", content=audio),
            )
            e2e_environment.start_service(AUTOMATION_SERVICE)

            retry = wait_for_job_state(
                client,
                first.audio_uuid,
                JobStatus.RETRY_SCHEDULED.value,
                timeout_seconds=JOB_TIMEOUT_SECONDS,
            )
            assert retry.stage == JobStage.TRANSCRIPTION.value
            assert retry.last_error_scope == "system"
            assert retry.last_error_category == "unavailable"
            assert retry.last_error_code == "submission_unavailable"

            time.sleep(ANTI_FAN_OUT_OBSERVATION_SECONDS)
            with open_e2e_database(e2e_environment.sqlalchemy_database_url) as database:
                first_snapshot = database.get_job_snapshot(first.audio_uuid)
                second_snapshot = database.get_job_snapshot(second.audio_uuid)

            assert first_snapshot is not None
            assert first_snapshot.status is JobStatus.RETRY_SCHEDULED
            assert first_snapshot.attempt_count == 1
            assert first_snapshot.next_attempt_at is not None
            assert first_snapshot.last_error_category == "unavailable"
            assert first_snapshot.last_error_code == "submission_unavailable"
            assert second_snapshot is not None
            assert second_snapshot.status is JobStatus.PENDING
            assert second_snapshot.attempt_count == 0

            first_retry_events = _events_of_type(
                get_job_events(client, first.audio_uuid),
                JobEventType.RETRY_SCHEDULED,
            )
            assert len(first_retry_events) == 1
            assert first_retry_events[0].stage == JobStage.TRANSCRIPTION.value
            assert first_retry_events[0].error_scope == "system"
            assert first_retry_events[0].error_category == "unavailable"
            assert first_retry_events[0].technical_code == "submission_unavailable"
            second_events = get_job_events(client, second.audio_uuid)
            assert _event_names(second_events) == [JobEventType.JOB_CREATED.value]
        finally:
            _restore_transcribe_then_automation(
                transcribe_ai_environment,
                TRANSCRIBE_API_SERVICE,
                e2e_environment,
            )

        completed = wait_for_jobs_state(
            client,
            (first.audio_uuid, second.audio_uuid),
            JobStatus.COMPLETED.value,
            timeout_seconds=JOB_TIMEOUT_SECONDS,
        )

    assert all(job.glpi_ticket_id is not None for job in completed)
    _assert_one_correlated_ticket(
        glpi_environment,
        first,
        _require_ticket_id(completed[0].glpi_ticket_id),
    )
    _assert_one_correlated_ticket(
        glpi_environment,
        second,
        _require_ticket_id(completed[1].glpi_ticket_id),
    )


def test_glpi_outage_after_transcription_retranscribes_before_recovery(
    e2e_environment: MvpComposeEnvironment,
    transcribe_ai_environment: TranscribeAiEnvironment,
    glpi_environment: GlpiTestEnvironment,
) -> None:
    """Retry GLPI safely while respecting the no-transcript-persistence policy."""
    audio = transcribe_ai_environment.audio_path.read_bytes()

    with e2e_environment.client() as client:
        try:
            glpi_environment.stop_service("glpi")
            accepted = seed_audio_job(
                e2e_environment,
                SyntheticAudio(filename="glpi-outage.wav", content=audio),
            )
            retry = wait_for_job_state(
                client,
                accepted.audio_uuid,
                JobStatus.RETRY_SCHEDULED.value,
                timeout_seconds=JOB_TIMEOUT_SECONDS,
            )
            assert retry.stage == JobStage.GLPI_CREATION.value
            assert retry.last_error_scope == "system"
            assert retry.last_error_category == "unavailable"
            assert retry.last_error_code == "transport_error"

            with open_e2e_database(e2e_environment.sqlalchemy_database_url) as database:
                retry_snapshot = database.get_job_snapshot(accepted.audio_uuid)
            assert retry_snapshot is not None
            first_provider_job_id = retry_snapshot.transcription_provider_job_id
            assert first_provider_job_id

            retry_events = get_job_events(client, accepted.audio_uuid)
            assert _contains_ordered_subsequence(
                _event_names(retry_events),
                [
                    JobEventType.PROCESSING_STARTED.value,
                    JobEventType.TRANSCRIPTION_SUBMITTED.value,
                    JobEventType.TRANSCRIPTION_SUCCEEDED.value,
                    JobEventType.RETRY_SCHEDULED.value,
                ],
            )
            glpi_retry_events = _events_of_type(
                retry_events,
                JobEventType.RETRY_SCHEDULED,
            )
            assert len(glpi_retry_events) == 1
            assert glpi_retry_events[0].stage == JobStage.GLPI_CREATION.value
            assert glpi_retry_events[0].error_scope == "system"
            assert glpi_retry_events[0].error_category == "unavailable"
            assert glpi_retry_events[0].technical_code == "transport_error"
        finally:
            glpi_environment.start_service("glpi")

        completed = wait_for_job_state(
            client,
            accepted.audio_uuid,
            JobStatus.COMPLETED.value,
            timeout_seconds=JOB_TIMEOUT_SECONDS,
        )
        completed_events = get_job_events(client, accepted.audio_uuid)

    with open_e2e_database(e2e_environment.sqlalchemy_database_url) as database:
        completed_snapshot = database.get_job_snapshot(accepted.audio_uuid)
    assert completed_snapshot is not None
    assert completed_snapshot.attempt_count >= 2
    assert completed_snapshot.transcription_provider_job_id
    assert completed_snapshot.transcription_provider_job_id != first_provider_job_id
    assert (
        _event_count(
            completed_events,
            JobEventType.TRANSCRIPTION_SUBMITTED,
        )
        >= 2
    )
    assert (
        _event_count(
            completed_events,
            JobEventType.TRANSCRIPTION_SUCCEEDED,
        )
        >= 2
    )
    assert _event_count(completed_events, JobEventType.TICKET_CONFIRMED) == 1
    assert _event_count(completed_events, JobEventType.JOB_COMPLETED) == 1
    _assert_one_correlated_ticket(
        glpi_environment,
        accepted,
        _require_ticket_id(completed.glpi_ticket_id),
    )


def test_sigkill_during_processing_resumes_and_reuses_precreated_glpi_ticket(
    e2e_environment: MvpComposeEnvironment,
    transcribe_ai_environment: TranscribeAiEnvironment,
    glpi_environment: GlpiTestEnvironment,
) -> None:
    """Resume the persisted job and keep GLPI idempotency after SIGKILL."""
    audio = transcribe_ai_environment.audio_path.read_bytes()

    with e2e_environment.client() as client:
        try:
            transcribe_ai_environment.stop_service(TRANSCRIBE_BATCH_WORKER_SERVICE)
            accepted = seed_audio_job(
                e2e_environment,
                SyntheticAudio(filename="worker-sigkill.wav", content=audio),
            )
            precreated_ticket_id = _precreate_glpi_ticket(
                glpi_environment,
                accepted.audio_uuid,
                e2e_environment.base_url,
            )

            wait_for_job_state(
                client,
                accepted.audio_uuid,
                JobStatus.PROCESSING.value,
                timeout_seconds=JOB_TIMEOUT_SECONDS,
            )
            _wait_for_events(
                client,
                accepted.audio_uuid,
                lambda events: (
                    _event_count(
                        events,
                        JobEventType.TRANSCRIPTION_SUBMITTED,
                    )
                    >= 1
                ),
                description="the first transcription submission",
            )

            e2e_environment.crash_service(AUTOMATION_SERVICE)
            e2e_environment.start_service(AUTOMATION_SERVICE)
            resumed_events = _wait_for_events(
                client,
                accepted.audio_uuid,
                lambda events: (
                    _event_count(events, JobEventType.PROCESSING_RESUMED) >= 1
                    and _event_count(
                        events,
                        JobEventType.TRANSCRIPTION_SUBMITTED,
                    )
                    >= 2
                ),
                description="processing resume and a fresh transcription submission",
            )
            assert _contains_ordered_subsequence(
                _event_names(resumed_events),
                [
                    JobEventType.PROCESSING_STARTED.value,
                    JobEventType.TRANSCRIPTION_SUBMITTED.value,
                    JobEventType.PROCESSING_RESUMED.value,
                    JobEventType.TRANSCRIPTION_SUBMITTED.value,
                ],
            )
        finally:
            _restore_transcribe_then_automation(
                transcribe_ai_environment,
                TRANSCRIBE_BATCH_WORKER_SERVICE,
                e2e_environment,
            )

        completed = wait_for_job_state(
            client,
            accepted.audio_uuid,
            JobStatus.COMPLETED.value,
            timeout_seconds=JOB_TIMEOUT_SECONDS,
        )
        completed_events = get_job_events(client, accepted.audio_uuid)

    assert _require_ticket_id(completed.glpi_ticket_id) == precreated_ticket_id
    with open_e2e_database(e2e_environment.sqlalchemy_database_url) as database:
        completed_snapshot = database.get_job_snapshot(accepted.audio_uuid)
    assert completed_snapshot is not None
    assert completed_snapshot.status is JobStatus.COMPLETED
    assert completed_snapshot.attempt_count >= 2
    assert completed_snapshot.glpi_ticket_id == precreated_ticket_id
    assert _event_count(completed_events, JobEventType.PROCESSING_STARTED) == 1
    assert _event_count(completed_events, JobEventType.PROCESSING_RESUMED) >= 1
    assert (
        _event_count(
            completed_events,
            JobEventType.TRANSCRIPTION_SUBMITTED,
        )
        >= 2
    )
    assert _event_count(completed_events, JobEventType.TICKET_CONFIRMED) == 1
    assert _event_count(completed_events, JobEventType.JOB_COMPLETED) == 1
    assert _event_count(completed_events, JobEventType.JOB_FAILED) == 0
    assert _event_count(completed_events, JobEventType.JOB_BLOCKED) == 0
    _assert_one_correlated_ticket(
        glpi_environment,
        accepted,
        precreated_ticket_id,
    )


def _wait_for_events(
    client: httpx.Client,
    audio_uuid: UUID,
    predicate: Callable[[tuple[JobEvent, ...]], bool],
    *,
    description: str,
) -> tuple[JobEvent, ...]:
    return poll_until(
        lambda: get_job_events(client, audio_uuid),
        predicate,
        description=description,
        timeout_seconds=JOB_TIMEOUT_SECONDS,
    )


def _event_names(events: Sequence[JobEvent]) -> list[str]:
    return [event.event_type for event in events]


def _event_count(events: Sequence[JobEvent], event_type: JobEventType) -> int:
    return sum(event.event_type == event_type.value for event in events)


def _events_of_type(
    events: Sequence[JobEvent],
    event_type: JobEventType,
) -> tuple[JobEvent, ...]:
    return tuple(event for event in events if event.event_type == event_type.value)


def _contains_ordered_subsequence(
    values: Sequence[str],
    expected: Sequence[str],
) -> bool:
    expected_iterator = iter(expected)
    next_expected = next(expected_iterator, None)
    for value in values:
        if value != next_expected:
            continue
        next_expected = next(expected_iterator, None)
        if next_expected is None:
            return True
    return next_expected is None


def _restore_transcribe_then_automation(
    transcribe_environment: TranscribeAiEnvironment,
    transcribe_service: str,
    mvp_environment: MvpComposeEnvironment,
) -> None:
    try:
        transcribe_environment.start_service(transcribe_service)
    finally:
        mvp_environment.start_service(AUTOMATION_SERVICE)


def _precreate_glpi_ticket(
    environment: GlpiTestEnvironment,
    audio_uuid: UUID,
    public_base_url: str,
) -> int:
    credentials = environment.credentials
    command = GlpiTicketCreateCommand(
        audio_uuid=audio_uuid,
        transcription="Synthetic pre-created ticket for worker crash recovery.",
        audio_url=f"{public_base_url}/audios/{audio_uuid}",
        defaults=PRECREATED_TICKET_DEFAULTS,
    )
    with httpx.Client() as http_client:
        client = GlpiClient(
            base_url=environment.base_url,
            username=credentials.username,
            password=SecretStr(credentials.password),
            client_id=credentials.client_id,
            client_secret=SecretStr(credentials.client_secret),
            http_client=http_client,
            timeout=30,
        )
        return int(client.ensure_ticket(command))


def _assert_one_correlated_ticket(
    environment: GlpiTestEnvironment,
    accepted: AcceptedJob,
    ticket_id: int,
) -> None:
    ticket = load_correlated_glpi_ticket(
        environment,
        accepted.audio_uuid,
        ticket_id,
    )
    assert ticket.ticket_id == ticket_id
    assert ticket.external_id == str(accepted.audio_uuid)
    assert count_glpi_tickets(environment, accepted.audio_uuid) == 1


def _require_ticket_id(value: int | None) -> int:
    if value is None:
        pytest.fail("The completed E2E job has no GLPI ticket identifier")
    return value
