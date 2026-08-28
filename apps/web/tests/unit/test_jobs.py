from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from uuid import UUID

import pytest

import glpi_ticketing_web.services.jobs as jobs
from glpi_ticketing_database import (
    AudioExtension,
    JobSnapshot,
    JobStage,
    JobStatus,
    PendingJob,
)
from glpi_ticketing_web.services import (
    AudioStorageConflictError,
    EmptyAudioError,
    GetAudioJob,
    JobPersistenceUnavailableError,
    JobQueryUnavailableError,
    SubmitAudioJob,
    UnsupportedAudioUploadError,
    UploadTooLargeError,
)

AUDIO_UUID = UUID("8ce23f5b-4e31-49f1-ae23-4eacdbba48f8")
AUDIO_BYTES = b"RIFFsynthetic-audio"


@dataclass
class FakeSession:
    add_error: Exception | None = None
    commit_error: Exception | None = None
    get_error: Exception | None = None
    snapshot: JobSnapshot | None = None
    pending_job: PendingJob | None = None
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
class FakeSessionFactory:
    sessions: list[FakeSession]
    calls: int = 0

    def __call__(self) -> FakeSession:
        self.calls += 1
        if not self.sessions:
            raise AssertionError("Unexpected session request")
        return self.sessions.pop(0)


class FakeJobRepository:
    def __init__(self, session: FakeSession) -> None:
        self._session = session

    def add_pending(self, pending_job: PendingJob) -> JobSnapshot:
        self._session.calls.append("add_pending")
        self._session.pending_job = pending_job
        if self._session.add_error is not None:
            raise self._session.add_error
        return _snapshot(pending_job)

    def get_by_uuid(self, _audio_uuid: UUID) -> JobSnapshot | None:
        self._session.calls.append("get_by_uuid")
        if self._session.get_error is not None:
            raise self._session.get_error
        return self._session.snapshot


@pytest.fixture(autouse=True)
def fake_repository(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jobs, "JobRepository", FakeJobRepository)


def test_success_publishes_audio_before_transaction_and_persists_exact_metadata(
    tmp_path: Path,
) -> None:
    audio_root = _audio_root(tmp_path)
    session = FakeSession()
    factory = FakeSessionFactory([session])

    class OrderingRepository(FakeJobRepository):
        def add_pending(self, pending_job: PendingJob) -> JobSnapshot:
            audio_path = audio_root / pending_job.storage_key
            assert audio_path.read_bytes() == AUDIO_BYTES
            assert not list(audio_root.glob("*.part"))
            return super().add_pending(pending_job)

    jobs.JobRepository = OrderingRepository  # type: ignore[misc]
    service = _service(audio_root, factory)

    result = service.execute("patient recording.WAV", BytesIO(AUDIO_BYTES))

    assert result.audio_uuid == AUDIO_UUID
    assert result.state is JobStatus.PENDING
    assert session.calls == ["begin", "add_pending", "commit", "close"]
    assert session.pending_job == PendingJob(
        uuid=AUDIO_UUID,
        storage_key=f"{AUDIO_UUID}.wav",
        audio_extension=AudioExtension.WAV,
        audio_size_bytes=len(AUDIO_BYTES),
    )
    assert (audio_root / f"{AUDIO_UUID}.wav").read_bytes() == AUDIO_BYTES
    assert "patient recording" not in next(audio_root.iterdir()).name


@pytest.mark.parametrize("filename", [None, "recording", "recording.flac"])
def test_prevalidate_rejects_an_unsupported_upload_filename(
    tmp_path: Path,
    filename: str | None,
) -> None:
    service = _service(_audio_root(tmp_path), FakeSessionFactory([]))

    with pytest.raises(UnsupportedAudioUploadError):
        service.prevalidate(filename)


def test_exact_upload_limit_is_accepted(tmp_path: Path) -> None:
    audio_root = _audio_root(tmp_path)
    session = FakeSession()
    service = _service(
        audio_root,
        FakeSessionFactory([session]),
        max_upload_bytes=len(AUDIO_BYTES),
    )

    service.execute("recording.wav", BytesIO(AUDIO_BYTES))

    assert (audio_root / f"{AUDIO_UUID}.wav").read_bytes() == AUDIO_BYTES


