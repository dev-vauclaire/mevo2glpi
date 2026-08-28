"""Tests for canonical audio inspection in the worker pipeline."""

from pathlib import Path

import pytest

from glpi_ticketing_worker_automation.worker import BlockedOutcome

from ._pipeline_fakes import (
    SYNTHETIC_AUDIO,
    FakeGlpiClient,
    FakeJob,
    FakeState,
    FakeTranscriber,
    build_worker,
    terminal_repository_calls,
    write_audio,
)


def test_missing_canonical_audio_is_job_blocked_without_external_call(
    tmp_path: Path,
) -> None:
    state = FakeState()
    tmp_path.mkdir(exist_ok=True)
    transcriber = FakeTranscriber(state, [])
    glpi_client = FakeGlpiClient(state, [])
    worker = build_worker(tmp_path, state, transcriber, glpi_client)

    outcome = worker.run_once()

    assert isinstance(outcome, BlockedOutcome)
    assert outcome.failure.technical_code == "audio_missing_or_unsafe"
    assert transcriber.calls == []
    assert glpi_client.calls == []


def test_audio_inspection_error_is_job_blocked_without_external_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = FakeState()
    audio_path = write_audio(tmp_path)
    real_is_symlink = Path.is_symlink
    inspected_audio_calls = 0

    def fail_second_audio_inspection(path: Path) -> bool:
        nonlocal inspected_audio_calls
        if path == audio_path:
            inspected_audio_calls += 1
            if inspected_audio_calls == 2:
                raise OSError("sensitive filesystem diagnostic")
        return real_is_symlink(path)

    monkeypatch.setattr(Path, "is_symlink", fail_second_audio_inspection)
    transcriber = FakeTranscriber(state, [])
    glpi_client = FakeGlpiClient(state, [])
    worker = build_worker(tmp_path, state, transcriber, glpi_client)

    outcome = worker.run_once()

    assert isinstance(outcome, BlockedOutcome)
    assert outcome.failure.technical_code == "audio_inspection_failed"
    assert terminal_repository_calls(state) == ["mark_blocked"]
    assert transcriber.calls == []
    assert glpi_client.calls == []


def test_audio_size_mismatch_is_job_blocked_without_external_call(
    tmp_path: Path,
) -> None:
    state = FakeState(FakeJob(audio_size_bytes=len(SYNTHETIC_AUDIO) + 1))
    audio_path = write_audio(tmp_path)
    transcriber = FakeTranscriber(state, [])
    glpi_client = FakeGlpiClient(state, [])
    worker = build_worker(tmp_path, state, transcriber, glpi_client)

    outcome = worker.run_once()

    assert isinstance(outcome, BlockedOutcome)
    assert outcome.failure.technical_code == "audio_size_mismatch"
    assert terminal_repository_calls(state) == ["mark_blocked"]
    assert transcriber.calls == []
    assert glpi_client.calls == []
    assert audio_path.read_bytes() == SYNTHETIC_AUDIO
