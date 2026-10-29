"""Retention workflow against the shared disposable GLPI stack."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker
from tests.integration.glpi.plugin import GlpiTestEnvironment

from glpi_ticketing_common.backoff import ExponentialBackoffPolicy
from glpi_ticketing_database import (
    AudioExtension,
    AudioRetention,
    AudioRetentionRepository,
    DeletionReason,
    JobEventActor,
    JobEventType,
    JobStatus,
    PendingJob,
    RetentionPolicy,
    RetentionStatus,
    TicketCreationJob,
    TicketCreationJobEvent,
    TicketCreationJobRepository,
    create_session_factory,
)
from glpi_ticketing_glpi import (
    GlpiClient,
    GlpiTicketCreateCommand,
    GlpiTicketDefaults,
    TicketLevel,
    TicketStatus,
    TicketType,
)
from glpi_ticketing_worker_retention.retention import (
    AudioDeletedOutcome,
    GlpiCheckRescheduledOutcome,
    RetentionBlockedOutcome,
    RetentionCheckPolicy,
    RetentionJobLifecycleService,
    RetentionWorker,
)
from glpi_ticketing_worker_retention.retention.stages import (
    CrashSafeAudioPurger,
    GlpiCheckStage,
)

pytestmark = pytest.mark.glpi

NOW = datetime(2026, 8, 29, 10, tzinfo=UTC)
ACTIVATED_AT = NOW - timedelta(days=1)
HARD_DELETE_AT = NOW + timedelta(days=30)
AUDIO_CONTENT = b"RIFF-retention-glpi-integration"


def test_solved_ticket_keeps_audio_and_reschedules_retention(
    glpi_environment: GlpiTestEnvironment,
    migrated_engine: Engine,
    tmp_path: Path,
) -> None:
    session_factory = create_session_factory(migrated_engine)
    job_uuid = uuid4()
    audio_path = _publish_test_audio(tmp_path, job_uuid)

    with httpx.Client() as http_client:
        glpi_client = _glpi_client(glpi_environment, http_client)
        ticket_id = _create_ticket(
            glpi_client,
            job_uuid,
            glpi_environment.request_source.id,
        )
        glpi_environment.set_ticket_status(int(ticket_id), TicketStatus.SOLVED)
        _add_completed_retention(session_factory, job_uuid, int(ticket_id))
        worker = _worker(
            session_factory,
            audio_root=tmp_path,
            glpi_client=glpi_client,
        )

        outcome = worker.run_once()

    assert outcome == GlpiCheckRescheduledOutcome(
        job_uuid=job_uuid,
        ticket_status=TicketStatus.SOLVED,
        next_check_at=NOW + timedelta(hours=6),
    )
    assert audio_path.read_bytes() == AUDIO_CONTENT
    with session_factory() as session:
        job = session.get(TicketCreationJob, job_uuid)
        retention = session.get(AudioRetention, job_uuid)
        events = _retention_events(session, job_uuid)
    assert job is not None
    assert job.status == JobStatus.COMPLETED.value
    assert retention is not None
    assert retention.status == RetentionStatus.SCHEDULED.value
    assert retention.last_glpi_status_id == int(TicketStatus.SOLVED)
    assert retention.next_check_at == NOW + timedelta(hours=6)
    assert retention.deletion_started_at is None
    assert events == []


def test_closed_correlated_ticket_purges_audio_and_persists_events(
    glpi_environment: GlpiTestEnvironment,
    migrated_engine: Engine,
    tmp_path: Path,
) -> None:
    session_factory = create_session_factory(migrated_engine)
    job_uuid = uuid4()
    audio_path = _publish_test_audio(tmp_path, job_uuid)

    with httpx.Client() as http_client:
        glpi_client = _glpi_client(glpi_environment, http_client)
        ticket_id = _create_ticket(
            glpi_client,
            job_uuid,
            glpi_environment.request_source.id,
        )
        glpi_environment.set_ticket_status(int(ticket_id), TicketStatus.CLOSED)
        _add_completed_retention(session_factory, job_uuid, int(ticket_id))
        worker = _worker(
            session_factory,
            audio_root=tmp_path,
            glpi_client=glpi_client,
        )

        outcome = worker.run_once()

    assert outcome == AudioDeletedOutcome(
        job_uuid=job_uuid,
        reason=DeletionReason.TICKET_CLOSED,
    )
    assert not audio_path.exists()
    assert list((tmp_path / ".purge").iterdir()) == []
    with session_factory() as session:
        job = session.get(TicketCreationJob, job_uuid)
        retention = session.get(AudioRetention, job_uuid)
        events = _retention_events(session, job_uuid)
    assert job is not None
    assert job.status == JobStatus.COMPLETED.value
    assert retention is not None
    assert retention.status == RetentionStatus.DELETED.value
    assert retention.deletion_reason == "ticket_closed"
    assert retention.last_glpi_status_id == int(TicketStatus.CLOSED)
    assert retention.audio_deleted_at == NOW
    assert [event.event_type for event in events] == [
        JobEventType.AUDIO_DELETION_STARTED.value,
        JobEventType.AUDIO_DELETED.value,
    ]
    assert all(event.actor == JobEventActor.WORKER_RETENTION.value for event in events)


def test_ticket_correlated_to_another_uuid_blocks_retention_and_keeps_audio(
    glpi_environment: GlpiTestEnvironment,
    migrated_engine: Engine,
    tmp_path: Path,
) -> None:
    session_factory = create_session_factory(migrated_engine)
    local_job_uuid = uuid4()
    remote_job_uuid = uuid4()
    audio_path = _publish_test_audio(tmp_path, local_job_uuid)

    with httpx.Client() as http_client:
        glpi_client = _glpi_client(glpi_environment, http_client)
        ticket_id = _create_ticket(
            glpi_client,
            remote_job_uuid,
            glpi_environment.request_source.id,
        )
        _add_completed_retention(
            session_factory,
            local_job_uuid,
            int(ticket_id),
        )
        worker = _worker(
            session_factory,
            audio_root=tmp_path,
            glpi_client=glpi_client,
        )

        outcome = worker.run_once()

    assert isinstance(outcome, RetentionBlockedOutcome)
    assert outcome.job_uuid == local_job_uuid
    assert outcome.failure.category == "conflict"
    assert outcome.failure.technical_code == "external_id_mismatch"
    assert audio_path.read_bytes() == AUDIO_CONTENT
    assert not (tmp_path / ".purge").exists()
    with session_factory() as session:
        job = session.get(TicketCreationJob, local_job_uuid)
        retention = session.get(AudioRetention, local_job_uuid)
        events = _retention_events(session, local_job_uuid)
    assert job is not None
    assert job.status == JobStatus.COMPLETED.value
    assert retention is not None
    assert retention.status == RetentionStatus.BLOCKED.value
    assert retention.deletion_started_at is None
    assert retention.last_error_category == "conflict"
    assert retention.last_error_code == "external_id_mismatch"
    assert [event.event_type for event in events] == [
        JobEventType.RETENTION_BLOCKED.value
    ]


def _glpi_client(
    environment: GlpiTestEnvironment,
    http_client: httpx.Client,
) -> GlpiClient:
    credentials = environment.credentials
    return GlpiClient(
        base_url=environment.base_url,
        username=credentials.username,
        password=SecretStr(credentials.password),
        client_id=credentials.client_id,
        client_secret=SecretStr(credentials.client_secret),
        http_client=http_client,
        timeout=10.0,
    )


def _create_ticket(
    client: GlpiClient,
    audio_uuid: UUID,
    request_source_id: int,
) -> int:
    return int(
        client.ensure_ticket(
            GlpiTicketCreateCommand(
                audio_uuid=audio_uuid,
                transcription="Transcription synthétique du test de rétention.",
                audio_url=f"https://audio.example.test/{audio_uuid}.wav",
                opened_at=NOW,
                defaults=GlpiTicketDefaults(
                    title="Demande audio de rétention",
                    ticket_type=TicketType.REQUEST,
                    request_source_id=request_source_id,
                    urgency=TicketLevel.LOW,
                    impact=TicketLevel.MEDIUM,
                ),
            )
        )
    )


def _publish_test_audio(audio_root: Path, job_uuid: UUID) -> Path:
    audio_path = audio_root / f"{job_uuid}.wav"
    audio_path.write_bytes(AUDIO_CONTENT)
    return audio_path


def _add_completed_retention(
    session_factory: sessionmaker[Session],
    job_uuid: UUID,
    ticket_id: int,
) -> None:
    with session_factory.begin() as session:
        job_repository = TicketCreationJobRepository(session)
        job_repository.add_pending(
            PendingJob(
                uuid=job_uuid,
                storage_key=f"{job_uuid}.wav",
                audio_extension=AudioExtension.WAV,
                audio_size_bytes=len(AUDIO_CONTENT),
            )
        )
        acquisition = job_repository.resume_or_claim_next(ACTIVATED_AT)
        assert acquisition is not None
        provider_job_id = f"provider-{job_uuid}"
        job_repository.record_transcription_submitted(
            job_uuid,
            provider_job_id,
            ACTIVATED_AT,
        )
        job_repository.advance_after_transcription(
            job_uuid,
            ACTIVATED_AT,
        )
        job_repository.record_ticket_confirmed(
            job_uuid,
            ticket_id,
            ACTIVATED_AT,
        )
        job_repository.mark_completed(job_uuid, now=ACTIVATED_AT)
        AudioRetentionRepository(session).activate(
            job_uuid,
            policy=RetentionPolicy.TICKET_OR_DEADLINE,
            hard_delete_at=HARD_DELETE_AT,
            next_check_at=NOW,
            now=ACTIVATED_AT,
        )


def _worker(
    session_factory: sessionmaker[Session],
    *,
    audio_root: Path,
    glpi_client: GlpiClient,
) -> RetentionWorker:
    check_policy = RetentionCheckPolicy(check_interval=timedelta(hours=6))
    lifecycle = RetentionJobLifecycleService(
        session_factory=session_factory,
        check_policy=check_policy,
    )
    return RetentionWorker(
        lifecycle=lifecycle,
        glpi_check_stage=GlpiCheckStage(glpi_client),
        audio_purger=CrashSafeAudioPurger(audio_root),
        retry_policy=ExponentialBackoffPolicy(
            initial_seconds=5,
            maximum_seconds=300,
        ),
        check_policy=check_policy,
        poll_interval_seconds=60,
        system_probe_interval_seconds=60,
        now=lambda: NOW,
    )


def _retention_events(
    session: Session,
    job_uuid: UUID,
) -> list[TicketCreationJobEvent]:
    return list(
        session.scalars(
            select(TicketCreationJobEvent)
            .where(TicketCreationJobEvent.job_uuid == job_uuid)
            .order_by(TicketCreationJobEvent.event_id)
        ).all()
    )
