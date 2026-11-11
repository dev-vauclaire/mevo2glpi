"""Unit contracts for atomic operator retries."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

import mevo2glpi_web.services.jobs.retry as retry_module
from mevo2glpi_database import (
    AudioExtension,
    AudioRetentionSnapshot,
    ErrorScope,
    JobSnapshot,
    JobStage,
    JobStatus,
    RetentionPolicy,
    RetentionStatus,
)
from mevo2glpi_web.services import (
    AudioStorageUnavailableError,
    JobPersistenceUnavailableError,
    JobRetryConflictError,
    JobView,
    RetryAudioJob,
)

AUDIO_UUID = UUID("8ce23f5b-4e31-49f1-ae23-4eacdbba48f8")
AUDIO = b"RIFFsynthetic-audio"
NOW = datetime(2026, 8, 28, 14, tzinfo=UTC)


@dataclass
class FakeReader:
    job: JobView | None
    calls: list[UUID] = field(default_factory=list)

    def execute(self, audio_uuid: UUID) -> JobView | None:
        self.calls.append(audio_uuid)
        return self.job


@dataclass
class FakeSession:
    job: JobSnapshot | None = None
    retention: AudioRetentionSnapshot | None = None
    event_exists: bool = False
    append_error: Exception | None = None
    commit_error: Exception | None = None
    calls: list[str] = field(default_factory=list)

    def begin(self) -> None:
        self.calls.append("begin")

    def commit(self) -> None:
        self.calls.append("commit")
        if self.commit_error is not None:
            raise self.commit_error

    def rollback(self) -> None:
        self.calls.append("rollback")

    def close(self) -> None:
        self.calls.append("close")


@dataclass
class FakeFactory:
    sessions: list[FakeSession]
    calls: int = 0

    def __call__(self) -> FakeSession:
        self.calls += 1
        return self.sessions.pop(0)


class FakeJobRepository:
    def __init__(self, session: FakeSession) -> None:
        self._session = session

    def rearm_for_operator_retry(
        self,
        _audio_uuid: UUID,
        *,
        now: datetime,
    ) -> JobSnapshot:
        self._session.calls.append("rearm_job")
        assert self._session.job is not None
        self._session.job = replace(
            self._session.job,
            status=JobStatus.PENDING,
            stage=None,
            consecutive_retry_count=0,
            next_attempt_at=None,
            last_error_scope=None,
            last_error_category=None,
            last_error_code=None,
            updated_at=now,
            finished_at=None,
        )
        return self._session.job

    def get_by_uuid(self, _audio_uuid: UUID) -> JobSnapshot | None:
        self._session.calls.append("get_job")
        return self._session.job


class FakeRetentionRepository:
    def __init__(self, session: FakeSession) -> None:
        self._session = session

    def deactivate_for_operator_retry(
        self,
        _audio_uuid: UUID,
        *,
        now: datetime,
    ) -> AudioRetentionSnapshot:
        self._session.calls.append("deactivate_retention")
        assert self._session.retention is not None
        self._session.retention = replace(
            self._session.retention,
            status=RetentionStatus.INACTIVE,
            hard_delete_at=None,
            next_check_at=None,
            updated_at=now,
        )
        return self._session.retention

    def get_by_job_uuid(
        self,
        _audio_uuid: UUID,
    ) -> AudioRetentionSnapshot | None:
        self._session.calls.append("get_retention")
        return self._session.retention


class FakeEventRepository:
    def __init__(self, session: FakeSession) -> None:
        self._session = session

    def append(self, *_args: object, **_kwargs: object) -> None:
        self._session.calls.append("append_event")
        if self._session.append_error is not None:
            raise self._session.append_error
        self._session.event_exists = True

    def exists(self, *_args: object, **_kwargs: object) -> bool:
        self._session.calls.append("event_exists")
        return self._session.event_exists


@pytest.fixture(autouse=True)
def fake_repositories(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        retry_module,
        "TicketCreationJobRepository",
        FakeJobRepository,
    )
    monkeypatch.setattr(
        retry_module,
        "AudioRetentionRepository",
        FakeRetentionRepository,
    )
    monkeypatch.setattr(
        retry_module,
        "TicketCreationJobEventRepository",
        FakeEventRepository,
    )


def test_retry_updates_job_retention_then_event_in_one_transaction(
    tmp_path: Path,
) -> None:
    root = _audio_root(tmp_path, AUDIO)
    session = FakeSession(job=_job_snapshot(), retention=_retention())
    service = _service(root, FakeReader(_job_view()), FakeFactory([session]))

    result = service.execute(AUDIO_UUID)

    assert result is not None
    assert result.state is JobStatus.PENDING
    assert session.calls == [
        "begin",
        "rearm_job",
        "deactivate_retention",
        "append_event",
        "commit",
        "close",
    ]
    assert session.job is not None
    assert session.job.glpi_ticket_id == 42
    assert session.job.attempt_count == 5
    assert session.job.consecutive_retry_count == 0
    assert session.retention is not None
    assert session.retention.status is RetentionStatus.INACTIVE


def test_event_failure_rolls_back_job_and_retention_together(tmp_path: Path) -> None:
    root = _audio_root(tmp_path, AUDIO)
    session = FakeSession(
        job=_job_snapshot(),
        retention=_retention(),
        append_error=RuntimeError("private database detail"),
    )
    service = _service(root, FakeReader(_job_view()), FakeFactory([session]))

    with pytest.raises(JobPersistenceUnavailableError) as captured_error:
        service.execute(AUDIO_UUID)

    assert captured_error.value.technical_code == "operator_retry_failed"
    assert session.calls[-2:] == ["rollback", "close"]
    assert "commit" not in session.calls


@pytest.mark.parametrize("audio", [None, b"short"])
def test_missing_or_wrong_sized_audio_never_opens_a_write_session(
    tmp_path: Path,
    audio: bytes | None,
) -> None:
    root = _audio_root(tmp_path, audio)
    factory = FakeFactory([])
    service = _service(root, FakeReader(_job_view()), factory)

    with pytest.raises(JobRetryConflictError):
        service.execute(AUDIO_UUID)

    assert factory.calls == 0


def test_storage_io_failure_never_opens_a_write_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _audio_root(tmp_path, AUDIO)
    factory = FakeFactory([])

    def fail_open(_audio_path: Path):
        raise OSError("private volume failure")

    monkeypatch.setattr(retry_module, "open_audio_stream", fail_open)

    with pytest.raises(AudioStorageUnavailableError) as captured_error:
        _service(root, FakeReader(_job_view()), factory).execute(AUDIO_UUID)

    assert "private volume failure" not in str(captured_error.value)
    assert factory.calls == 0


def test_non_terminal_job_is_rejected_before_audio_or_database_access(
    tmp_path: Path,
) -> None:
    root = _audio_root(tmp_path, None)
    factory = FakeFactory([])
    job = replace(_job_view(), state=JobStatus.PROCESSING, finished_at=None)

    with pytest.raises(JobRetryConflictError):
        _service(root, FakeReader(job), factory).execute(AUDIO_UUID)

    assert factory.calls == 0


def test_unknown_job_returns_none_without_inspecting_storage(tmp_path: Path) -> None:
    root = _audio_root(tmp_path, None)
    factory = FakeFactory([])

    result = _service(root, FakeReader(None), factory).execute(AUDIO_UUID)

    assert result is None
    assert factory.calls == 0


def test_lost_commit_response_is_reconciled_from_a_new_session(
    tmp_path: Path,
) -> None:
    root = _audio_root(tmp_path, AUDIO)
    write_session = FakeSession(
        job=_job_snapshot(),
        retention=_retention(),
        commit_error=RuntimeError("connection lost"),
    )
    reconciliation_session = FakeSession(
        job=_pending_snapshot(),
        retention=replace(
            _retention(),
            status=RetentionStatus.INACTIVE,
            hard_delete_at=None,
            next_check_at=None,
            updated_at=NOW,
        ),
        event_exists=True,
    )
    factory = FakeFactory([write_session, reconciliation_session])
    service = _service(root, FakeReader(_job_view()), factory)

    result = service.execute(AUDIO_UUID)

    assert result is not None
    assert result.state is JobStatus.PENDING
    assert write_session.calls[-2:] == ["commit", "close"]
    assert "rollback" not in write_session.calls
    assert reconciliation_session.calls == [
        "get_job",
        "get_retention",
        "event_exists",
        "close",
    ]


def test_unresolved_commit_response_is_sanitized(tmp_path: Path) -> None:
    root = _audio_root(tmp_path, AUDIO)
    write_session = FakeSession(
        job=_job_snapshot(),
        retention=_retention(),
        commit_error=RuntimeError("secret commit failure"),
    )
    reconciliation_session = FakeSession(
        job=_job_snapshot(),
        retention=_retention(),
        event_exists=False,
    )
    service = _service(
        root,
        FakeReader(_job_view()),
        FakeFactory([write_session, reconciliation_session]),
    )

    with pytest.raises(JobPersistenceUnavailableError) as captured_error:
        service.execute(AUDIO_UUID)

    assert captured_error.value.technical_code == "operator_retry_outcome_unknown"
    assert "secret commit failure" not in str(captured_error.value)


def _service(
    root: Path,
    reader: FakeReader,
    factory: FakeFactory,
) -> RetryAudioJob:
    return RetryAudioJob(
        audio_root=root,
        session_factory=factory,  # type: ignore[arg-type]
        get_audio_job=reader,
        now_factory=lambda: NOW,
    )


def _audio_root(tmp_path: Path, content: bytes | None) -> Path:
    root = tmp_path / "audios"
    root.mkdir()
    if content is not None:
        (root / f"{AUDIO_UUID}.wav").write_bytes(content)
    return root


def _job_view() -> JobView:
    snapshot = _job_snapshot()
    return JobView(
        audio_uuid=snapshot.uuid,
        state=snapshot.status,
        stage=snapshot.stage,
        audio_extension=snapshot.audio_extension,
        audio_size_bytes=snapshot.audio_size_bytes,
        glpi_ticket_id=snapshot.glpi_ticket_id,
        attempt_count=snapshot.attempt_count,
        consecutive_retry_count=snapshot.consecutive_retry_count,
        next_attempt_at=snapshot.next_attempt_at,
        last_error_scope=snapshot.last_error_scope,
        last_error_category=snapshot.last_error_category,
        last_error_code=snapshot.last_error_code,
        created_at=snapshot.created_at,
        first_started_at=snapshot.first_started_at,
        updated_at=snapshot.updated_at,
        finished_at=snapshot.finished_at,
        retention_status=RetentionStatus.SCHEDULED,
        audio_deleted_at=None,
    )


def _job_snapshot() -> JobSnapshot:
    return JobSnapshot(
        uuid=AUDIO_UUID,
        status=JobStatus.FAILED,
        stage=JobStage.FINALIZATION,
        storage_key=f"{AUDIO_UUID}.wav",
        audio_extension=AudioExtension.WAV,
        audio_size_bytes=len(AUDIO),
        glpi_ticket_id=42,
        attempt_count=5,
        consecutive_retry_count=3,
        next_attempt_at=None,
        last_error_scope=ErrorScope.JOB,
        last_error_category="validation",
        last_error_code="terminal_error",
        created_at=NOW - timedelta(hours=1),
        first_started_at=NOW - timedelta(minutes=50),
        updated_at=NOW - timedelta(minutes=1),
        finished_at=NOW - timedelta(minutes=1),
    )


def _pending_snapshot() -> JobSnapshot:
    return replace(
        _job_snapshot(),
        status=JobStatus.PENDING,
        stage=None,
        consecutive_retry_count=0,
        last_error_scope=None,
        last_error_category=None,
        last_error_code=None,
        updated_at=NOW,
        finished_at=None,
    )


def _retention() -> AudioRetentionSnapshot:
    return AudioRetentionSnapshot(
        job_uuid=AUDIO_UUID,
        policy=RetentionPolicy.DEADLINE_ONLY,
        status=RetentionStatus.SCHEDULED,
        hard_delete_at=NOW + timedelta(days=7),
        next_check_at=None,
        last_checked_at=None,
        last_glpi_status_id=None,
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
