import json
from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from glpi_ticketing_worker_automation.transcription import (
    JobSnapshot,
    JobStatus,
    JobUuid,
    ResponseLogEntry,
    Submission,
    TranscriptionHttpError,
    TranscriptionOperation,
    TranscriptionProvider,
    TranscriptionTransportError,
)
from glpi_ticketing_worker_automation.transcription.poller import (
    MAX_RESPONSE_LOGS,
    TranscriptionPoller,
    TranscriptionProcessingFailed,
    TranscriptionRetryRequired,
)

AUDIO_UUID = "123e4567-e89b-42d3-a456-426614174000"
AUDIO_FILENAME = f"{AUDIO_UUID}.m4a"
JOB_UUID = JobUuid("7a85ed93")
SYNTHETIC_AUDIO = b"synthetic-audio"
SYNTHETIC_TRANSCRIPTION = "Coucou je suis une transcription synthétique"


class SimulatedProcessCrash(BaseException):
    """Interrupt one attempt without running the poller's failure transition."""


class FakeClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 8, 25, 14, 0, tzinfo=UTC)
        self.sleep_calls: list[float] = []

    def now(self) -> datetime:
        return self.current

    def sleep(self, seconds: float) -> None:
        self.sleep_calls.append(seconds)
        self.current += timedelta(seconds=seconds)


class FakeTranscriptionClient:
    provider = TranscriptionProvider.LEGACY

    def __init__(
        self,
        *,
        events: list[JobSnapshot | BaseException],
        submit_error: Exception | None = None,
    ) -> None:
        self.events = deque(events)
        self.submit_error = submit_error
        self.submit_calls = 0
        self.get_job_calls = 0

    def submit(self, audio_path: Path) -> Submission:
        self.submit_calls += 1
        assert audio_path.name == AUDIO_FILENAME
        if self.submit_error is not None:
            raise self.submit_error
        return Submission(
            job_uuid=JOB_UUID,
            response_log=_response_log(
                TranscriptionOperation.SUBMIT,
                job_status=None,
            ),
        )

    def get_job(self, job_uuid: JobUuid) -> JobSnapshot:
        self.get_job_calls += 1
        assert job_uuid == JOB_UUID
        if not self.events:
            raise AssertionError("unexpected polling call")
        event = self.events.popleft()
        if isinstance(event, BaseException):
            raise event
        return event


@pytest.fixture
def audio_directories(tmp_path: Path) -> tuple[Path, Path]:
    processing_directory = tmp_path / "processing"
    failed_directory = tmp_path / "failed"
    processing_directory.mkdir()
    failed_directory.mkdir()
    return processing_directory, failed_directory


def test_success_returns_transcription_without_operational_sidecar(
    audio_directories: tuple[Path, Path],
) -> None:
    processing_directory, failed_directory = audio_directories
    audio_path = _write_audio(processing_directory)
    client = FakeTranscriptionClient(events=[_completed_snapshot()])
    poller = _make_poller(client, failed_directory, FakeClock())

    result = poller.transcribe(audio_path)

    assert result.audio_path == audio_path
    assert str(result.audio_uuid) == AUDIO_UUID
    assert result.transcription == SYNTHETIC_TRANSCRIPTION
    assert SYNTHETIC_TRANSCRIPTION not in repr(result)
    assert client.submit_calls == 1
    assert client.get_job_calls == 1
    assert list(processing_directory.iterdir()) == [audio_path]


def test_each_call_submits_a_new_job_for_an_audio_still_in_processing(
    audio_directories: tuple[Path, Path],
) -> None:
    processing_directory, failed_directory = audio_directories
    audio_path = _write_audio(processing_directory)
    client = FakeTranscriptionClient(
        events=[_completed_snapshot(), _completed_snapshot()]
    )
    poller = _make_poller(client, failed_directory, FakeClock())

    first_result = poller.transcribe(audio_path)
    second_result = poller.transcribe(audio_path)

    assert first_result.transcription == SYNTHETIC_TRANSCRIPTION
    assert second_result.transcription == SYNTHETIC_TRANSCRIPTION
    assert client.submit_calls == 2
    assert client.get_job_calls == 2
    assert audio_path.read_bytes() == SYNTHETIC_AUDIO