def test_oversized_upload_leaves_no_file_part_or_database_call(
    tmp_path: Path,
) -> None:
    audio_root = _audio_root(tmp_path)
    factory = FakeSessionFactory([])
    service = _service(audio_root, factory, max_upload_bytes=len(AUDIO_BYTES) - 1)

    with pytest.raises(UploadTooLargeError):
        service.execute("recording.wav", BytesIO(AUDIO_BYTES))

    assert factory.calls == 0
    assert not list(audio_root.iterdir())


def test_empty_upload_is_removed_before_database_call(tmp_path: Path) -> None:
    audio_root = _audio_root(tmp_path)
    factory = FakeSessionFactory([])
    service = _service(audio_root, factory)

    with pytest.raises(EmptyAudioError):
        service.execute("recording.wav", BytesIO())

    assert factory.calls == 0
    assert not list(audio_root.iterdir())


def test_existing_destination_is_never_overwritten(tmp_path: Path) -> None:
    audio_root = _audio_root(tmp_path)
    destination = audio_root / f"{AUDIO_UUID}.wav"
    destination.write_bytes(b"existing")
    service = _service(audio_root, FakeSessionFactory([]))

    with pytest.raises(AudioStorageConflictError):
        service.execute("recording.wav", BytesIO(AUDIO_BYTES))

    assert destination.read_bytes() == b"existing"
    assert not list(audio_root.glob("*.part"))


def test_failure_before_commit_rolls_back_and_compensates_audio(
    tmp_path: Path,
) -> None:
    audio_root = _audio_root(tmp_path)
    session = FakeSession(add_error=RuntimeError("private database detail"))
    service = _service(audio_root, FakeSessionFactory([session]))

    with pytest.raises(JobPersistenceUnavailableError) as captured_error:
        service.execute("recording.wav", BytesIO(AUDIO_BYTES))

    assert captured_error.value.technical_code == "database_write_failed"
    assert session.calls == ["begin", "add_pending", "rollback", "close"]
    assert not list(audio_root.iterdir())


def test_uncertain_commit_with_matching_job_keeps_audio_and_succeeds(
    tmp_path: Path,
) -> None:
    audio_root = _audio_root(tmp_path)
    pending = _pending_job()
    write_session = FakeSession(commit_error=RuntimeError("connection lost"))
    read_session = FakeSession(snapshot=_snapshot(pending, status=JobStatus.PROCESSING))
    service = _service(
        audio_root,
        FakeSessionFactory([write_session, read_session]),
    )

    result = service.execute("recording.wav", BytesIO(AUDIO_BYTES))

    assert result.state is JobStatus.PENDING
    assert (audio_root / pending.storage_key).read_bytes() == AUDIO_BYTES
    assert write_session.calls == ["begin", "add_pending", "commit", "close"]
    assert read_session.calls == ["get_by_uuid", "close"]


def test_uncertain_commit_with_no_visible_job_keeps_audio(
    tmp_path: Path,
) -> None:
    audio_root = _audio_root(tmp_path)
    write_session = FakeSession(commit_error=RuntimeError("connection lost"))
    read_session = FakeSession(snapshot=None)
    service = _service(
        audio_root,
        FakeSessionFactory([write_session, read_session]),
    )

    with pytest.raises(JobPersistenceUnavailableError) as captured_error:
        service.execute("recording.wav", BytesIO(AUDIO_BYTES))

    assert captured_error.value.technical_code == "database_outcome_unknown"
    assert (audio_root / f"{AUDIO_UUID}.wav").read_bytes() == AUDIO_BYTES


def test_audio_root_is_resolved_once_before_publication(tmp_path: Path) -> None:
    first_root = _audio_root(tmp_path / "first")
    second_root = _audio_root(tmp_path / "second")
    configured_root = tmp_path / "configured-audios"
    configured_root.symlink_to(first_root, target_is_directory=True)
    session = FakeSession()
    service = _service(configured_root, FakeSessionFactory([session]))

    configured_root.unlink()
    configured_root.symlink_to(second_root, target_is_directory=True)
    service.execute("recording.wav", BytesIO(AUDIO_BYTES))

    assert (first_root / f"{AUDIO_UUID}.wav").read_bytes() == AUDIO_BYTES
    assert not list(second_root.iterdir())


