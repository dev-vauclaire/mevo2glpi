"""Exercise ticket-driven and deadline-driven audio retention end to end."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol
from uuid import UUID

import httpx
import pytest
from tests.e2e.support import (
    SyntheticAudio,
    download_audio,
    get_job,
    get_job_events,
    open_e2e_database,
    poll_until,
    seed_audio_job,
    wait_for_job_state,
)
from tests.integration.glpi.plugin import GlpiTestEnvironment
from tests.support.mvp_compose import MvpComposeEnvironment

from mevo2glpi_database import (
    AudioRetentionSnapshot,
    DeletionReason,
    JobEventActor,
    JobEventType,
    JobStatus,
    RetentionStatus,
)
from mevo2glpi_glpi import TicketStatus

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(45 * 60)]

JOB_TIMEOUT_SECONDS = 20 * 60.0
RETENTION_TIMEOUT_SECONDS = 2 * 60.0
RETENTION_SERVICE = "worker-retention"
GLPI_SERVICE = "glpi"


class TranscribeAiEnvironment(Protocol):
    """Narrow fixture contract needed to seed one valid synthetic audio."""

    audio_path: Path


def test_solved_ticket_keeps_audio_then_closed_ticket_purges_it(
    e2e_environment: MvpComposeEnvironment,
    transcribe_ai_environment: TranscribeAiEnvironment,
    glpi_environment: GlpiTestEnvironment,
) -> None:
    """Retain a SOLVED ticket audio, then purge it after the CLOSED transition."""
    audio = transcribe_ai_environment.audio_path.read_bytes()
    retention_stopped = False
    primary_error: BaseException | None = None

    accepted = seed_audio_job(
        e2e_environment,
        SyntheticAudio(filename="retention-ticket.wav", content=audio),
    )
    with e2e_environment.client() as client:
        completed = wait_for_job_state(
            client,
            accepted.audio_uuid,
            JobStatus.COMPLETED.value,
            timeout_seconds=JOB_TIMEOUT_SECONDS,
        )
        ticket_id = _require_ticket_id(completed.glpi_ticket_id)

        try:
            retention_stopped = True
            e2e_environment.stop_service(RETENTION_SERVICE)
            glpi_environment.set_ticket_status(ticket_id, TicketStatus.SOLVED)
            with open_e2e_database(e2e_environment.sqlalchemy_database_url) as database:
                now = datetime.now(UTC)
                database.force_retention_timestamps(
                    accepted.audio_uuid,
                    hard_delete_at=now + timedelta(days=1),
                    next_check_at=now - timedelta(minutes=1),
                )
                e2e_environment.start_service(RETENTION_SERVICE)
                retention_stopped = False
                solved_retention = poll_until(
                    lambda: database.get_retention_snapshot(accepted.audio_uuid),
                    _is_solved_and_scheduled,
                    description="the SOLVED ticket retention check",
                    timeout_seconds=RETENTION_TIMEOUT_SECONDS,
                )

            assert solved_retention is not None
            assert solved_retention.deletion_started_at is None
            retained_audio = download_audio(client, accepted.audio_uuid)
            assert retained_audio.content_length == len(audio)
            assert _digest(retained_audio.content) == _digest(audio)

            retention_stopped = True
            e2e_environment.stop_service(RETENTION_SERVICE)
            glpi_environment.set_ticket_status(ticket_id, TicketStatus.CLOSED)
            with open_e2e_database(e2e_environment.sqlalchemy_database_url) as database:
                now = datetime.now(UTC)
                database.force_retention_timestamps(
                    accepted.audio_uuid,
                    hard_delete_at=now + timedelta(days=1),
                    next_check_at=now - timedelta(minutes=1),
                )
                e2e_environment.start_service(RETENTION_SERVICE)
                retention_stopped = False
                deleted_retention = poll_until(
                    lambda: database.get_retention_snapshot(accepted.audio_uuid),
                    _is_ticket_closed_deletion,
                    description="the CLOSED ticket audio purge",
                    timeout_seconds=RETENTION_TIMEOUT_SECONDS,
                )
        except BaseException as error:
            primary_error = error
            raise
        finally:
            _restore_services(
                e2e_environment,
                glpi_environment,
                retention_stopped=retention_stopped,
                glpi_stopped=False,
                primary_error=primary_error,
            )

        assert deleted_retention is not None
        _assert_audio_absent(client, accepted.audio_uuid)
        detail = get_job(client, accepted.audio_uuid)
        assert detail.state == JobStatus.COMPLETED.value
        assert detail.retention_status == RetentionStatus.DELETED.value
        _assert_deletion_events(client, accepted.audio_uuid)


def test_expired_deadline_purges_audio_without_glpi(
    e2e_environment: MvpComposeEnvironment,
    transcribe_ai_environment: TranscribeAiEnvironment,
    glpi_environment: GlpiTestEnvironment,
) -> None:
    """Apply the hard deadline while GLPI is unavailable."""
    audio = transcribe_ai_environment.audio_path.read_bytes()
    retention_stopped = False
    glpi_stopped = False
    primary_error: BaseException | None = None

    accepted = seed_audio_job(
        e2e_environment,
        SyntheticAudio(filename="retention-deadline.wav", content=audio),
    )
    with e2e_environment.client() as client:
        wait_for_job_state(
            client,
            accepted.audio_uuid,
            JobStatus.COMPLETED.value,
            timeout_seconds=JOB_TIMEOUT_SECONDS,
        )
        retained_audio = download_audio(client, accepted.audio_uuid)
        assert retained_audio.content_length == len(audio)
        assert _digest(retained_audio.content) == _digest(audio)

        try:
            retention_stopped = True
            e2e_environment.stop_service(RETENTION_SERVICE)
            glpi_stopped = True
            glpi_environment.stop_service(GLPI_SERVICE)
            with open_e2e_database(e2e_environment.sqlalchemy_database_url) as database:
                now = datetime.now(UTC)
                database.force_retention_timestamps(
                    accepted.audio_uuid,
                    hard_delete_at=now - timedelta(minutes=1),
                    next_check_at=now - timedelta(minutes=2),
                )
                e2e_environment.start_service(RETENTION_SERVICE)
                retention_stopped = False
                deleted_retention = poll_until(
                    lambda: database.get_retention_snapshot(accepted.audio_uuid),
                    _is_deadline_deletion_without_glpi,
                    description="the deadline audio purge without GLPI",
                    timeout_seconds=RETENTION_TIMEOUT_SECONDS,
                )
        except BaseException as error:
            primary_error = error
            raise
        finally:
            _restore_services(
                e2e_environment,
                glpi_environment,
                retention_stopped=retention_stopped,
                glpi_stopped=glpi_stopped,
                primary_error=primary_error,
            )

        assert deleted_retention is not None
        _assert_audio_absent(client, accepted.audio_uuid)
        detail = get_job(client, accepted.audio_uuid)
        assert detail.state == JobStatus.COMPLETED.value
        assert detail.retention_status == RetentionStatus.DELETED.value
        _assert_deletion_events(client, accepted.audio_uuid)


def _is_solved_and_scheduled(
    retention: AudioRetentionSnapshot | None,
) -> bool:
    return (
        retention is not None
        and retention.status is RetentionStatus.SCHEDULED
        and retention.last_glpi_status_id == int(TicketStatus.SOLVED)
        and retention.check_attempt_count == 1
        and retention.last_checked_at is not None
    )


def _is_ticket_closed_deletion(
    retention: AudioRetentionSnapshot | None,
) -> bool:
    return (
        retention is not None
        and retention.status is RetentionStatus.DELETED
        and retention.deletion_reason is DeletionReason.TICKET_CLOSED
        and retention.last_glpi_status_id == int(TicketStatus.CLOSED)
        and retention.check_attempt_count == 2
        and retention.audio_deleted_at is not None
    )


def _is_deadline_deletion_without_glpi(
    retention: AudioRetentionSnapshot | None,
) -> bool:
    return (
        retention is not None
        and retention.status is RetentionStatus.DELETED
        and retention.deletion_reason is DeletionReason.DEADLINE_EXPIRED
        and retention.last_glpi_status_id is None
        and retention.check_attempt_count == 0
        and retention.audio_deleted_at is not None
    )


def _assert_audio_absent(client: httpx.Client, audio_uuid: UUID) -> None:
    response = client.get(f"/audios/{audio_uuid}")
    assert response.status_code == httpx.codes.GONE


def _assert_deletion_events(client: httpx.Client, audio_uuid: UUID) -> None:
    events = get_job_events(client, audio_uuid)
    retention_events = tuple(
        event.event_type
        for event in events
        if event.actor == JobEventActor.WORKER_RETENTION.value
    )
    assert retention_events == (
        JobEventType.AUDIO_DELETION_STARTED.value,
        JobEventType.AUDIO_DELETED.value,
    )


def _require_ticket_id(value: int | None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise AssertionError("the completed job has no GLPI ticket identifier")
    return value


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _restore_services(
    e2e_environment: MvpComposeEnvironment,
    glpi_environment: GlpiTestEnvironment,
    *,
    retention_stopped: bool,
    glpi_stopped: bool,
    primary_error: BaseException | None,
) -> None:
    failed_services: list[str] = []
    if glpi_stopped:
        try:
            glpi_environment.start_service(GLPI_SERVICE)
        except Exception:
            failed_services.append("GLPI")
    if retention_stopped:
        try:
            e2e_environment.start_service(RETENTION_SERVICE)
        except Exception:
            failed_services.append("retention worker")
    if not failed_services:
        return

    message = "Unable to restore: " + ", ".join(failed_services)
    if primary_error is not None:
        primary_error.add_note(message)
        return
    raise RuntimeError(message)
