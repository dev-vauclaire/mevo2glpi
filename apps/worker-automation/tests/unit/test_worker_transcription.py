import json
import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from glpi_ticketing_common import audio_files
from glpi_ticketing_worker_automation.transcription import (
    JobSnapshot,
    JobStatus,
    JobUuid,
    ResponseLogEntry,
    Submission,
    TranscriptionOperation,
    TranscriptionProcessingFailed,
    TranscriptionProvider,
    TranscriptionResult,
    TranscriptionTransportError,
)
from glpi_ticketing_worker_automation.worker import run_forever, run_once

AUDIO_UUID = "123e4567-e89b-42d3-a456-426614174000"
AUDIO_FILENAME = f"{AUDIO_UUID}.m4a"
JOB_UUID = JobUuid("7a85ed93")
SYNTHETIC_AUDIO = b"synthetic-audio"
SYNTHETIC_TRANSCRIPTION = "Coucou je suis une transcription synthétique"
SYNTHETIC_SECRET = "synthetic-api-key-that-must-never-be-persisted"
FIXED_NOW = datetime(2026, 8, 27, 10, 15, 30, tzinfo=UTC)


class SimulatedProcessCrash(BaseException):
    """Interrupt processing without executing a controlled error transition."""


@dataclass(frozen=True, slots=True)
class AudioDirectories:
    pending: Path
    processing: Path
    failed: Path


class ScriptedTranscriptionClient:
    """Protocol-compatible client driven by explicit submit and polling events."""

    provider = TranscriptionProvider.LEGACY

    def __init__(
        self,
        *,
        submit_events: list[Submission | BaseException],
        job_events: list[JobSnapshot | BaseException],
    ) -> None:
        self._submit_events = deque(submit_events)
        self._job_events = deque(job_events)
        self.submitted_paths: list[Path] = []
        self.polled_job_uuids: list[JobUuid] = []

    def submit(self, audio_path: Path) -> Submission:
        self.submitted_paths.append(audio_path)
        event = _next_event(self._submit_events, "submission")
        if not isinstance(event, Submission):
            raise AssertionError("submission event must return Submission")
        return event

    def get_job(self, job_uuid: JobUuid) -> JobSnapshot:
        self.polled_job_uuids.append(job_uuid)
        event = _next_event(self._job_events, "polling")
        if not isinstance(event, JobSnapshot):
            raise AssertionError("polling event must return JobSnapshot")
        return event


@pytest.fixture
def audio_directories(tmp_path: Path) -> AudioDirectories:
    directories = AudioDirectories(
        pending=tmp_path / "pending",
        processing=tmp_path / "processing",
        failed=tmp_path / "failed",
    )
    directories.pending.mkdir()
    directories.processing.mkdir()
    directories.failed.mkdir()
    return directories


def test_successful_transcription_keeps_audio_in_processing(
    audio_directories: AudioDirectories,
) -> None:
    audio_path = _write_processing_audio(audio_directories)
    client = _successful_client()

    result = _run_once(audio_directories, client)

    assert result is not None
    assert str(result.audio_uuid) == AUDIO_UUID
    assert result.audio_path == audio_path
    assert result.transcription == SYNTHETIC_TRANSCRIPTION
    assert audio_path.read_bytes() == SYNTHETIC_AUDIO
    assert not list(audio_directories.failed.iterdir())


def test_unavailable_service_retries_same_audio(
    audio_directories: AudioDirectories,
) -> None:
    audio_path = _write_processing_audio(audio_directories)
    client = ScriptedTranscriptionClient(
        submit_events=[
            TranscriptionTransportError(
                TranscriptionProvider.LEGACY,
                TranscriptionOperation.SUBMIT,
            ),
            _submission(),
        ],
        job_events=[_completed_snapshot()],
    )
    sleep_calls: list[float] = []

    result = _run_once(audio_directories, client, sleep=sleep_calls.append)

    assert result is not None
    assert client.submitted_paths == [audio_path, audio_path]
    assert sleep_calls == [0.25]