def test_unavailable_reconciliation_never_deletes_potentially_committed_audio(
    tmp_path: Path,
) -> None:
    audio_root = _audio_root(tmp_path)
    write_session = FakeSession(commit_error=RuntimeError("connection lost"))
    read_session = FakeSession(get_error=RuntimeError("database unavailable"))
    service = _service(
        audio_root,
        FakeSessionFactory([write_session, read_session]),
    )

    with pytest.raises(JobPersistenceUnavailableError) as captured_error:
        service.execute("recording.wav", BytesIO(AUDIO_BYTES))

    assert captured_error.value.technical_code == "database_outcome_unknown"
    assert (audio_root / f"{AUDIO_UUID}.wav").read_bytes() == AUDIO_BYTES


def test_compensation_failure_preserves_sanitized_primary_diagnostic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    audio_root = _audio_root(tmp_path)
    private_value = str(tmp_path / "private-database-password")
    session = FakeSession(add_error=RuntimeError(private_value))

    def fail_delete(_audio_root: Path, _storage_key: str) -> bool:
        raise OSError(private_value)

    monkeypatch.setattr(jobs, "delete_audio", fail_delete)
    service = _service(audio_root, FakeSessionFactory([session]))

    with (
        caplog.at_level("INFO"),
        pytest.raises(JobPersistenceUnavailableError) as captured_error,
    ):
        service.execute("recording.wav", BytesIO(AUDIO_BYTES))

    assert captured_error.value.technical_code == "audio_compensation_failed"
    assert private_value not in caplog.text
    assert str(audio_root) not in caplog.text
    assert "audio_compensation_failed" in caplog.text


def test_get_audio_job_closes_its_session() -> None:
    expected = _snapshot(_pending_job())
    session = FakeSession(snapshot=expected)

    result = GetAudioJob(FakeSessionFactory([session])).execute(AUDIO_UUID)  # type: ignore[arg-type]

    assert result == expected
    assert session.calls == ["get_by_uuid", "close"]


def test_get_audio_job_sanitizes_query_failure() -> None:
    session = FakeSession(get_error=RuntimeError("secret database URL"))

    with pytest.raises(JobQueryUnavailableError) as captured_error:
        GetAudioJob(FakeSessionFactory([session])).execute(AUDIO_UUID)  # type: ignore[arg-type]

    assert "secret database URL" not in str(captured_error.value)
    assert session.calls == ["get_by_uuid", "close"]


def _audio_root(tmp_path: Path) -> Path:
    audio_root = tmp_path / "audios"
    audio_root.mkdir(parents=True)
    return audio_root


def _service(
    audio_root: Path,
    session_factory: FakeSessionFactory,
    *,
    max_upload_bytes: int = 1024,
) -> SubmitAudioJob:
    return SubmitAudioJob(
        audio_root=audio_root,
        session_factory=session_factory,  # type: ignore[arg-type]
        max_upload_bytes=max_upload_bytes,
        uuid_factory=lambda: AUDIO_UUID,
    )


def _pending_job() -> PendingJob:
    return PendingJob(
        uuid=AUDIO_UUID,
        storage_key=f"{AUDIO_UUID}.wav",
        audio_extension=AudioExtension.WAV,
        audio_size_bytes=len(AUDIO_BYTES),
    )


def _snapshot(
    pending_job: PendingJob,
    *,
    status: JobStatus = JobStatus.PENDING,
) -> JobSnapshot:
    now = datetime(2026, 8, 27, 12, tzinfo=UTC)
    return JobSnapshot(
        uuid=pending_job.uuid,
        status=status,
        stage=None if status is JobStatus.PENDING else JobStage.TRANSCRIPTION,
        storage_key=pending_job.storage_key,
        audio_extension=pending_job.audio_extension,
        audio_size_bytes=pending_job.audio_size_bytes,
        glpi_ticket_id=None,
        transcription_provider_job_id=None,
        attempt_count=0,
        consecutive_retry_count=0,
        next_attempt_at=None,
        last_error_scope=None,
        last_error_category=None,
        last_error_code=None,
        created_at=now,
        first_started_at=None,
        updated_at=now,
        finished_at=None,
    )
