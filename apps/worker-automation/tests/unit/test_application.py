from __future__ import annotations

import logging
import signal
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Event
from types import FrameType
from typing import cast
from uuid import UUID

import httpx
import pytest

from glpi_ticketing_worker_automation.application import (
    WorkerExitCode,
    build_processor,
    build_public_audio_url,
    run_application,
    run_cli,
    shutdown_signal_handlers,
    worker_runtime,
)
from glpi_ticketing_worker_automation.settings import WorkerSettings
from glpi_ticketing_worker_automation.transcription import (
    TranscriptionClient,
    TranscriptionPoller,
)
from glpi_ticketing_worker_automation.worker import (
    BlockedOutcome,
    FailureDetails,
    IdleOutcome,
    ProcessingStage,
    WorkerBlockedError,
)

AUDIO_UUID = UUID("123e4567-e89b-42d3-a456-426614174000")
AUDIO_FILENAME = f"{AUDIO_UUID}.m4a"


def _settings(tmp_path: Path, **overrides: object) -> WorkerSettings:
    values: dict[str, object] = {
        "public_base_url": "https://web.example.test/base/",
        "audio_pending_dir": tmp_path / "pending",
        "audio_processing_dir": tmp_path / "processing",
        "audio_completed_dir": tmp_path / "completed",
        "audio_failed_dir": tmp_path / "failed",
        "worker_scan_interval_seconds": 1.25,
        "legacy_transcription_base_url": "https://transcribe.example.test/prefix",
        "legacy_transcription_api_key": "synthetic-transcription-key",
        "transcription_http_timeout_seconds": 4.5,
        "transcription_poll_interval_seconds": 2.5,
        "transcription_job_timeout_seconds": 120.0,
        "glpi_base_url": "https://glpi.example.test/prefix",
        "glpi_username": "synthetic-worker",
        "glpi_password": "synthetic-password",
        "glpi_client_id": "synthetic-client-id",
        "glpi_client_secret": "synthetic-client-secret",
        "glpi_http_timeout_seconds": 5.5,
        "glpi_retry_interval_seconds": 3.5,
        "glpi_ticket_title": "Demande audio",
        "glpi_ticket_type": 2,
        "glpi_ticket_urgency": 4,
        "glpi_ticket_impact": 3,
    }
    values.update(overrides)
    for field_name in (
        "audio_pending_dir",
        "audio_processing_dir",
        "audio_completed_dir",
        "audio_failed_dir",
    ):
        directory = values[field_name]
        if isinstance(directory, Path):
            directory.mkdir(parents=True, exist_ok=True)
    return WorkerSettings(**values, _env_file=None)


def _passthrough_signals(_stop_event: Event) -> Iterator[None]:
    yield


def test_public_audio_url_preserves_base_path_without_double_slash(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)

    audio_url = build_public_audio_url(settings.public_base_url, AUDIO_UUID)

    assert audio_url == f"https://web.example.test/base/audios/{AUDIO_UUID}"