def test_interrupted_attempt_is_resubmitted_on_the_next_call(
    audio_directories: tuple[Path, Path],
) -> None:
    processing_directory, failed_directory = audio_directories
    audio_path = _write_audio(processing_directory)
    interrupted_client = FakeTranscriptionClient(events=[SimulatedProcessCrash()])
    interrupted_poller = _make_poller(
        interrupted_client,
        failed_directory,
        FakeClock(),
    )

    with pytest.raises(SimulatedProcessCrash):
        interrupted_poller.transcribe(audio_path)

    assert interrupted_client.submit_calls == 1
    assert audio_path.read_bytes() == SYNTHETIC_AUDIO
    assert list(processing_directory.iterdir()) == [audio_path]

    restarted_client = FakeTranscriptionClient(events=[_completed_snapshot()])
    restarted_poller = _make_poller(
        restarted_client,
        failed_directory,
        FakeClock(),
    )

    result = restarted_poller.transcribe(audio_path)

    assert result.transcription == SYNTHETIC_TRANSCRIPTION
    assert restarted_client.submit_calls == 1
    assert restarted_client.get_job_calls == 1


def test_pending_and_processing_are_polled_with_injected_sleep(
    audio_directories: tuple[Path, Path],
) -> None:
    processing_directory, failed_directory = audio_directories
    audio_path = _write_audio(processing_directory)
    client = FakeTranscriptionClient(
        events=[
            _snapshot(JobStatus.PENDING),
            _snapshot(JobStatus.PROCESSING),
            _completed_snapshot(),
        ]
    )
    clock = FakeClock()
    poller = _make_poller(client, failed_directory, clock)

    result = poller.transcribe(audio_path)

    assert result.transcription == SYNTHETIC_TRANSCRIPTION
    assert client.submit_calls == 1
    assert client.get_job_calls == 3
    assert clock.sleep_calls == [1.0, 1.0]


def test_post_transport_error_is_terminal_and_never_retried(
    audio_directories: tuple[Path, Path],
) -> None:
    processing_directory, failed_directory = audio_directories
    audio_path = _write_audio(processing_directory)
    client = FakeTranscriptionClient(
        events=[],
        submit_error=TranscriptionTransportError(
            TranscriptionProvider.LEGACY,
            TranscriptionOperation.SUBMIT,
        ),
    )
    clock = FakeClock()
    poller = _make_poller(client, failed_directory, clock)

    with pytest.raises(
        TranscriptionProcessingFailed,
        match="ambiguous_submission",
    ):
        poller.transcribe(audio_path)

    assert client.submit_calls == 1
    assert client.get_job_calls == 0
    assert clock.sleep_calls == []
    payload = _read_failure_payload(failed_directory)
    assert payload["code"] == "ambiguous_submission"
    assert "job_uuid" not in payload


def test_post_500_is_treated_as_an_ambiguous_submission(
    audio_directories: tuple[Path, Path],
) -> None:
    processing_directory, failed_directory = audio_directories
    audio_path = _write_audio(processing_directory)
    client = FakeTranscriptionClient(
        events=[],
        submit_error=TranscriptionHttpError(
            TranscriptionProvider.LEGACY,
            TranscriptionOperation.SUBMIT,
            500,
            retryable=False,
        ),
    )
    poller = _make_poller(client, failed_directory, FakeClock())

    with pytest.raises(
        TranscriptionProcessingFailed,
        match="ambiguous_submission",
    ):
        poller.transcribe(audio_path)

    payload = _read_failure_payload(failed_directory)
    assert payload["response_logs"] == [
        {
            "http_status": 500,
            "operation": TranscriptionOperation.SUBMIT.value,
        }
    ]


