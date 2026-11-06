"""Unit contracts for controlled completed-audio downloads."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

import mevo2glpi_web.services.jobs.downloads as downloads_module
from mevo2glpi_database import (
    AudioExtension,
    JobStage,
    JobStatus,
    RetentionStatus,
)
from mevo2glpi_web.services import (
    AudioPurgedError,
    AudioStorageInvariantError,
    AudioStorageUnavailableError,
    GetDownloadableAudio,
    JobView,
)

AUDIO_UUID = UUID("8ce23f5b-4e31-49f1-ae23-4eacdbba48f8")
AUDIO = b"RIFFsynthetic-audio"


class FakeReader:
    def __init__(self, job: JobView | None) -> None:
        self.job = job

    def execute(self, _audio_uuid: UUID) -> JobView | None:
        return self.job


@pytest.mark.parametrize(
    "retention_status",
    [RetentionStatus.DELETING, RetentionStatus.DELETED],
)
def test_deleting_or_deleted_audio_returns_a_purged_outcome(
    tmp_path: Path,
    retention_status: RetentionStatus,
) -> None:
    audio_root = _root(tmp_path)
    job = _job(retention_status=retention_status)
    (audio_root / f"{AUDIO_UUID}.wav").write_bytes(AUDIO)

    with pytest.raises(AudioPurgedError):
        GetDownloadableAudio(audio_root, FakeReader(job)).execute(AUDIO_UUID)


@pytest.mark.parametrize("file_bytes", [None, b"short"])
def test_missing_or_wrong_sized_completed_audio_is_an_invariant_error(
    tmp_path: Path,
    file_bytes: bytes | None,
) -> None:
    audio_root = _root(tmp_path)
    job = _job()
    if file_bytes is not None:
        (audio_root / f"{AUDIO_UUID}.wav").write_bytes(file_bytes)

    with pytest.raises(AudioStorageInvariantError):
        GetDownloadableAudio(audio_root, FakeReader(job)).execute(AUDIO_UUID)


@pytest.mark.parametrize(
    "retention_status",
    [None, RetentionStatus.INACTIVE],
)
def test_completed_audio_without_active_retention_is_an_invariant_error(
    tmp_path: Path,
    retention_status: RetentionStatus | None,
) -> None:
    audio_root = _root(tmp_path)
    job = _job(retention_status=retention_status)
    (audio_root / f"{AUDIO_UUID}.wav").write_bytes(AUDIO)

    with pytest.raises(AudioStorageInvariantError):
        GetDownloadableAudio(audio_root, FakeReader(job)).execute(AUDIO_UUID)


def test_storage_io_failure_is_reported_as_temporarily_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audio_root = _root(tmp_path)
    (audio_root / f"{AUDIO_UUID}.wav").write_bytes(AUDIO)

    def fail_open(_audio_path: Path):
        raise OSError("private volume failure")

    monkeypatch.setattr(downloads_module, "_open_audio_stream", fail_open)

    with pytest.raises(AudioStorageUnavailableError) as captured_error:
        GetDownloadableAudio(audio_root, FakeReader(_job())).execute(AUDIO_UUID)

    assert "private volume failure" not in str(captured_error.value)


def test_known_non_completed_audio_is_not_downloadable(tmp_path: Path) -> None:
    audio_root = _root(tmp_path)
    job = _job(state=JobStatus.PROCESSING, retention_status=None)
    (audio_root / f"{AUDIO_UUID}.wav").write_bytes(AUDIO)

    result = GetDownloadableAudio(audio_root, FakeReader(job)).execute(AUDIO_UUID)

    assert result is None


@pytest.mark.parametrize(
    "retention_status",
    [
        RetentionStatus.SCHEDULED,
        RetentionStatus.CHECKING,
        RetentionStatus.RETRY_SCHEDULED,
        RetentionStatus.BLOCKED,
    ],
)
def test_active_retention_download_uses_size_and_closes_after_iteration(
    tmp_path: Path,
    retention_status: RetentionStatus,
) -> None:
    audio_root = _root(tmp_path)
    (audio_root / f"{AUDIO_UUID}.wav").write_bytes(AUDIO)

    downloadable = GetDownloadableAudio(
        audio_root,
        FakeReader(_job(retention_status=retention_status)),
    ).execute(AUDIO_UUID)

    assert downloadable is not None
    assert b"".join(downloadable.iter_bytes()) == AUDIO
    assert downloadable._stream.closed


def _root(tmp_path: Path) -> Path:
    audio_root = tmp_path / "audios"
    audio_root.mkdir()
    return audio_root


def _job(
    *,
    state: JobStatus = JobStatus.COMPLETED,
    retention_status: RetentionStatus | None = RetentionStatus.SCHEDULED,
) -> JobView:
    now = datetime(2026, 8, 28, 14, tzinfo=UTC)
    return JobView(
        audio_uuid=AUDIO_UUID,
        state=state,
        stage=JobStage.FINALIZATION,
        audio_extension=AudioExtension.WAV,
        audio_size_bytes=len(AUDIO),
        glpi_ticket_id=42,
        attempt_count=1,
        consecutive_retry_count=0,
        next_attempt_at=None,
        last_error_scope=None,
        last_error_category=None,
        last_error_code=None,
        created_at=now,
        first_started_at=now,
        updated_at=now,
        finished_at=now if state is JobStatus.COMPLETED else None,
        retention_status=retention_status,
        audio_deleted_at=None,
    )