def test_build_processor_wires_all_settings_into_real_adapters(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    http_client = httpx.Client()
    transcription_sleeps: list[float] = []
    transcription_sleep = transcription_sleeps.append

    try:
        processor = build_processor(
            settings,
            http_client=http_client,
            transcription_sleep=transcription_sleep,
        )
    finally:
        http_client.close()

    assert processor._directories.pending == settings.audio_pending_dir
    assert processor._directories.processing == settings.audio_processing_dir
    assert processor._directories.completed == settings.audio_completed_dir
    assert processor._directories.failed == settings.audio_failed_dir
    assert processor._idle_wait_seconds == 1.25
    assert processor._transcription_retry_interval_seconds == 2.5
    assert processor._glpi_retry_interval_seconds == 3.5

    transcriber = cast(TranscriptionPoller, processor._transcriber)
    assert isinstance(transcriber, TranscriptionPoller)
    assert isinstance(transcriber._client, TranscriptionClient)
    assert transcriber._client._http_client is http_client
    assert transcriber._poll_interval_seconds == 2.5
    assert transcriber._job_timeout_seconds == 120.0
    assert transcriber._sleep is transcription_sleep

    assert processor._glpi_client._http_client is http_client
    assert processor._glpi_client._ticket_defaults.title == "Demande audio"
    assert int(processor._glpi_client._ticket_defaults.ticket_type) == 2
    assert int(processor._glpi_client._ticket_defaults.urgency) == 4
    assert int(processor._glpi_client._ticket_defaults.impact) == 3
    assert processor._audio_url_for(AUDIO_UUID) == (
        f"https://web.example.test/base/audios/{AUDIO_UUID}"
    )


def test_build_processor_resolves_relative_audio_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    settings = _settings(
        tmp_path,
        audio_pending_dir=Path("pending"),
        audio_processing_dir=Path("processing"),
        audio_completed_dir=Path("completed"),
        audio_failed_dir=Path("failed"),
    )

    with httpx.Client() as http_client:
        processor = build_processor(settings, http_client=http_client)

    assert processor._directories.pending == (tmp_path / "pending").resolve()
    assert processor._directories.processing == (tmp_path / "processing").resolve()
    assert processor._directories.completed == (tmp_path / "completed").resolve()
    assert processor._directories.failed == (tmp_path / "failed").resolve()


def test_worker_runtime_closes_http_client_after_success(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    http_client = httpx.Client()

    with worker_runtime(
        settings,
        http_client_factory=lambda: http_client,
    ) as processor:
        assert processor is not None
        assert not http_client.is_closed

    assert http_client.is_closed


def test_worker_runtime_closes_http_client_after_exception(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    http_client = httpx.Client()

    with pytest.raises(RuntimeError, match="synthetic runtime failure"):
        with worker_runtime(
            settings,
            http_client_factory=lambda: http_client,
        ):
            raise RuntimeError("synthetic runtime failure")

    assert http_client.is_closed


def test_run_application_stops_cooperatively_without_real_sleep(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    stop_event = Event()

    class StoppingProcessor:
        calls = 0

        def run_once(self) -> IdleOutcome:
            self.calls += 1
            stop_event.set()
            return IdleOutcome(wait_seconds=60.0)

    processor = StoppingProcessor()
    runtime_callbacks: dict[str, object] = {}

    @contextmanager
    def runtime(
        _settings: WorkerSettings,
        *,
        transcription_sleep: object,
        transcription_should_stop: object,
    ) -> Iterator[StoppingProcessor]:
        assert callable(transcription_sleep)
        assert callable(transcription_should_stop)
        runtime_callbacks["sleep"] = transcription_sleep
        runtime_callbacks["should_stop"] = transcription_should_stop
        yield processor

    run_application(
        settings,
        stop_event,
        runtime_factory=runtime,
    )

    assert processor.calls == 1
    should_stop = runtime_callbacks["should_stop"]
    assert callable(should_stop)
    assert should_stop()


def test_shutdown_signal_handlers_set_event_and_restore_previous_handlers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop_event = Event()
    registered_handlers: dict[signal.Signals, object] = {}
    calls: list[tuple[signal.Signals, object]] = []

    def fake_signal(
        handled_signal: signal.Signals,
        handler: object,
    ) -> object:
        calls.append((handled_signal, handler))
        previous_handler = registered_handlers.get(
            handled_signal,
            f"previous-{handled_signal.name}",
        )
        registered_handlers[handled_signal] = handler
        return previous_handler

    monkeypatch.setattr(signal, "signal", fake_signal)

    with shutdown_signal_handlers(stop_event):
        sigterm_handler = registered_handlers[signal.SIGTERM]
        assert callable(sigterm_handler)
        sigterm_handler(signal.SIGTERM, cast(FrameType | None, None))
        assert stop_event.is_set()

    assert len(calls) == 4
    assert registered_handlers[signal.SIGINT] == "previous-SIGINT"
    assert registered_handlers[signal.SIGTERM] == "previous-SIGTERM"


def test_run_cli_returns_blocked_code_with_sanitized_log(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = _settings(tmp_path)
    outcome = BlockedOutcome(
        audio_uuid=AUDIO_UUID,
        processing_path=tmp_path / "processing" / AUDIO_FILENAME,
        stage=ProcessingStage.TICKETING,
        failure=FailureDetails(
            category="authorization",
            technical_code="http_403",
        ),
    )

    def blocked_runner(_settings: WorkerSettings, _stop_event: Event) -> None:
        raise WorkerBlockedError(outcome)

    with caplog.at_level(logging.INFO):
        exit_code = run_cli(
            settings_factory=lambda: settings,
            application_runner=blocked_runner,
            signal_context_factory=contextmanager(_passthrough_signals),
        )

    assert exit_code is WorkerExitCode.BLOCKED
    assert str(AUDIO_UUID) in caplog.text
    assert "http_403" in caplog.text


def test_run_cli_does_not_log_invalid_configuration_secrets(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "never-log-this-password"

    def invalid_settings() -> WorkerSettings:
        return _settings(tmp_path, glpi_password=f"{secret}\n")

    with caplog.at_level(logging.INFO):
        exit_code = run_cli(settings_factory=invalid_settings)

    assert exit_code is WorkerExitCode.CONFIGURATION_ERROR
    assert secret not in caplog.text
    assert "worker configuration is invalid" in caplog.text


def test_run_cli_rejects_missing_runtime_directory_without_logging_its_path(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = _settings(tmp_path)
    missing_directory = settings.audio_pending_dir
    missing_directory.rmdir()

    with caplog.at_level(logging.INFO):
        exit_code = run_cli(
            settings_factory=lambda: settings,
            signal_context_factory=contextmanager(_passthrough_signals),
        )

    assert exit_code is WorkerExitCode.CONFIGURATION_ERROR
    assert str(missing_directory) not in caplog.text
    assert "worker runtime configuration is invalid" in caplog.text


def test_run_cli_does_not_log_unexpected_exception_message(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = _settings(tmp_path)
    secret = "never-log-this-api-key"

    def failing_runner(_settings: WorkerSettings, _stop_event: Event) -> None:
        raise RuntimeError(secret)

    with caplog.at_level(logging.INFO):
        exit_code = run_cli(
            settings_factory=lambda: settings,
            application_runner=failing_runner,
            signal_context_factory=contextmanager(_passthrough_signals),
        )

    assert exit_code is WorkerExitCode.UNEXPECTED_ERROR
    assert secret not in caplog.text
    assert "error_type=RuntimeError" in caplog.text


def test_run_cli_returns_success_after_cooperative_shutdown(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    @contextmanager
    def request_shutdown(stop_event: Event) -> Iterator[None]:
        stop_event.set()
        yield

    def assert_stopped(_settings: WorkerSettings, stop_event: Event) -> None:
        assert stop_event.is_set()

    exit_code = run_cli(
        settings_factory=lambda: settings,
        application_runner=assert_stopped,
        signal_context_factory=request_shutdown,
    )

    assert exit_code is WorkerExitCode.SUCCESS


def test_run_cli_returns_130_for_unhandled_keyboard_interrupt(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    def interrupt(_settings: WorkerSettings, _stop_event: Event) -> None:
        raise KeyboardInterrupt

    exit_code = run_cli(
        settings_factory=lambda: settings,
        application_runner=interrupt,
        signal_context_factory=contextmanager(_passthrough_signals),
    )

    assert exit_code is WorkerExitCode.INTERRUPTED