def test_ambiguous_get_leaves_audio_in_processing_for_a_fresh_submission(
    audio_directories: tuple[Path, Path],
) -> None:
    processing_directory, failed_directory = audio_directories
    audio_path = _write_audio(processing_directory)
    client = FakeTranscriptionClient(
        events=[
            TranscriptionTransportError(
                TranscriptionProvider.LEGACY,
                TranscriptionOperation.GET_JOB,
            ),
            _completed_snapshot(),
        ]
    )
    clock = FakeClock()
    poller = _make_poller(client, failed_directory, clock)

    with pytest.raises(
        TranscriptionRetryRequired,
        match="ambiguous_polling_response",
    ) as error_info:
        poller.transcribe(audio_path)

    assert client.submit_calls == 1
    assert client.get_job_calls == 1
    assert clock.sleep_calls == []
    assert error_info.value.audio_path == audio_path
    assert audio_path.read_bytes() == SYNTHETIC_AUDIO
    assert not list(failed_directory.iterdir())

    result = poller.transcribe(audio_path)

    assert result.transcription == SYNTHETIC_TRANSCRIPTION
    assert client.submit_calls == 2
    assert client.get_job_calls == 2


def test_retryable_get_http_error_leaves_audio_in_processing(
    audio_directories: tuple[Path, Path],
) -> None:
    processing_directory, failed_directory = audio_directories
    audio_path = _write_audio(processing_directory)
    client = FakeTranscriptionClient(
        events=[
            TranscriptionHttpError(
                TranscriptionProvider.LEGACY,
                TranscriptionOperation.GET_JOB,
                503,
                retryable=True,
            )
        ]
    )
    poller = _make_poller(client, failed_directory, FakeClock())

    with pytest.raises(TranscriptionRetryRequired):
        poller.transcribe(audio_path)

    assert audio_path.read_bytes() == SYNTHETIC_AUDIO
    assert not list(failed_directory.iterdir())


def test_polling_deadline_moves_audio_to_failed(
    audio_directories: tuple[Path, Path],
) -> None:
    processing_directory, failed_directory = audio_directories
    audio_path = _write_audio(processing_directory)
    client = FakeTranscriptionClient(
        events=[_snapshot(JobStatus.PENDING) for _ in range(3)]
    )
    clock = FakeClock()
    poller = _make_poller(
        client,
        failed_directory,
        clock,
        job_timeout_seconds=2.5,
    )

    with pytest.raises(
        TranscriptionProcessingFailed,
        match="polling_deadline_exceeded",
    ):
        poller.transcribe(audio_path)

    assert client.submit_calls == 1
    assert client.get_job_calls == 3
    assert clock.sleep_calls == [1.0, 1.0, 0.5]
    payload = _read_failure_payload(failed_directory)
    assert payload["code"] == "polling_deadline_exceeded"
    assert payload["timestamp"] == "2026-08-25T14:00:02.500000Z"


def test_remote_failure_moves_audio_with_sanitized_response_logs(
    audio_directories: tuple[Path, Path],
) -> None:
    processing_directory, failed_directory = audio_directories
    audio_path = _write_audio(processing_directory)
    client = FakeTranscriptionClient(events=[_snapshot(JobStatus.FAILED)])
    poller = _make_poller(client, failed_directory, FakeClock())

    with pytest.raises(TranscriptionProcessingFailed, match="remote_job_failed"):
        poller.transcribe(audio_path)

    payload = _read_failure_payload(failed_directory)
    serialized_payload = json.dumps(payload)
    assert payload["code"] == "remote_job_failed"
    assert payload["provider"] == "legacy"
    assert payload["job_uuid"] == JOB_UUID
    assert payload["response_logs"][-1]["job_status"] == "FAILED"
    assert "message" not in payload["response_logs"][-1]
    assert SYNTHETIC_TRANSCRIPTION not in serialized_payload
    assert "synthetic-api-key" not in serialized_payload
    assert not audio_path.exists()
    assert (failed_directory / AUDIO_FILENAME).read_bytes() == SYNTHETIC_AUDIO


