"""Persistent worker lifecycle against migrated PostgreSQL and real storage."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from mevo2glpi_common import OriginatingPhoneNumberCipher
from mevo2glpi_common.audio_files import publish_audio
from mevo2glpi_database import (
    AudioExtension,
    AudioRetention,
    DiscoveredMail,
    ErrorScope,
    JobEventActor,
    JobEventType,
    JobStage,
    JobStatus,
    MailIngestionRepository,
    PendingJob,
    RetentionPolicy,
    RetentionStatus,
    TicketCreationJob,
    TicketCreationJobEvent,
    TicketCreationJobEventRepository,
    TicketCreationJobRepository,
    create_session_factory,
)
from mevo2glpi_glpi import GlpiTicketDefaults, TicketLevel, TicketType
from mevo2glpi_worker_automation.title_generation import DefaultTitleGenerator
from mevo2glpi_worker_automation.transcription import (
    TranscriptionResult,
    TranscriptionRetryRequired,
)
from mevo2glpi_worker_automation.worker import (
    AudioInspector,
    AutomationJobLifecycleService,
    AutomationWorker,
    BlockedOutcome,
    CompletedOutcome,
    IdleOutcome,
    InterruptedOutcome,
    JobTransitionService,
    RetentionSchedulePolicy,
    RetryBackoffPolicy,
    TicketCreationStage,
    TranscriptionStage,
)

pytestmark = pytest.mark.database

NOW = datetime(2026, 8, 28, 10, 0, tzinfo=UTC)
SOURCE_RECEIVED_AT = datetime(2026, 8, 27, 7, 30, tzinfo=UTC)
MAILBOX_ID = UUID("78f1f5c0-c798-4af9-9876-f61ef00d996c")
SYNTHETIC_AUDIO = b"RIFF-persistent-worker-integration"
TRANSCRIPTION = "Transcription réservée au ticket de test"
ORIGINATING_PHONE_NUMBER = "0678374363"


@dataclass(slots=True)
class MutableClock:
    current: datetime

    def __call__(self) -> datetime:
        return self.current


class FakeTranscriber:
    """Return one synchronous provider-neutral transcription result."""

    def __init__(self) -> None:
        self.calls: list[Path] = []

    def transcribe(self, audio_path: Path) -> TranscriptionResult:
        self.calls.append(audio_path)
        audio_uuid = UUID(audio_path.stem)
        return TranscriptionResult(
            audio_path=audio_path,
            audio_uuid=audio_uuid,
            transcription=TRANSCRIPTION,
        )


class FakeGlpiClient:
    def __init__(self, ticket_id: int = 42) -> None:
        self._ticket_id = ticket_id
        self.calls = []

    def ensure_ticket(self, ticket) -> int:
        self.calls.append(ticket)
        return self._ticket_id


class IdempotentFakeGlpiClient:
    """Model GLPI reconciliation by the audio UUID external identifier."""

    def __init__(self, ticket_id: int = 42) -> None:
        self._ticket_id = ticket_id
        self._tickets: dict[UUID, int] = {}
        self.calls = []
        self.created_count = 0

    def ensure_ticket(self, ticket) -> int:
        self.calls.append(ticket)
        existing = self._tickets.get(ticket.audio_uuid)
        if existing is not None:
            return existing
        self.created_count += 1
        self._tickets[ticket.audio_uuid] = self._ticket_id
        return self._ticket_id


class InterruptingFakeTranscriber(FakeTranscriber):
    def transcribe(self, audio_path: Path) -> TranscriptionResult:
        super().transcribe(audio_path)
        raise TranscriptionRetryRequired("shutdown_requested", audio_path)


class TicketPersistenceCrashRepository(TicketCreationJobRepository):
    def record_ticket_confirmed(
        self,
        job_uuid: UUID,
        ticket_id: int,
        now: datetime,
    ) -> None:
        del job_uuid, ticket_id, now
        raise RuntimeError("synthetic crash before ticket persistence")


class FailIfCalledTranscriber:
    def __init__(self) -> None:
        self.calls = 0

    def transcribe(self, _audio_path: Path) -> TranscriptionResult:
        self.calls += 1
        raise AssertionError("transcription must not be called")


class FailIfCalledGlpiClient:
    def __init__(self) -> None:
        self.calls = 0

    def ensure_ticket(self, _ticket) -> int:
        self.calls += 1
        raise AssertionError("GLPI must not be called")


def test_pending_job_completes_with_events_retention_and_unchanged_audio(
    migrated_engine,
    tmp_path: Path,
) -> None:
    session_factory = create_session_factory(migrated_engine)
    cipher = OriginatingPhoneNumberCipher(b"k" * 32)
    audio_uuid, audio_path = _add_pending_job(
        session_factory,
        tmp_path,
        originating_phone_number=ORIGINATING_PHONE_NUMBER,
        originating_phone_number_cipher=cipher,
    )
    transcriber = FakeTranscriber()
    glpi_client = FakeGlpiClient(ticket_id=42)
    worker = _worker(
        session_factory,
        tmp_path,
        transcriber,
        glpi_client,
        MutableClock(NOW),
        originating_phone_number_cipher=cipher,
    )

    outcome = worker.run_once()

    assert outcome == CompletedOutcome(audio_uuid=audio_uuid, ticket_id=42)
    assert [command.opened_at for command in glpi_client.calls] == [SOURCE_RECEIVED_AT]
    assert [command.originating_phone_number for command in glpi_client.calls] == [
        ORIGINATING_PHONE_NUMBER
    ]
    assert audio_path.read_bytes() == SYNTHETIC_AUDIO
    assert list(tmp_path.iterdir()) == [audio_path]

    with session_factory() as session:
        job = session.get(TicketCreationJob, audio_uuid)
        retention = session.get(AudioRetention, audio_uuid)
        events = session.scalars(
            select(TicketCreationJobEvent)
            .where(TicketCreationJobEvent.job_uuid == audio_uuid)
            .order_by(TicketCreationJobEvent.event_id)
        ).all()
        source_ingestion = MailIngestionRepository(session).get_by_uuid(audio_uuid)

    assert job is not None
    assert job.status == JobStatus.COMPLETED.value
    assert job.stage == JobStage.FINALIZATION.value
    assert job.glpi_ticket_id == 42
    assert source_ingestion is not None
    assert source_ingestion.originating_phone_number_ciphertext is None
    assert job.attempt_count == 1
    assert retention is not None
    assert retention.policy == RetentionPolicy.TICKET_OR_DEADLINE.value
    assert retention.status == RetentionStatus.SCHEDULED.value
    assert retention.hard_delete_at == NOW + timedelta(days=90)
    assert retention.next_check_at == NOW + timedelta(hours=6)
    assert [event.event_type for event in events] == [
        JobEventType.JOB_CREATED.value,
        JobEventType.PROCESSING_STARTED.value,
        JobEventType.TRANSCRIPTION_SUCCEEDED.value,
        JobEventType.TICKET_CONFIRMED.value,
        JobEventType.JOB_COMPLETED.value,
    ]


def test_crash_recovery_retranscribes_processing_job_without_ticket(
    migrated_engine,
    tmp_path: Path,
) -> None:
    session_factory = create_session_factory(migrated_engine)
    audio_uuid, audio_path = _add_pending_job(session_factory, tmp_path)
    acquisition = AutomationJobLifecycleService(
        session_factory=session_factory
    ).resume_or_claim_next(NOW)
    assert acquisition is not None
    assert acquisition.previous_status is JobStatus.PENDING

    transcriber = FakeTranscriber()
    glpi_client = FakeGlpiClient()
    outcome = _worker(
        session_factory,
        tmp_path,
        transcriber,
        glpi_client,
        MutableClock(NOW + timedelta(seconds=1)),
    ).run_once()

    assert isinstance(outcome, CompletedOutcome)
    assert transcriber.calls == [audio_path]
    assert len(glpi_client.calls) == 1
    with session_factory() as session:
        job = session.get(TicketCreationJob, audio_uuid)
        event_types = session.scalars(
            select(TicketCreationJobEvent.event_type)
            .where(TicketCreationJobEvent.job_uuid == audio_uuid)
            .order_by(TicketCreationJobEvent.event_id)
        ).all()
    assert job is not None
    assert job.attempt_count == 2
    assert JobEventType.PROCESSING_RESUMED.value in event_types
    assert audio_path.read_bytes() == SYNTHETIC_AUDIO


def test_crash_recovery_with_confirmed_ticket_finalizes_without_network(
    migrated_engine,
    tmp_path: Path,
) -> None:
    session_factory = create_session_factory(migrated_engine)
    audio_uuid, audio_path = _add_pending_job(session_factory, tmp_path)
    lifecycle = AutomationJobLifecycleService(session_factory=session_factory)
    assert lifecycle.resume_or_claim_next(NOW) is not None
    lifecycle.record_transcription_succeeded(
        audio_uuid,
        NOW,
    )
    lifecycle.record_ticket_confirmed(audio_uuid, 84, NOW)

    transcriber = FailIfCalledTranscriber()
    glpi_client = FailIfCalledGlpiClient()
    outcome = _worker(
        session_factory,
        tmp_path,
        transcriber,
        glpi_client,
        MutableClock(NOW + timedelta(seconds=1)),
    ).run_once()

    assert outcome == CompletedOutcome(audio_uuid=audio_uuid, ticket_id=84)
    assert transcriber.calls == 0
    assert glpi_client.calls == 0
    assert audio_path.read_bytes() == SYNTHETIC_AUDIO
    with session_factory() as session:
        job = session.get(TicketCreationJob, audio_uuid)
    assert job is not None
    assert job.status == JobStatus.COMPLETED.value


def test_shutdown_during_transcription_keeps_processing_then_restarts_cleanly(
    migrated_engine,
    tmp_path: Path,
) -> None:
    session_factory = create_session_factory(migrated_engine)
    audio_uuid, audio_path = _add_pending_job(session_factory, tmp_path)
    interrupted_transcriber = InterruptingFakeTranscriber()
    first_worker = _worker(
        session_factory,
        tmp_path,
        interrupted_transcriber,
        FakeGlpiClient(),
        MutableClock(NOW),
    )

    first_outcome = first_worker.run_once()

    assert first_outcome == InterruptedOutcome(
        audio_uuid=audio_uuid,
        stage=JobStage.TRANSCRIPTION,
    )
    with session_factory() as session:
        interrupted_job = session.get(TicketCreationJob, audio_uuid)
        interrupted_events = session.scalars(
            select(TicketCreationJobEvent.event_type).where(
                TicketCreationJobEvent.job_uuid == audio_uuid
            )
        ).all()
    assert interrupted_job is not None
    assert interrupted_job.status == JobStatus.PROCESSING.value
    assert interrupted_job.consecutive_retry_count == 0
    assert JobEventType.RETRY_SCHEDULED.value not in interrupted_events

    restarted_transcriber = FakeTranscriber()
    second_outcome = _worker(
        session_factory,
        tmp_path,
        restarted_transcriber,
        FakeGlpiClient(),
        MutableClock(NOW + timedelta(seconds=1)),
    ).run_once()

    assert isinstance(second_outcome, CompletedOutcome)
    assert restarted_transcriber.calls == [audio_path]
    with session_factory() as session:
        completed_job = session.get(TicketCreationJob, audio_uuid)
    assert completed_job is not None
    assert completed_job.attempt_count == 2


def test_restart_reconciles_ticket_created_before_local_id_was_persisted(
    migrated_engine,
    tmp_path: Path,
) -> None:
    session_factory = create_session_factory(migrated_engine)
    audio_uuid, _audio_path = _add_pending_job(session_factory, tmp_path)
    glpi_client = IdempotentFakeGlpiClient(ticket_id=73)

    with pytest.raises(RuntimeError, match="before ticket persistence"):
        _worker(
            session_factory,
            tmp_path,
            FakeTranscriber(),
            glpi_client,
            MutableClock(NOW),
            job_repository_factory=TicketPersistenceCrashRepository,
        ).run_once()

    with session_factory() as session:
        crashed_job = session.get(TicketCreationJob, audio_uuid)
    assert crashed_job is not None
    assert crashed_job.status == JobStatus.PROCESSING.value
    assert crashed_job.stage == JobStage.GLPI_CREATION.value
    assert crashed_job.glpi_ticket_id is None
    assert glpi_client.created_count == 1

    outcome = _worker(
        session_factory,
        tmp_path,
        FakeTranscriber(),
        glpi_client,
        MutableClock(NOW + timedelta(seconds=1)),
    ).run_once()

    assert outcome == CompletedOutcome(audio_uuid=audio_uuid, ticket_id=73)
    assert len(glpi_client.calls) == 2
    assert glpi_client.created_count == 1
    with session_factory() as session:
        completed_job = session.get(TicketCreationJob, audio_uuid)
    assert completed_job is not None
    assert completed_job.glpi_ticket_id == 73
    assert completed_job.status == JobStatus.COMPLETED.value


def test_system_retry_is_ignored_until_due_then_becomes_eligible(
    migrated_engine,
    tmp_path: Path,
) -> None:
    session_factory = create_session_factory(migrated_engine)
    audio_uuid, audio_path = _add_pending_job(session_factory, tmp_path)
    next_attempt_at = NOW + timedelta(minutes=5)
    lifecycle = AutomationJobLifecycleService(session_factory=session_factory)
    assert lifecycle.resume_or_claim_next(NOW) is not None
    lifecycle.schedule_retry(
        audio_uuid,
        stage=JobStage.TRANSCRIPTION,
        error_scope=ErrorScope.SYSTEM,
        error_category="unavailable",
        error_code="transport_error",
        next_attempt_at=next_attempt_at,
        now=NOW,
    )

    clock = MutableClock(NOW + timedelta(minutes=4))
    transcriber = FakeTranscriber()
    glpi_client = FakeGlpiClient()
    worker = _worker(
        session_factory,
        tmp_path,
        transcriber,
        glpi_client,
        clock,
    )

    assert isinstance(worker.run_once(), IdleOutcome)
    assert transcriber.calls == []

    clock.current = next_attempt_at
    outcome = worker.run_once()

    assert isinstance(outcome, CompletedOutcome)
    assert transcriber.calls == [audio_path]
    with session_factory() as session:
        job = session.get(TicketCreationJob, audio_uuid)
    assert job is not None
    assert job.attempt_count == 2


def test_replaced_audio_size_is_blocked_before_external_calls(
    migrated_engine,
    tmp_path: Path,
) -> None:
    session_factory = create_session_factory(migrated_engine)
    audio_uuid, audio_path = _add_pending_job(session_factory, tmp_path)
    audio_path.write_bytes(b"truncated")
    transcriber = FailIfCalledTranscriber()
    glpi_client = FailIfCalledGlpiClient()

    outcome = _worker(
        session_factory,
        tmp_path,
        transcriber,
        glpi_client,
        MutableClock(NOW),
    ).run_once()

    assert isinstance(outcome, BlockedOutcome)
    assert outcome.failure.technical_code == "audio_size_mismatch"
    assert transcriber.calls == 0
    assert glpi_client.calls == 0
    with session_factory() as session:
        job = session.get(TicketCreationJob, audio_uuid)
    assert job is not None
    assert job.status == JobStatus.BLOCKED.value
    assert job.last_error_code == "audio_size_mismatch"


def test_missing_mail_ingestion_blocks_before_audio_and_external_calls(
    migrated_engine,
    tmp_path: Path,
) -> None:
    session_factory = create_session_factory(migrated_engine)
    audio_uuid, audio_path = _add_pending_job(
        session_factory,
        tmp_path,
        with_source_ingestion=False,
    )
    transcriber = FailIfCalledTranscriber()
    glpi_client = FailIfCalledGlpiClient()

    outcome = _worker(
        session_factory,
        tmp_path,
        transcriber,
        glpi_client,
        MutableClock(NOW),
    ).run_once()

    assert isinstance(outcome, BlockedOutcome)
    assert outcome.failure.scope is ErrorScope.JOB
    assert outcome.failure.category == "data_integrity"
    assert outcome.failure.technical_code == "source_received_at_missing"
    assert transcriber.calls == 0
    assert glpi_client.calls == 0
    assert audio_path.read_bytes() == SYNTHETIC_AUDIO
    with session_factory() as session:
        job = session.get(TicketCreationJob, audio_uuid)
    assert job is not None
    assert job.status == JobStatus.BLOCKED.value
    assert job.last_error_code == "source_received_at_missing"


def test_completion_event_failure_rolls_back_status_and_retention_together(
    migrated_engine,
    tmp_path: Path,
) -> None:
    session_factory = create_session_factory(migrated_engine)
    audio_uuid, audio_path = _add_pending_job(session_factory, tmp_path)
    with migrated_engine.begin() as connection:
        connection.execute(
            text(
                "ALTER TABLE ticket_creation_job_events "
                "ADD CONSTRAINT ck_test_reject_completed_event "
                "CHECK (event_type <> 'job_completed')"
            )
        )

    worker = _worker(
        session_factory,
        tmp_path,
        FakeTranscriber(),
        FakeGlpiClient(ticket_id=42),
        MutableClock(NOW),
    )

    with pytest.raises(IntegrityError):
        worker.run_once()

    with session_factory() as session:
        job = session.get(TicketCreationJob, audio_uuid)
        retention = session.get(AudioRetention, audio_uuid)
        completed_events = session.scalars(
            select(TicketCreationJobEvent).where(
                TicketCreationJobEvent.job_uuid == audio_uuid,
                TicketCreationJobEvent.event_type == JobEventType.JOB_COMPLETED.value,
            )
        ).all()
    assert job is not None
    assert job.status == JobStatus.PROCESSING.value
    assert job.stage == JobStage.FINALIZATION.value
    assert job.glpi_ticket_id == 42
    assert retention is None
    assert completed_events == []
    assert audio_path.read_bytes() == SYNTHETIC_AUDIO


@pytest.mark.parametrize(
    ("transition_name", "event_type"),
    [
        ("mark_failed", JobEventType.JOB_FAILED),
        ("mark_blocked", JobEventType.JOB_BLOCKED),
    ],
)
def test_terminal_event_failure_rolls_back_job_and_retention_together(
    migrated_engine,
    tmp_path: Path,
    transition_name: str,
    event_type: JobEventType,
) -> None:
    session_factory = create_session_factory(migrated_engine)
    audio_uuid, _audio_path = _add_pending_job(session_factory, tmp_path)
    lifecycle = AutomationJobLifecycleService(session_factory=session_factory)
    assert lifecycle.resume_or_claim_next(NOW) is not None
    rejection_statement = {
        JobEventType.JOB_FAILED: text(
            "ALTER TABLE ticket_creation_job_events "
            "ADD CONSTRAINT ck_test_reject_job_failed "
            "CHECK (event_type <> 'job_failed')"
        ),
        JobEventType.JOB_BLOCKED: text(
            "ALTER TABLE ticket_creation_job_events "
            "ADD CONSTRAINT ck_test_reject_job_blocked "
            "CHECK (event_type <> 'job_blocked')"
        ),
    }[event_type]
    with migrated_engine.begin() as connection:
        connection.execute(rejection_statement)

    transition = getattr(lifecycle, transition_name)
    transition_arguments = {
        "stage": JobStage.TRANSCRIPTION,
        "error_category": "terminal",
        "error_code": "remote_job_failed",
        "hard_delete_at": NOW + timedelta(days=30),
        "now": NOW + timedelta(seconds=1),
    }
    if transition_name == "mark_blocked":
        transition_arguments["error_scope"] = ErrorScope.JOB

    with pytest.raises(IntegrityError):
        transition(audio_uuid, **transition_arguments)

    with session_factory() as session:
        job = session.get(TicketCreationJob, audio_uuid)
        retention = session.get(AudioRetention, audio_uuid)
        terminal_events = session.scalars(
            select(TicketCreationJobEvent).where(
                TicketCreationJobEvent.job_uuid == audio_uuid,
                TicketCreationJobEvent.event_type == event_type.value,
            )
        ).all()
    assert job is not None
    assert job.status == JobStatus.PROCESSING.value
    assert retention is None
    assert terminal_events == []


def _add_pending_job(
    session_factory,
    audio_root: Path,
    *,
    with_source_ingestion: bool = True,
    originating_phone_number: str | None = None,
    originating_phone_number_cipher: OriginatingPhoneNumberCipher | None = None,
) -> tuple[UUID, Path]:
    audio_uuid = uuid4()
    storage_key = f"{audio_uuid}.wav"
    published = publish_audio(audio_root, storage_key, BytesIO(SYNTHETIC_AUDIO))
    ciphertext = None
    if originating_phone_number is not None:
        assert originating_phone_number_cipher is not None
        ciphertext = originating_phone_number_cipher.encrypt(
            originating_phone_number,
            audio_uuid,
        )
    with session_factory.begin() as session:
        ingestion_repository = MailIngestionRepository(session)
        if with_source_ingestion:
            ingestion_repository.add_discovered(
                DiscoveredMail(
                    uuid=audio_uuid,
                    mailbox_id=MAILBOX_ID,
                    message_id=f"message-{audio_uuid}",
                    received_at=SOURCE_RECEIVED_AT,
                    originating_phone_number_ciphertext=ciphertext,
                ),
                now=NOW,
            )
            acquisition = ingestion_repository.resume_or_claim_next(NOW)
            assert acquisition is not None
            assert acquisition.ingestion.uuid == audio_uuid
            ingestion_repository.record_attachment_validated(
                audio_uuid,
                attachment_id=f"attachment-{audio_uuid}",
                audio_extension=AudioExtension.WAV,
                attachment_size_bytes=published.size_bytes,
                now=NOW,
            )
        pending = PendingJob(
            uuid=audio_uuid,
            storage_key=storage_key,
            audio_extension=AudioExtension.WAV,
            audio_size_bytes=published.size_bytes,
        )
        snapshot = TicketCreationJobRepository(session).add_pending(pending)
        if with_source_ingestion:
            ingestion_repository.record_accepted(
                audio_uuid,
                job_uuid=audio_uuid,
                now=NOW,
            )
            ingestion_repository.record_completed(audio_uuid, now=NOW)
        TicketCreationJobEventRepository(session).append(
            snapshot.uuid,
            occurred_at=snapshot.created_at,
            status=snapshot.status,
            stage=snapshot.stage,
            event_type=JobEventType.JOB_CREATED,
            actor=(
                JobEventActor.WORKER_PUBLISHER
                if with_source_ingestion
                else JobEventActor.WEB
            ),
        )
    return audio_uuid, published.path


def _worker(
    session_factory,
    audio_root: Path,
    transcriber,
    glpi_client,
    clock: MutableClock,
    *,
    job_repository_factory=TicketCreationJobRepository,
    originating_phone_number_cipher: OriginatingPhoneNumberCipher | None = None,
) -> AutomationWorker:
    lifecycle = AutomationJobLifecycleService(
        session_factory=session_factory,
        job_repository_factory=job_repository_factory,
        originating_phone_number_cipher=originating_phone_number_cipher,
    )
    retry_policy = RetryBackoffPolicy(
        initial_seconds=5,
        maximum_seconds=300,
    )
    retention_policy = RetentionSchedulePolicy(
        completed_ttl=timedelta(days=90),
        failed_ttl=timedelta(days=30),
        blocked_ttl=timedelta(days=90),
        first_completed_check_after=timedelta(hours=6),
    )
    transitions = JobTransitionService(
        lifecycle=lifecycle,
        retry_policy=retry_policy,
        retention_policy=retention_policy,
        system_probe_interval_seconds=60,
        now=clock,
    )
    return AutomationWorker(
        lifecycle=lifecycle,
        audio_inspector=AudioInspector(audio_root),
        transcription_stage=TranscriptionStage(
            transcriber=transcriber,
            lifecycle=lifecycle,
            transitions=transitions,
        ),
        ticket_creation_stage=TicketCreationStage(
            glpi_client=glpi_client,
            lifecycle=lifecycle,
            transitions=transitions,
            audio_url_for=lambda audio_uuid: f"https://audio.test/audios/{audio_uuid}",
            ticket_defaults=GlpiTicketDefaults(
                title="Demande issue d'un appel audio",
                ticket_type=TicketType.REQUEST,
                request_source_id=42,
                urgency=TicketLevel.LOW,
                impact=TicketLevel.MEDIUM,
            ),
            title_generator=DefaultTitleGenerator("Demande issue d'un appel audio"),
        ),
        transitions=transitions,
        idle_wait_seconds=1,
    )
