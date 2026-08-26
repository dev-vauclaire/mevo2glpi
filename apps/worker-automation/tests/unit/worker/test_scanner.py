from collections.abc import Iterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest

import glpi_ticketing_worker_automation.worker.processor as processor_module
from glpi_ticketing_worker_automation.worker import (
    AudioProcessor,
    IdleOutcome,
    WorkerDirectories,
    claim_next_audio,
)

PENDING_FILENAME = "00000000-0000-0000-0000-000000000001.wav"
PROCESSING_FILENAME = "ffffffff-ffff-ffff-ffff-ffffffffffff.ogg"
SYNTHETIC_AUDIO = b"RIFF\x10\x00\x00\x00WAVEfmt \x00\x00\x00\x00data"


class UnusedTranscriber:
    def transcribe(self, _audio_path: Path):
        raise AssertionError("an empty scanner must not transcribe")


class UnusedGlpiClient:
    def ensure_ticket(self, _ticket):
        raise AssertionError("an empty scanner must not contact GLPI")


@pytest.fixture
def audio_directories(tmp_path: Path) -> WorkerDirectories:
    directories = WorkerDirectories(
        pending=tmp_path / "pending",
        processing=tmp_path / "processing",
        completed=tmp_path / "completed",
        failed=tmp_path / "failed",
    )
    for directory in (
        directories.pending,
        directories.processing,
        directories.completed,
        directories.failed,
    ):
        directory.mkdir()
    return directories


def test_processing_file_has_priority_over_pending(
    audio_directories: WorkerDirectories,
) -> None:
    pending_audio = audio_directories.pending / PENDING_FILENAME
    processing_audio = audio_directories.processing / PROCESSING_FILENAME
    pending_audio.write_bytes(b"pending-audio")
    processing_audio.write_bytes(b"processing-audio")

    selected_audio = claim_next_audio(
        pending_directory=audio_directories.pending,
        processing_directory=audio_directories.processing,
    )

    assert selected_audio == processing_audio
    assert pending_audio.read_bytes() == b"pending-audio"
    assert processing_audio.read_bytes() == b"processing-audio"
    assert not (audio_directories.processing / PENDING_FILENAME).exists()


