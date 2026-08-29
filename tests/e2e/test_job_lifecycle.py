"""End-to-end web, lifecycle and observability scenarios for the MVP."""

from __future__ import annotations

import hmac
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
import pytest
from tests.e2e.support import (
    AcceptedJob,
    AudioUpload,
    JobDetail,
    JobEvent,
    download_audio,
    get_job_events,
    list_jobs,
    load_correlated_glpi_ticket,
    open_e2e_database,
    retry_job,
    upload_audio,
    upload_audios_concurrently,
    wait_for_alert,
    wait_for_alert_absent,
    wait_for_job_state,
    wait_for_jobs_state,
)
from tests.support.mvp_compose import MvpComposeEnvironment

from glpi_ticketing_database import JobStage, JobStatus
from glpi_ticketing_glpi import TicketStatus

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(45 * 60)]

PIPELINE_TIMEOUT_SECONDS = 20 * 60.0
AUTOMATION_SERVICE = "worker-automation"
EXPECTED_TICKET_TITLE = "Synthetic audio request"
NOMINAL_EVENTS = (
    ("job_created", "pending", None, "web"),
    ("processing_started", "processing", "transcription", "worker_automation"),
    (
        "transcription_submitted",
        "processing",
        "transcription",
        "worker_automation",
    ),
    (
        "transcription_succeeded",
        "processing",
        "glpi_creation",
        "worker_automation",
    ),
    ("ticket_confirmed", "processing", "finalization", "worker_automation"),
    ("job_completed", "completed", "finalization", "worker_automation"),
)


def test_three_concurrent_https_uploads_complete_with_exact_artifacts(
    e2e_environment: MvpComposeEnvironment,
    glpi_environment,
    transcribe_ai_environment,
) -> None:
    """Process the browser concurrency limit through every real application."""
    assert httpx.URL(e2e_environment.base_url).scheme == "https"
    expected_audio = transcribe_ai_environment.audio_path.read_bytes()
    uploads = tuple(
        AudioUpload(
            filename=f"batch-{index}.wav",
            content=expected_audio,
        )
        for index in range(1, 4)
    )

    accepted = upload_audios_concurrently(e2e_environment, uploads)
    audio_uuids = tuple(job.audio_uuid for job in accepted)
    assert len(set(audio_uuids)) == 3
    assert all(job.state == "pending" for job in accepted)
    assert all(job.location == f"/api/jobs/{job.audio_uuid}" for job in accepted)

    with e2e_environment.client() as client:
        completed = wait_for_jobs_state(
            client,
            audio_uuids,
            "completed",
            timeout_seconds=PIPELINE_TIMEOUT_SECONDS,
        )
        summaries = {job.audio_uuid: job for job in list_jobs(client)}
        assert set(audio_uuids) <= set(summaries)

        for detail in completed:
            _assert_completed_detail(detail, expected_attempt_count=1)
            summary = summaries[detail.audio_uuid]
            assert summary.state == "completed"
            assert summary.stage == "finalization"
            assert summary.glpi_ticket_id == detail.glpi_ticket_id
            _assert_exact_events(
                get_job_events(client, detail.audio_uuid), NOMINAL_EVENTS
            )
            _assert_exact_download(client, detail.audio_uuid, expected_audio)

    for detail in completed:
        _assert_exact_glpi_ticket(
            e2e_environment,
            glpi_environment,
            detail,
        )