def test_unavailable_service_never_moves_audio_to_failed(
    audio_directories: AudioDirectories,
) -> None:
    audio_path = _write_processing_audio(audio_directories)
    client = ScriptedTranscriptionClient(
        submit_events=[_submission(), _submission()],
        job_events=[
            TranscriptionTransportError(
                TranscriptionProvider.LEGACY,
                TranscriptionOperation.GET_JOB,
            ),
            _completed_snapshot(),
        ],
    )

    result = _run_once(audio_directories, client, sleep=lambda _seconds: None)

    assert result is not None
    assert audio_path.read_bytes() == SYNTHETIC_AUDIO
    assert client.submitted_paths == [audio_path, audio_path]
    assert not list(audio_directories.failed.iterdir())


def test_terminal_error_moves_audio_to_failed(
    audio_directories: AudioDirectories,
) -> None:
    audio_path = _write_processing_audio(audio_directories)
    client = _failed_client()

    with pytest.raises(TranscriptionProcessingFailed, match="remote_job_failed"):
        _run_once(audio_directories, client)

    assert not audio_path.exists()
    assert (audio_directories.failed / AUDIO_FILENAME).read_bytes() == SYNTHETIC_AUDIO
    assert (audio_directories.failed / f"{AUDIO_UUID}.error.json").is_file()


