"""Unit contracts for dashboard query use cases and opaque cursors."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

import glpi_ticketing_web.services.jobs.queries as queries
from glpi_ticketing_database import (
    AudioExtension,
    AudioRetentionSnapshot,
    ErrorScope,
    JobCursor,
    JobEventActor,
    JobEventCursor,
    JobEventPage,
    JobEventSnapshot,
    JobEventType,
    JobPage,
    JobSnapshot,
    JobStage,
    JobStatus,
    RetentionPolicy,
    RetentionStatus,
)
from glpi_ticketing_web.services import (
    GetAudioJob,
    InvalidCursorError,
    ListAudioJobEvents,
    ListAudioJobs,
)

AUDIO_UUID = UUID("8ce23f5b-4e31-49f1-ae23-4eacdbba48f8")
NOW = datetime(2026, 8, 28, 14, tzinfo=UTC)


@dataclass
class FakeSession:
    closed: bool = False

    def close(self) -> None:
        self.closed = True


@dataclass
class FakeFactory:
    session: FakeSession = field(default_factory=FakeSession)
    calls: int = 0

    def __call__(self) -> FakeSession:
        self.calls += 1
        return self.session


class FakeJobRepository:
    snapshots: dict[UUID, JobSnapshot] = {}
    pages: list[JobPage] = []
    list_calls: list[dict[str, object]] = []

    def __init__(self, _session: FakeSession) -> None:
        pass

    def get_by_uuid(self, audio_uuid: UUID) -> JobSnapshot | None:
        return self.snapshots.get(audio_uuid)

    def list_page(self, **kwargs: object) -> JobPage:
        self.list_calls.append(kwargs)
        return self.pages.pop(0)


class FakeRetentionRepository:
    snapshot: AudioRetentionSnapshot | None = None

    def __init__(self, _session: FakeSession) -> None:
        pass

    def get_by_job_uuid(self, _audio_uuid: UUID) -> AudioRetentionSnapshot | None:
        return self.snapshot


class FakeEventRepository:
    pages: list[JobEventPage] = []
    list_calls: list[dict[str, object]] = []

    def __init__(self, _session: FakeSession) -> None:
        pass

    def list_page(self, audio_uuid: UUID, **kwargs: object) -> JobEventPage:
        self.list_calls.append({"audio_uuid": audio_uuid, **kwargs})
        return self.pages.pop(0)


@pytest.fixture(autouse=True)
def fake_repositories(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeJobRepository.snapshots = {}
    FakeJobRepository.pages = []
    FakeJobRepository.list_calls = []
    FakeRetentionRepository.snapshot = None
    FakeEventRepository.pages = []
    FakeEventRepository.list_calls = []
    monkeypatch.setattr(queries, "TicketCreationJobRepository", FakeJobRepository)
    monkeypatch.setattr(queries, "AudioRetentionRepository", FakeRetentionRepository)
    monkeypatch.setattr(
        queries,
        "TicketCreationJobEventRepository",
        FakeEventRepository,
    )


def test_list_jobs_round_trips_filter_and_opaque_keyset_cursor() -> None:
    snapshot = _snapshot()
    cursor = JobCursor(updated_at=snapshot.updated_at, uuid=snapshot.uuid)
    FakeJobRepository.pages = [
        JobPage(items=(snapshot,), next_cursor=cursor, has_more=True),
        JobPage(items=(), next_cursor=None, has_more=False),
    ]
    factory = FakeFactory()
    service = ListAudioJobs(factory)  # type: ignore[arg-type]

    first_page = service.execute(
        limit=25,
        state="failed",
        audio_uuid=AUDIO_UUID,
    )
    second_page = service.execute(limit=25, cursor=first_page.next_cursor)

    assert first_page.items[0].audio_uuid == AUDIO_UUID
    assert first_page.next_cursor is not None
    assert str(AUDIO_UUID) not in first_page.next_cursor
    assert second_page.items == ()
    assert FakeJobRepository.list_calls == [
        {
            "limit": 25,
            "status": JobStatus.FAILED,
            "job_uuid": AUDIO_UUID,
            "cursor": None,
        },
        {
            "limit": 25,
            "status": None,
            "job_uuid": None,
            "cursor": cursor,
        },
    ]
    assert factory.calls == 2
    assert factory.session.closed


@pytest.mark.parametrize(
    "cursor",
    ["not-base64!", "e30", "a" * 513],
)
def test_invalid_job_cursor_is_rejected_before_opening_a_session(cursor: str) -> None:
    factory = FakeFactory()

    with pytest.raises(InvalidCursorError):
        ListAudioJobs(factory).execute(limit=25, cursor=cursor)  # type: ignore[arg-type]

    assert factory.calls == 0


def test_job_detail_maps_retention_without_sensitive_persistence_fields() -> None:
    snapshot = _snapshot()
    FakeJobRepository.snapshots[AUDIO_UUID] = snapshot
    FakeRetentionRepository.snapshot = _retention()

    job = GetAudioJob(FakeFactory()).execute(AUDIO_UUID)  # type: ignore[arg-type]

    assert job is not None
    assert job.retention_status is RetentionStatus.SCHEDULED
    assert job.audio_deleted_at is None
    exposed_fields = asdict(job)
    assert "storage_key" not in exposed_fields
    assert "transcription_provider_job_id" not in exposed_fields


def test_job_detail_sanitizes_repository_failure_and_closes_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingRepository:
        def __init__(self, _session: FakeSession) -> None:
            pass

        def get_by_uuid(self, _audio_uuid: UUID) -> JobSnapshot | None:
            raise RuntimeError("secret database URL")

    monkeypatch.setattr(queries, "TicketCreationJobRepository", FailingRepository)
    factory = FakeFactory()

    with pytest.raises(queries.JobQueryUnavailableError) as captured_error:
        GetAudioJob(factory).execute(AUDIO_UUID)  # type: ignore[arg-type]

    assert "secret database URL" not in str(captured_error.value)
    assert factory.session.closed


def test_event_history_returns_none_for_unknown_job_without_querying_events() -> None:
    result = ListAudioJobEvents(FakeFactory()).execute(  # type: ignore[arg-type]
        AUDIO_UUID,
        limit=25,
    )

    assert result is None
    assert FakeEventRepository.list_calls == []


def test_event_history_round_trips_its_separate_cursor() -> None:
    FakeJobRepository.snapshots[AUDIO_UUID] = _snapshot()
    event = _event()
    cursor = JobEventCursor(occurred_at=event.occurred_at, event_id=event.event_id)
    FakeEventRepository.pages = [
        JobEventPage(items=(event,), next_cursor=cursor, has_more=True),
        JobEventPage(items=(), next_cursor=None, has_more=False),
    ]
    service = ListAudioJobEvents(FakeFactory())  # type: ignore[arg-type]

    first_page = service.execute(AUDIO_UUID, limit=10)
    assert first_page is not None
    second_page = service.execute(
        AUDIO_UUID,
        limit=10,
        cursor=first_page.next_cursor,
    )

    assert first_page.items[0].technical_code == "remote_unavailable"
    assert second_page is not None
    assert second_page.items == ()
    assert FakeEventRepository.list_calls[1]["cursor"] == cursor


def test_job_cursor_cannot_be_used_as_an_event_cursor() -> None:
    FakeJobRepository.pages = [
        JobPage(
            items=(_snapshot(),),
            next_cursor=JobCursor(updated_at=NOW, uuid=AUDIO_UUID),
            has_more=True,
        )
    ]
    job_cursor = (
        ListAudioJobs(FakeFactory())
        .execute(  # type: ignore[arg-type]
            limit=25
        )
        .next_cursor
    )
    FakeJobRepository.snapshots[AUDIO_UUID] = _snapshot()

    with pytest.raises(InvalidCursorError):
        ListAudioJobEvents(FakeFactory()).execute(  # type: ignore[arg-type]
            AUDIO_UUID,
            limit=25,
            cursor=job_cursor,
        )


def test_event_cursor_rejects_an_identifier_outside_postgresql_bigint() -> None:
    oversized_cursor = queries._encode_cursor("events", NOW, 1 << 63)

    with pytest.raises(InvalidCursorError):
        ListAudioJobEvents(FakeFactory()).execute(  # type: ignore[arg-type]
            AUDIO_UUID,
            limit=25,
            cursor=oversized_cursor,
        )


def _snapshot() -> JobSnapshot:
    return JobSnapshot(
        uuid=AUDIO_UUID,
        status=JobStatus.FAILED,
        stage=JobStage.TRANSCRIPTION,
        storage_key=f"{AUDIO_UUID}.wav",
        audio_extension=AudioExtension.WAV,
        audio_size_bytes=20,
        glpi_ticket_id=42,
        transcription_provider_job_id="provider-private-id",
        attempt_count=3,
        consecutive_retry_count=2,
        next_attempt_at=None,
        last_error_scope=ErrorScope.JOB,
        last_error_category="invalid_audio",
        last_error_code="unsupported_codec",
        created_at=NOW - timedelta(hours=1),
        first_started_at=NOW - timedelta(minutes=50),
        updated_at=NOW,
        finished_at=NOW,
    )


def _retention() -> AudioRetentionSnapshot:
    return AudioRetentionSnapshot(
        job_uuid=AUDIO_UUID,
        policy=RetentionPolicy.DEADLINE_ONLY,
        status=RetentionStatus.SCHEDULED,
        hard_delete_at=NOW + timedelta(days=7),
        next_check_at=None,
        last_checked_at=None,
        last_glpi_status=None,
        check_attempt_count=0,
        consecutive_retry_count=0,
        deletion_reason=None,
        deletion_started_at=None,
        audio_deleted_at=None,
        last_error_category=None,
        last_error_code=None,
        created_at=NOW,
        updated_at=NOW,
    )


def _event() -> JobEventSnapshot:
    return JobEventSnapshot(
        event_id=7,
        job_uuid=AUDIO_UUID,
        occurred_at=NOW,
        status=JobStatus.RETRY_SCHEDULED,
        stage=JobStage.TRANSCRIPTION,
        event_type=JobEventType.RETRY_SCHEDULED,
        error_scope=ErrorScope.SYSTEM,
        error_category="availability",
        technical_code="remote_unavailable",
        actor=JobEventActor.WORKER_AUTOMATION,
    )