def test_audio_size_mismatch_blocks_then_operator_retry_completes(
    e2e_environment: MvpComposeEnvironment,
    glpi_environment,
    transcribe_ai_environment,
) -> None:
    """Expose a storage invariant, repair only the bytes, then rearm via HTTP."""
    expected_audio = transcribe_ai_environment.audio_path.read_bytes()
    with _automation_stopped(e2e_environment):
        with e2e_environment.client() as client:
            accepted = upload_audio(
                client,
                AudioUpload(filename="blocked-size.wav", content=expected_audio),
            )
        _change_audio_size(e2e_environment, accepted.audio_uuid, delta=1)

    try:
        with e2e_environment.client() as client:
            blocked = wait_for_job_state(
                client,
                accepted.audio_uuid,
                "blocked",
                timeout_seconds=30,
            )
            assert blocked.stage == "transcription"
            assert blocked.attempt_count == 1
            assert blocked.glpi_ticket_id is None
            assert blocked.last_error_scope == "job"
            assert blocked.last_error_category == "storage"
            assert blocked.last_error_code == "audio_size_mismatch"
            assert blocked.retention_status == "scheduled"
            assert client.get(f"/audios/{accepted.audio_uuid}").status_code == 404

            alert = wait_for_alert(
                client,
                "job_blocked",
                key="jobs:blocked",
                timeout_seconds=10,
            )
            assert alert.severity == "critical"
            assert alert.count >= 1
            _assert_exact_events(
                get_job_events(client, accepted.audio_uuid),
                (
                    NOMINAL_EVENTS[0],
                    NOMINAL_EVENTS[1],
                    (
                        "job_blocked",
                        "blocked",
                        "transcription",
                        "worker_automation",
                    ),
                ),
                expected_error_event=(
                    "job_blocked",
                    "job",
                    "storage",
                    "audio_size_mismatch",
                ),
            )
    finally:
        _change_audio_size(e2e_environment, accepted.audio_uuid, delta=-1)
    with e2e_environment.client() as client:
        retried = retry_job(client, accepted.audio_uuid)
        _assert_retry_acknowledgement(retried, accepted.audio_uuid)
        completed = wait_for_job_state(
            client,
            accepted.audio_uuid,
            "completed",
            timeout_seconds=PIPELINE_TIMEOUT_SECONDS,
        )
        _assert_completed_detail(completed, expected_attempt_count=2)
        wait_for_alert_absent(
            client,
            "job_blocked",
            key="jobs:blocked",
            timeout_seconds=10,
        )
        _assert_recovered_events(
            get_job_events(client, accepted.audio_uuid),
            terminal_event="job_blocked",
            terminal_error=("job", "storage", "audio_size_mismatch"),
        )
        _assert_exact_download(client, accepted.audio_uuid, expected_audio)

    _assert_exact_glpi_ticket(e2e_environment, glpi_environment, completed)


def test_application_failed_transition_alerts_then_operator_retry_completes(
    e2e_environment: MvpComposeEnvironment,
    glpi_environment,
    transcribe_ai_environment,
) -> None:
    """Materialize one terminal job error through the real lifecycle service."""
    expected_audio = transcribe_ai_environment.audio_path.read_bytes()
    with _automation_stopped(e2e_environment):
        with e2e_environment.client() as client:
            accepted = upload_audio(
                client,
                AudioUpload(filename="terminal-failure.wav", content=expected_audio),
            )

        with open_e2e_database(e2e_environment.sqlalchemy_database_url) as database:
            started_at = datetime.now(UTC)
            acquisition = database.lifecycle.resume_or_claim_next(started_at)
            assert acquisition is not None
            assert acquisition.job.uuid == accepted.audio_uuid
            failed_at = started_at + timedelta(microseconds=1)
            snapshot = database.lifecycle.mark_failed(
                accepted.audio_uuid,
                stage=JobStage.TRANSCRIPTION,
                error_category="terminal",
                error_code="synthetic_terminal_rejection",
                hard_delete_at=failed_at + timedelta(days=30),
                now=failed_at,
            )
            assert snapshot.status is JobStatus.FAILED

    with e2e_environment.client() as client:
        failed = wait_for_job_state(
            client,
            accepted.audio_uuid,
            "failed",
            timeout_seconds=10,
        )
        assert failed.stage == "transcription"
        assert failed.attempt_count == 1
        assert failed.glpi_ticket_id is None
        assert failed.last_error_scope == "job"
        assert failed.last_error_category == "terminal"
        assert failed.last_error_code == "synthetic_terminal_rejection"
        assert failed.retention_status == "scheduled"

        alert = wait_for_alert(
            client,
            "job_failed",
            key="jobs:failed",
            timeout_seconds=10,
        )
        assert alert.severity == "critical"
        assert alert.count >= 1
        _assert_exact_events(
            get_job_events(client, accepted.audio_uuid),
            (
                NOMINAL_EVENTS[0],
                NOMINAL_EVENTS[1],
                ("job_failed", "failed", "transcription", "worker_automation"),
            ),
            expected_error_event=(
                "job_failed",
                "job",
                "terminal",
                "synthetic_terminal_rejection",
            ),
        )

        retried = retry_job(client, accepted.audio_uuid)
        _assert_retry_acknowledgement(retried, accepted.audio_uuid)
        completed = wait_for_job_state(
            client,
            accepted.audio_uuid,
            "completed",
            timeout_seconds=PIPELINE_TIMEOUT_SECONDS,
        )
        _assert_completed_detail(completed, expected_attempt_count=2)
        wait_for_alert_absent(
            client,
            "job_failed",
            key="jobs:failed",
            timeout_seconds=10,
        )
        _assert_recovered_events(
            get_job_events(client, accepted.audio_uuid),
            terminal_event="job_failed",
            terminal_error=(
                "job",
                "terminal",
                "synthetic_terminal_rejection",
            ),
        )
        _assert_exact_download(client, accepted.audio_uuid, expected_audio)

    _assert_exact_glpi_ticket(e2e_environment, glpi_environment, completed)