def test_terminal_error_removes_processing_only_after_failed_is_complete(
    audio_directories: AudioDirectories,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audio_path = _write_processing_audio(audio_directories)
    failed_audio = audio_directories.failed / AUDIO_FILENAME
    failed_sidecar = audio_directories.failed / f"{AUDIO_UUID}.error.json"
    real_unlink_source = audio_files._unlink_source
    deletion_observed = False

    def tracked_unlink_source(source: Path) -> None:
        nonlocal deletion_observed
        assert source == audio_path
        assert source.exists()
        assert failed_audio.read_bytes() == SYNTHETIC_AUDIO
        assert json.loads(failed_sidecar.read_text(encoding="utf-8"))["uuid"] == (
            AUDIO_UUID
        )
        assert not list(audio_directories.failed.glob("*.part"))
        deletion_observed = True
        real_unlink_source(source)

    monkeypatch.setattr(audio_files, "_unlink_source", tracked_unlink_source)

    with pytest.raises(TranscriptionProcessingFailed):
        _run_once(audio_directories, _failed_client())

    assert deletion_observed
    assert not audio_path.exists()


def test_error_sidecar_contains_no_transcription_or_secret(
    audio_directories: AudioDirectories,
) -> None:
    _write_processing_audio(audio_directories)
    unsafe_failure = JobSnapshot(
        job_uuid=JOB_UUID,
        status=JobStatus.FAILED,
        transcription=None,
        response_log=ResponseLogEntry(
            operation=TranscriptionOperation.GET_JOB,
            http_status=200,
            provider_status=SYNTHETIC_SECRET,
            job_status=SYNTHETIC_TRANSCRIPTION,
        ),
    )
    client = ScriptedTranscriptionClient(
        submit_events=[_submission()],
        job_events=[unsafe_failure],
    )

    with pytest.raises(TranscriptionProcessingFailed):
        _run_once(audio_directories, client)

    sidecar = audio_directories.failed / f"{AUDIO_UUID}.error.json"
    serialized_sidecar = sidecar.read_text(encoding="utf-8")
    payload = json.loads(serialized_sidecar)
    assert set(payload) == {"uuid", "step", "category", "code", "timestamp"}
    assert payload == {
        "uuid": AUDIO_UUID,
        "step": "transcription",
        "category": "terminal",
        "code": "remote_job_failed",
        "timestamp": "2026-08-27T10:15:30Z",
    }
    assert SYNTHETIC_TRANSCRIPTION not in serialized_sidecar
    assert SYNTHETIC_SECRET not in serialized_sidecar


def test_worker_logs_uuid_but_not_transcript(
    audio_directories: AudioDirectories,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _write_processing_audio(audio_directories)

    with caplog.at_level(
        logging.INFO,
        logger="glpi_ticketing_worker_automation.worker",
    ):
        result = _run_once(audio_directories, _successful_client())

    assert result is not None
    assert AUDIO_UUID in caplog.text
    assert SYNTHETIC_TRANSCRIPTION not in caplog.text


def test_restart_reprocesses_existing_processing_file(
    audio_directories: AudioDirectories,
) -> None:
    audio_path = _write_processing_audio(audio_directories)
    interrupted_client = ScriptedTranscriptionClient(
        submit_events=[_submission()],
        job_events=[SimulatedProcessCrash()],
    )

    with pytest.raises(SimulatedProcessCrash):
        _run_once(audio_directories, interrupted_client)

    assert audio_path.read_bytes() == SYNTHETIC_AUDIO
    assert not list(audio_directories.failed.iterdir())

    restarted_client = _successful_client()
    result = _run_once(audio_directories, restarted_client)

    assert result is not None
    assert result.audio_path == audio_path
    assert restarted_client.submitted_paths == [audio_path]


def test_run_forever_refuses_to_retranscribe_unhandled_success(
    audio_directories: AudioDirectories,
) -> None:
    audio_path = _write_processing_audio(audio_directories)
    client = _successful_client()

    with pytest.raises(RuntimeError, match="must move the successful audio"):
        run_forever(
            pending_directory=audio_directories.pending,
            processing_directory=audio_directories.processing,
            failed_directory=audio_directories.failed,
            transcription_client=client,
            handle_transcription=lambda _result: None,
            transcription_poll_interval_seconds=0.25,
            transcription_job_timeout_seconds=10.0,
            sleep=lambda _seconds: None,
            now=lambda: FIXED_NOW,
        )

    assert client.submitted_paths == [audio_path]
    assert audio_path.read_bytes() == SYNTHETIC_AUDIO


def _run_once(
    directories: AudioDirectories,
    client: ScriptedTranscriptionClient,
    *,
    sleep: Callable[[float], None] = lambda _seconds: None,
) -> TranscriptionResult | None:
    return run_once(
        pending_directory=directories.pending,
        processing_directory=directories.processing,
        failed_directory=directories.failed,
        transcription_client=client,
        transcription_poll_interval_seconds=0.25,
        transcription_job_timeout_seconds=10.0,
        sleep=sleep,
        now=lambda: FIXED_NOW,
    )


def _write_processing_audio(directories: AudioDirectories) -> Path:
    audio_path = directories.processing / AUDIO_FILENAME
    audio_path.write_bytes(SYNTHETIC_AUDIO)
    return audio_path


def _successful_client() -> ScriptedTranscriptionClient:
    return ScriptedTranscriptionClient(
        submit_events=[_submission()],
        job_events=[_completed_snapshot()],
    )


def _failed_client() -> ScriptedTranscriptionClient:
    return ScriptedTranscriptionClient(
        submit_events=[_submission()],
        job_events=[_failed_snapshot()],
    )


def _submission() -> Submission:
    return Submission(
        job_uuid=JOB_UUID,
        response_log=_response_log(TranscriptionOperation.SUBMIT, job_status=None),
    )


def _completed_snapshot() -> JobSnapshot:
    return JobSnapshot(
        job_uuid=JOB_UUID,
        status=JobStatus.COMPLETED,
        transcription=SYNTHETIC_TRANSCRIPTION,
        response_log=_response_log(
            TranscriptionOperation.GET_JOB,
            job_status="COMPLETED",
        ),
    )


def _failed_snapshot() -> JobSnapshot:
    return JobSnapshot(
        job_uuid=JOB_UUID,
        status=JobStatus.FAILED,
        transcription=None,
        response_log=_response_log(
            TranscriptionOperation.GET_JOB,
            job_status="FAILED",
        ),
    )


def _response_log(
    operation: TranscriptionOperation,
    *,
    job_status: str | None,
) -> ResponseLogEntry:
    return ResponseLogEntry(
        operation=operation,
        http_status=200,
        provider_status="success",
        job_status=job_status,
    )


def _next_event(events: deque[object], operation: str) -> object:
    if not events:
        raise AssertionError(f"unexpected {operation} call")
    event = events.popleft()
    if isinstance(event, BaseException):
        raise event
    return event
