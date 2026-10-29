"""Private fakes shared by worker pipeline unit tests."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType
from uuid import UUID

from glpi_ticketing_database import (
    ErrorScope,
    JobStage,
    JobStatus,
    MailIngestionDisposition,
)
from glpi_ticketing_glpi import GlpiTicketDefaults, TicketLevel, TicketType
from glpi_ticketing_worker_automation.title_generation import DefaultTitleGenerator
from glpi_ticketing_worker_automation.transcription import (
    TranscriptionResult,
)
from glpi_ticketing_worker_automation.worker import (
    AudioInspector,
    AutomationJobLifecycleService,
    AutomationWorker,
    JobTransitionService,
    RetentionSchedulePolicy,
    RetryBackoffPolicy,
    TicketCreationStage,
    TranscriptionStage,
)

AUDIO_UUID = UUID("123e4567-e89b-42d3-a456-426614174000")
STORAGE_KEY = f"{AUDIO_UUID}.wav"
NOW = datetime(2026, 8, 28, 10, 0, tzinfo=UTC)
SOURCE_RECEIVED_AT = datetime(2026, 8, 27, 7, 30, tzinfo=UTC)
TRANSCRIPTION = "Contenu confidentiel de la transcription"
SECRET = "temporary-secret-must-not-be-logged"
SYNTHETIC_AUDIO = b"RIFF-test-audio"
TICKET_DEFAULTS = GlpiTicketDefaults(
    title="Demande issue d'un appel audio",
    ticket_type=TicketType.REQUEST,
    request_source_id=42,
    urgency=TicketLevel.LOW,
    impact=TicketLevel.MEDIUM,
)


@dataclass(slots=True)
class FakeJob:
    uuid: UUID = AUDIO_UUID
    status: object = JobStatus.PROCESSING
    stage: JobStage | None = JobStage.TRANSCRIPTION
    storage_key: str = STORAGE_KEY
    audio_size_bytes: int = len(SYNTHETIC_AUDIO)
    glpi_ticket_id: int | None = None
    transcription_provider_job_id: str | None = "provider-job-1"
    consecutive_retry_count: int = 0


@dataclass(frozen=True, slots=True)
class FakeAcquisition:
    job: FakeJob
    previous_status: JobStatus = JobStatus.PENDING
    resumed: bool = False


@dataclass(frozen=True, slots=True)
class FakeMailIngestion:
    uuid: UUID = AUDIO_UUID
    job_uuid: UUID = AUDIO_UUID
    disposition: MailIngestionDisposition = MailIngestionDisposition.ACCEPTED
    received_at: datetime = SOURCE_RECEIVED_AT


class FakeState:
    def __init__(
        self,
        job: FakeJob | None = None,
        *,
        source_received_at: datetime | None = SOURCE_RECEIVED_AT,
    ) -> None:
        self.job = job or FakeJob()
        self.source_received_at = source_received_at
        self.eligible = True
        self.active_transactions = 0
        self.repository_calls: list[tuple[object, ...]] = []
        self.event_calls: list[tuple[object, ...]] = []
        self.retention_calls: list[tuple[object, ...]] = []
        self.transaction_outcomes: list[str] = []


class FakeTransaction(AbstractContextManager[object]):
    def __init__(self, state: FakeState) -> None:
        self._state = state

    def __enter__(self) -> object:
        self._state.active_transactions += 1
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_value, traceback
        self._state.active_transactions -= 1
        self._state.transaction_outcomes.append(
            "rollback" if exc_type is not None else "commit"
        )


class FakeSession:
    def __init__(self, state: FakeState) -> None:
        self.state = state
        self.closed = False

    def begin(self) -> FakeTransaction:
        return FakeTransaction(self.state)

    def close(self) -> None:
        self.closed = True


class FakeTicketCreationJobRepository:
    def __init__(self, session: FakeSession) -> None:
        self._state = session.state

    def _record(self, *call: object) -> None:
        assert self._state.active_transactions == 1
        self._state.repository_calls.append(call)

    def resume_or_claim_next(self, now: datetime) -> FakeAcquisition | None:
        self._record("resume_or_claim_next", now)
        if not self._state.eligible:
            return None
        return FakeAcquisition(self._state.job)

    def advance_after_transcription(
        self,
        job_uuid: UUID,
        now: datetime,
    ) -> FakeJob:
        self._record(
            "advance_after_transcription",
            job_uuid,
            now,
        )
        self._state.job.stage = JobStage.GLPI_CREATION
        return self._state.job

    def schedule_retry(
        self,
        job_uuid: UUID,
        *,
        stage: JobStage,
        error_scope: ErrorScope,
        error_category: str,
        error_code: str,
        next_attempt_at: datetime,
        now: datetime,
    ) -> FakeJob:
        self._record(
            "schedule_retry",
            job_uuid,
            stage,
            error_scope,
            error_category,
            error_code,
            next_attempt_at,
            now,
        )
        self._state.eligible = False
        self._state.job.status = JobStatus.RETRY_SCHEDULED
        self._state.job.consecutive_retry_count += 1
        return self._state.job

    def record_ticket_confirmed(
        self,
        job_uuid: UUID,
        ticket_id: int,
        now: datetime,
    ) -> FakeJob:
        self._record("record_ticket_confirmed", job_uuid, ticket_id, now)
        self._state.job.glpi_ticket_id = ticket_id
        self._state.job.stage = JobStage.FINALIZATION
        return self._state.job

    def mark_completed(
        self,
        job_uuid: UUID,
        *,
        now: datetime,
    ) -> FakeJob:
        self._record("mark_completed", job_uuid, now)
        self._state.eligible = False
        self._state.job.status = JobStatus.COMPLETED
        return self._state.job

    def mark_failed(
        self,
        job_uuid: UUID,
        *,
        stage: JobStage,
        error_category: str,
        error_code: str,
        now: datetime,
    ) -> FakeJob:
        self._record(
            "mark_failed",
            job_uuid,
            stage,
            error_category,
            error_code,
            now,
        )
        self._state.eligible = False
        self._state.job.status = JobStatus.FAILED
        return self._state.job

    def mark_blocked(
        self,
        job_uuid: UUID,
        *,
        stage: JobStage,
        error_scope: ErrorScope,
        error_category: str,
        error_code: str,
        now: datetime,
    ) -> FakeJob:
        self._record(
            "mark_blocked",
            job_uuid,
            stage,
            error_scope,
            error_category,
            error_code,
            now,
        )
        self._state.eligible = False
        self._state.job.status = JobStatus.BLOCKED
        return self._state.job


class FakeMailIngestionRepository:
    def __init__(self, session: FakeSession) -> None:
        self._state = session.state

    def get_by_uuid(self, job_uuid: UUID) -> FakeMailIngestion | None:
        assert self._state.active_transactions == 1
        self._state.repository_calls.append(("get_ingestion_by_uuid", job_uuid))
        if self._state.source_received_at is None:
            return None
        return FakeMailIngestion(received_at=self._state.source_received_at)


class FakeTicketCreationJobEventRepository:
    def __init__(self, session: FakeSession) -> None:
        self._state = session.state

    def append(self, *args: object, **kwargs: object) -> None:
        assert self._state.active_transactions == 1
        self._state.event_calls.append((*args, kwargs))


class FakeAudioRetentionRepository:
    def __init__(self, session: FakeSession) -> None:
        self._state = session.state

    def activate(self, *args: object, **kwargs: object) -> None:
        assert self._state.active_transactions == 1
        self._state.retention_calls.append((*args, kwargs))


class FakeTranscriber:
    def __init__(self, state: FakeState, outcomes: list[object]) -> None:
        self._state = state
        self._outcomes = deque(outcomes)
        self.calls: list[Path] = []

    def transcribe(self, audio_path: Path) -> TranscriptionResult:
        assert self._state.active_transactions == 0
        self.calls.append(audio_path)
        outcome = self._outcomes.popleft()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome  # type: ignore[return-value]


class FakeGlpiClient:
    def __init__(self, state: FakeState, outcomes: list[object]) -> None:
        self._state = state
        self._outcomes = deque(outcomes)
        self.calls: list[object] = []

    def ensure_ticket(self, ticket: object) -> int:
        assert self._state.active_transactions == 0
        self.calls.append(ticket)
        outcome = self._outcomes.popleft()
        if isinstance(outcome, BaseException):
            raise outcome
        assert isinstance(outcome, int)
        return outcome


class OwnershipLostError(RuntimeError):
    pass


class FailOwnershipGuardOnCall:
    def __init__(self, failing_call: int) -> None:
        self._failing_call = failing_call
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1
        if self.calls == self._failing_call:
            raise OwnershipLostError("automation advisory lock was lost")


def build_worker(
    audio_root: Path,
    state: FakeState,
    transcriber: FakeTranscriber,
    glpi_client: FakeGlpiClient,
    *,
    audio_url_for: Callable[[UUID], str] | None = None,
    title_generator: object | None = None,
    ownership_guard: Callable[[], None] | None = None,
    on_job_started: Callable[[UUID], None] | None = None,
) -> AutomationWorker:
    lifecycle = AutomationJobLifecycleService(
        session_factory=lambda: FakeSession(state),  # type: ignore[arg-type]
        ownership_guard=ownership_guard,
        job_repository_factory=FakeTicketCreationJobRepository,  # type: ignore[arg-type]
        event_repository_factory=FakeTicketCreationJobEventRepository,  # type: ignore[arg-type]
        retention_repository_factory=FakeAudioRetentionRepository,  # type: ignore[arg-type]
        mail_ingestion_repository_factory=FakeMailIngestionRepository,  # type: ignore[arg-type]
    )
    transitions = JobTransitionService(
        lifecycle=lifecycle,
        retry_policy=RetryBackoffPolicy(initial_seconds=5, maximum_seconds=20),
        retention_policy=RetentionSchedulePolicy(
            completed_ttl=timedelta(days=90),
            failed_ttl=timedelta(days=30),
            blocked_ttl=timedelta(days=90),
            first_completed_check_after=timedelta(hours=6),
        ),
        system_probe_interval_seconds=60,
        now=lambda: NOW,
    )
    return AutomationWorker(
        lifecycle=lifecycle,
        audio_inspector=AudioInspector(audio_root),
        transcription_stage=TranscriptionStage(
            transcriber=transcriber,
            lifecycle=lifecycle,
            transitions=transitions,
            ownership_guard=ownership_guard,
        ),
        ticket_creation_stage=TicketCreationStage(
            glpi_client=glpi_client,
            lifecycle=lifecycle,
            transitions=transitions,
            audio_url_for=(
                audio_url_for
                if audio_url_for is not None
                else lambda audio_uuid: f"https://audio.test/audios/{audio_uuid}"
            ),
            ticket_defaults=TICKET_DEFAULTS,
            title_generator=(
                title_generator
                if title_generator is not None
                else DefaultTitleGenerator(TICKET_DEFAULTS.title)
            ),  # type: ignore[arg-type]
            ownership_guard=ownership_guard,
        ),
        transitions=transitions,
        idle_wait_seconds=1,
        on_job_started=on_job_started,
    )


def write_audio(audio_root: Path) -> Path:
    audio_root.mkdir(exist_ok=True)
    audio_path = audio_root / STORAGE_KEY
    audio_path.write_bytes(SYNTHETIC_AUDIO)
    return audio_path


def transcription_result(audio_path: Path) -> TranscriptionResult:
    return TranscriptionResult(
        audio_path=audio_path,
        audio_uuid=AUDIO_UUID,
        transcription=TRANSCRIPTION,
    )


def repository_call_names(state: FakeState) -> list[object]:
    return [call[0] for call in state.repository_calls]


def terminal_repository_calls(state: FakeState) -> list[object]:
    terminal_names = {"mark_completed", "mark_failed", "mark_blocked"}
    return [
        call_name
        for call_name in repository_call_names(state)
        if call_name in terminal_names
    ]
