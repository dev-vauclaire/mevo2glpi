from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest

from glpi_ticketing_worker_automation import worker
from glpi_ticketing_worker_automation.worker import run_forever, run_once

PENDING_FILENAME = "00000000-0000-0000-0000-000000000001.wav"
PROCESSING_FILENAME = "ffffffff-ffff-ffff-ffff-ffffffffffff.ogg"
SYNTHETIC_AUDIO = b"RIFF\x10\x00\x00\x00WAVEfmt \x00\x00\x00\x00data"


@dataclass(frozen=True, slots=True)
class AudioDirectories:
    pending: Path
    processing: Path


@pytest.fixture
def audio_directories(tmp_path: Path) -> AudioDirectories:
    pending_directory = tmp_path / "pending"
    processing_directory = tmp_path / "processing"
    pending_directory.mkdir()
    processing_directory.mkdir()
    return AudioDirectories(
        pending=pending_directory,
        processing=processing_directory,
    )


def test_processing_file_has_priority_over_pending(
    audio_directories: AudioDirectories,
) -> None:
    pending_audio = audio_directories.pending / PENDING_FILENAME
    processing_audio = audio_directories.processing / PROCESSING_FILENAME
    pending_audio.write_bytes(b"pending-audio")
    processing_audio.write_bytes(b"processing-audio")

    selected_audio = run_once(
        pending_directory=audio_directories.pending,
        processing_directory=audio_directories.processing,
    )

    assert selected_audio == processing_audio
    assert pending_audio.read_bytes() == b"pending-audio"
    assert processing_audio.read_bytes() == b"processing-audio"
    assert not (audio_directories.processing / PENDING_FILENAME).exists()


def test_pending_file_is_claimed_into_processing(
    audio_directories: AudioDirectories,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pending_audio = audio_directories.pending / PENDING_FILENAME
    pending_audio.write_bytes(SYNTHETIC_AUDIO)
    expected_processing_audio = audio_directories.processing / PENDING_FILENAME
    real_move_audio = worker.audio_files.move_audio
    move_calls: list[tuple[Path, Path]] = []

    def tracked_move_audio(
        source: Path,
        destination_directory: Path,
        *,
        buffer_size: int = worker.audio_files.DEFAULT_BUFFER_SIZE,
    ) -> Path:
        move_calls.append((source, destination_directory))
        return real_move_audio(
            source,
            destination_directory,
            buffer_size=buffer_size,
        )

    monkeypatch.setattr(worker.audio_files, "move_audio", tracked_move_audio)

    claimed_audio = run_once(
        pending_directory=audio_directories.pending,
        processing_directory=audio_directories.processing,
    )

    assert claimed_audio == expected_processing_audio
    assert move_calls == [(pending_audio, audio_directories.processing)]
    assert expected_processing_audio.read_bytes() == SYNTHETIC_AUDIO
    assert not list(audio_directories.processing.glob("*.part"))


def test_pending_source_is_deleted_after_claim(
    audio_directories: AudioDirectories,
) -> None:
    pending_audio = audio_directories.pending / PENDING_FILENAME
    pending_audio.write_bytes(SYNTHETIC_AUDIO)

    claimed_audio = run_once(
        pending_directory=audio_directories.pending,
        processing_directory=audio_directories.processing,
    )

    assert claimed_audio == audio_directories.processing / PENDING_FILENAME
    assert claimed_audio.read_bytes() == SYNTHETIC_AUDIO
    assert not pending_audio.exists()


def test_part_file_is_ignored(audio_directories: AudioDirectories) -> None:
    pending_part = audio_directories.pending / (f"{PENDING_FILENAME}.{uuid4()}.part")
    processing_part = audio_directories.processing / (
        f"{PROCESSING_FILENAME}.{uuid4()}.part"
    )
    pending_part.write_bytes(b"partial-pending")
    processing_part.write_bytes(b"partial-processing")

    selected_audio = run_once(
        pending_directory=audio_directories.pending,
        processing_directory=audio_directories.processing,
    )

    assert selected_audio is None
    assert pending_part.read_bytes() == b"partial-pending"
    assert processing_part.read_bytes() == b"partial-processing"


def test_failed_directory_is_never_scanned(
    audio_directories: AudioDirectories,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failed_directory = tmp_path / "failed"
    failed_directory.mkdir()
    failed_audio = failed_directory / PROCESSING_FILENAME
    failed_audio.write_bytes(SYNTHETIC_AUDIO)
    real_iterdir = Path.iterdir

    def guarded_iterdir(directory: Path) -> Iterator[Path]:
        if directory == failed_directory:
            pytest.fail("The scanner must never inspect the failed directory")
        return real_iterdir(directory)

    monkeypatch.setattr(Path, "iterdir", guarded_iterdir)

    selected_audio = run_once(
        pending_directory=audio_directories.pending,
        processing_directory=audio_directories.processing,
    )

    assert selected_audio is None
    assert failed_audio.read_bytes() == SYNTHETIC_AUDIO


def test_run_once_returns_none_when_empty(
    audio_directories: AudioDirectories,
) -> None:
    selected_audio = run_once(
        pending_directory=audio_directories.pending,
        processing_directory=audio_directories.processing,
    )

    assert selected_audio is None


def test_run_forever_uses_injected_sleep(
    audio_directories: AudioDirectories,
) -> None:
    class StopPolling(Exception):
        pass

    sleep_calls: list[float] = []

    def stop_after_first_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        raise StopPolling

    with pytest.raises(StopPolling):
        run_forever(
            pending_directory=audio_directories.pending,
            processing_directory=audio_directories.processing,
            poll_interval_seconds=2.5,
            sleep=stop_after_first_sleep,
        )

    assert sleep_calls == [2.5]