def test_untrusted_status_values_are_sanitized_before_the_error_report(
    audio_directories: tuple[Path, Path],
) -> None:
    processing_directory, failed_directory = audio_directories
    audio_path = _write_audio(processing_directory)
    sensitive_status = f"{SYNTHETIC_TRANSCRIPTION} synthetic-api-key"
    unsafe_snapshot = JobSnapshot(
        job_uuid=JOB_UUID,
        status=JobStatus.FAILED,
        transcription=None,
        response_log=ResponseLogEntry(
            operation=sensitive_status,
            http_status=200,
            provider_status=sensitive_status,
            job_status=sensitive_status,
        ),
    )
    client = FakeTranscriptionClient(events=[unsafe_snapshot])
    poller = _make_poller(client, failed_directory, FakeClock())

    with pytest.raises(TranscriptionProcessingFailed, match="remote_job_failed"):
        poller.transcribe(audio_path)

    payload = _read_failure_payload(failed_directory)
    serialized_payload = json.dumps(payload)
    assert sensitive_status not in serialized_payload
    assert payload["response_logs"][-1] == {
        "http_status": 200,
        "job_status": "OTHER",
        "operation": TranscriptionOperation.UNKNOWN.value,
        "provider_status": "other",
    }


def test_response_history_is_bounded_until_terminal_failure(
    audio_directories: tuple[Path, Path],
) -> None:
    processing_directory, failed_directory = audio_directories
    audio_path = _write_audio(processing_directory)
    client = FakeTranscriptionClient(
        events=[
            *[_snapshot(JobStatus.PENDING) for _ in range(MAX_RESPONSE_LOGS + 2)],
            _snapshot(JobStatus.FAILED),
        ]
    )
    poller = _make_poller(
        client,
        failed_directory,
        FakeClock(),
        job_timeout_seconds=60.0,
    )

    with pytest.raises(TranscriptionProcessingFailed, match="remote_job_failed"):
        poller.transcribe(audio_path)

    response_logs = _read_failure_payload(failed_directory)["response_logs"]
    assert isinstance(response_logs, list)
    assert len(response_logs) == MAX_RESPONSE_LOGS
    assert all(
        log["operation"] == TranscriptionOperation.GET_JOB.value
        for log in response_logs
    )
    assert response_logs[-1]["job_status"] == "FAILED"


def test_failed_storage_error_preserves_processing_audio(
    audio_directories: tuple[Path, Path],
) -> None:
    processing_directory, failed_directory = audio_directories
    audio_path = _write_audio(processing_directory)
    existing_failed_audio = failed_directory / AUDIO_FILENAME
    existing_failed_audio.write_bytes(b"existing-failed-audio")
    client = FakeTranscriptionClient(events=[_snapshot(JobStatus.FAILED)])
    poller = _make_poller(client, failed_directory, FakeClock())

    with pytest.raises(FileExistsError):
        poller.transcribe(audio_path)

    assert audio_path.read_bytes() == SYNTHETIC_AUDIO
    assert existing_failed_audio.read_bytes() == b"existing-failed-audio"
    assert not (failed_directory / f"{AUDIO_UUID}.error.json").exists()


def _make_poller(
    client: FakeTranscriptionClient,
    failed_directory: Path,
    clock: FakeClock,
    *,
    job_timeout_seconds: float = 15.0,
) -> TranscriptionPoller:
    return TranscriptionPoller(
        client=client,
        failed_directory=failed_directory,
        poll_interval_seconds=1.0,
        job_timeout_seconds=job_timeout_seconds,
        sleep=clock.sleep,
        now=clock.now,
    )


def _write_audio(processing_directory: Path) -> Path:
    audio_path = processing_directory / AUDIO_FILENAME
    audio_path.write_bytes(SYNTHETIC_AUDIO)
    return audio_path


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


def _snapshot(status: JobStatus) -> JobSnapshot:
    return JobSnapshot(
        job_uuid=JOB_UUID,
        status=status,
        transcription=None,
        response_log=_response_log(
            TranscriptionOperation.GET_JOB,
            job_status=status.value.upper(),
        ),
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


def _read_failure_payload(failed_directory: Path) -> dict[str, object]:
    sidecar = failed_directory / f"{AUDIO_UUID}.error.json"
    return json.loads(sidecar.read_text(encoding="utf-8"))