def test_pending_file_is_claimed_into_processing(
    audio_directories: WorkerDirectories,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pending_audio = audio_directories.pending / PENDING_FILENAME
    pending_audio.write_bytes(SYNTHETIC_AUDIO)
    expected_processing_audio = audio_directories.processing / PENDING_FILENAME
    real_move_audio = processor_module.audio_files.move_audio
    move_calls: list[tuple[Path, Path]] = []

    def tracked_move_audio(
        source: Path,
        destination_directory: Path,
        *,
        buffer_size: int = processor_module.audio_files.DEFAULT_BUFFER_SIZE,
    ) -> Path:
        move_calls.append((source, destination_directory))
        return real_move_audio(
            source,
            destination_directory,
            buffer_size=buffer_size,
        )

    monkeypatch.setattr(processor_module.audio_files, "move_audio", tracked_move_audio)

    claimed_audio = claim_next_audio(
        pending_directory=audio_directories.pending,
        processing_directory=audio_directories.processing,
    )

    assert claimed_audio == expected_processing_audio
    assert move_calls == [(pending_audio, audio_directories.processing)]
    assert expected_processing_audio.read_bytes() == SYNTHETIC_AUDIO
    assert not list(audio_directories.processing.glob("*.part"))


def test_pending_source_is_deleted_after_claim(
    audio_directories: WorkerDirectories,
) -> None:
    pending_audio = audio_directories.pending / PENDING_FILENAME
    pending_audio.write_bytes(SYNTHETIC_AUDIO)

    claimed_audio = claim_next_audio(
        pending_directory=audio_directories.pending,
        processing_directory=audio_directories.processing,
    )

    assert claimed_audio == audio_directories.processing / PENDING_FILENAME
    assert claimed_audio.read_bytes() == SYNTHETIC_AUDIO
    assert not pending_audio.exists()


def test_claim_reconciles_error_after_pending_source_unlink(
    audio_directories: WorkerDirectories,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pending_audio = audio_directories.pending / PENDING_FILENAME
    pending_audio.write_bytes(SYNTHETIC_AUDIO)
    expected_processing_audio = audio_directories.processing / PENDING_FILENAME
    real_unlink_source = processor_module.audio_files._unlink_source

    def unlink_then_raise(source: Path) -> None:
        real_unlink_source(source)
        raise OSError("synthetic error after pending source unlink")

    monkeypatch.setattr(
        processor_module.audio_files,
        "_unlink_source",
        unlink_then_raise,
    )

    claimed_audio = claim_next_audio(
        pending_directory=audio_directories.pending,
        processing_directory=audio_directories.processing,
    )

    assert claimed_audio == expected_processing_audio
    assert claimed_audio.read_bytes() == SYNTHETIC_AUDIO
    assert not pending_audio.exists()
    assert not list(audio_directories.processing.glob("*.part"))


def test_part_file_is_ignored(audio_directories: WorkerDirectories) -> None:
    pending_part = audio_directories.pending / (f"{PENDING_FILENAME}.{uuid4()}.part")
    processing_part = audio_directories.processing / (
        f"{PROCESSING_FILENAME}.{uuid4()}.part"
    )
    pending_part.write_bytes(b"partial-pending")
    processing_part.write_bytes(b"partial-processing")

    selected_audio = claim_next_audio(
        pending_directory=audio_directories.pending,
        processing_directory=audio_directories.processing,
    )

    assert selected_audio is None
    assert pending_part.read_bytes() == b"partial-pending"
    assert processing_part.read_bytes() == b"partial-processing"


def test_failed_directory_is_never_scanned(
    audio_directories: WorkerDirectories,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failed_audio = audio_directories.failed / PROCESSING_FILENAME
    failed_audio.write_bytes(SYNTHETIC_AUDIO)
    real_iterdir = Path.iterdir

    def guarded_iterdir(directory: Path) -> Iterator[Path]:
        if directory == audio_directories.failed:
            pytest.fail("The scanner must never inspect the failed directory")
        return real_iterdir(directory)

    monkeypatch.setattr(Path, "iterdir", guarded_iterdir)

    selected_audio = claim_next_audio(
        pending_directory=audio_directories.pending,
        processing_directory=audio_directories.processing,
    )

    assert selected_audio is None
    assert failed_audio.read_bytes() == SYNTHETIC_AUDIO


def test_run_once_returns_idle_outcome_when_empty(
    audio_directories: WorkerDirectories,
) -> None:
    processor = AudioProcessor(
        directories=audio_directories,
        transcriber=UnusedTranscriber(),
        glpi_client=UnusedGlpiClient(),
        audio_url_for=lambda audio_uuid: f"https://audio.example.test/{audio_uuid}",
        idle_wait_seconds=2.5,
    )

    outcome = processor.run_once()

    assert isinstance(outcome, IdleOutcome)
    assert outcome.wait_seconds == 2.5
    assert not hasattr(outcome, "audio_uuid")


def test_worker_directories_do_not_scan_failed_during_idle(
    audio_directories: WorkerDirectories,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failed_audio = audio_directories.failed / f"{UUID(int=1)}.wav"
    failed_audio.write_bytes(SYNTHETIC_AUDIO)
    real_iterdir = Path.iterdir

    def guarded_iterdir(directory: Path) -> Iterator[Path]:
        if directory == audio_directories.failed:
            pytest.fail("The processor must never inspect failed")
        return real_iterdir(directory)

    monkeypatch.setattr(Path, "iterdir", guarded_iterdir)
    processor = AudioProcessor(
        directories=audio_directories,
        transcriber=UnusedTranscriber(),
        glpi_client=UnusedGlpiClient(),
        audio_url_for=lambda audio_uuid: f"https://audio.example.test/{audio_uuid}",
    )

    assert isinstance(processor.run_once(), IdleOutcome)