@contextmanager
def _automation_stopped(
    environment: MvpComposeEnvironment,
) -> Iterator[None]:
    environment.stop_service(AUTOMATION_SERVICE)
    try:
        yield
    finally:
        environment.start_service(AUTOMATION_SERVICE)


def _change_audio_size(
    environment: MvpComposeEnvironment,
    audio_uuid: UUID,
    *,
    delta: int,
) -> None:
    if delta not in {-1, 1}:
        raise ValueError("delta must be -1 or 1")
    audio_path = f"/data/audios/{audio_uuid}.wav"
    if delta > 0:
        operation = (
            f"path=Path({audio_path!r}); stream=path.open('ab'); "
            "stream.write(b'!'); stream.flush(); stream.close()"
        )
    else:
        operation = (
            f"path=Path({audio_path!r}); size=path.stat().st_size; "
            "stream=path.open('r+b'); stream.truncate(size-1); "
            "stream.flush(); stream.close()"
        )
    environment.compose(
        "exec",
        "-T",
        "web",
        "python",
        "-c",
        f"from pathlib import Path; {operation}",
    )


def _assert_completed_detail(
    detail: JobDetail,
    *,
    expected_attempt_count: int,
) -> None:
    assert detail.state == "completed"
    assert detail.stage == "finalization"
    assert detail.audio_extension == "wav"
    assert detail.audio_size_bytes > 44
    assert detail.glpi_ticket_id is not None and detail.glpi_ticket_id > 0
    assert detail.attempt_count == expected_attempt_count
    assert detail.consecutive_retry_count == 0
    assert detail.next_attempt_at is None
    assert detail.last_error_scope is None
    assert detail.last_error_category is None
    assert detail.last_error_code is None
    assert detail.finished_at is not None
    assert detail.retention_status == "scheduled"


def _assert_exact_download(
    client: httpx.Client,
    audio_uuid: UUID,
    expected_audio: bytes,
) -> None:
    downloaded = download_audio(client, audio_uuid)
    assert downloaded.audio_uuid == audio_uuid
    assert hmac.compare_digest(downloaded.content, expected_audio)
    assert downloaded.content_length == len(expected_audio)
    assert downloaded.content_disposition == (
        f'attachment; filename="{audio_uuid}.wav"'
    )
    assert downloaded.media_type == "audio/wav"
    assert downloaded.cache_control == "private, no-store"


def _assert_exact_glpi_ticket(
    environment: MvpComposeEnvironment,
    glpi_environment,
    detail: JobDetail,
) -> None:
    assert detail.glpi_ticket_id is not None
    ticket = load_correlated_glpi_ticket(
        glpi_environment,
        detail.audio_uuid,
        detail.glpi_ticket_id,
    )
    assert ticket.ticket_id == detail.glpi_ticket_id
    assert ticket.external_id == str(detail.audio_uuid)
    assert ticket.status is TicketStatus.INCOMING
    assert ticket.title == EXPECTED_TICKET_TITLE
    assert ticket.contains_non_empty_transcription()
    assert ticket.references_audio_url(
        f"{environment.base_url}/audios/{detail.audio_uuid}"
    )


def _assert_retry_acknowledgement(
    retried: AcceptedJob,
    audio_uuid: UUID,
) -> None:
    assert retried.audio_uuid == audio_uuid
    assert retried.state == "pending"
    assert retried.location == f"/api/jobs/{audio_uuid}"


def _assert_exact_events(
    events: tuple[JobEvent, ...],
    expected: tuple[tuple[str, str, str | None, str], ...],
    *,
    expected_error_event: tuple[str, str, str, str] | None = None,
) -> None:
    assert (
        tuple(
            (event.event_type, event.state, event.stage, event.actor)
            for event in events
        )
        == expected
    )
    for event in events:
        if (
            expected_error_event is not None
            and event.event_type == expected_error_event[0]
        ):
            assert (
                event.error_scope,
                event.error_category,
                event.technical_code,
            ) == expected_error_event[1:]
        else:
            assert event.error_scope is None
            assert event.error_category is None
            assert event.technical_code is None


def _assert_recovered_events(
    events: tuple[JobEvent, ...],
    *,
    terminal_event: str,
    terminal_error: tuple[str, str, str],
) -> None:
    expected_types = (
        "job_created",
        "processing_started",
        terminal_event,
        "operator_retry_requested",
        "processing_started",
        "transcription_submitted",
        "transcription_succeeded",
        "ticket_confirmed",
        "job_completed",
    )
    assert tuple(event.event_type for event in events) == expected_types
    terminal = events[2]
    assert (
        terminal.error_scope,
        terminal.error_category,
        terminal.technical_code,
    ) == terminal_error
    assert all(
        event.error_scope is None
        and event.error_category is None
        and event.technical_code is None
        for index, event in enumerate(events)
        if index != 2
    )
