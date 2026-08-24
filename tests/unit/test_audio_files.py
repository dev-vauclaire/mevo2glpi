import json
import os
from io import BytesIO
from pathlib import Path
from typing import BinaryIO

import pytest

from glpi_ticketing_automation import audio_files
from glpi_ticketing_automation.audio_files import (
    InvalidAudioFilenameError,
    UnsupportedAudioExtensionError,
    atomic_write,
    list_ready_audio_files,
    move_audio,
    move_to_failed,
    parse_audio_filename,
)

AUDIO_UUID = "123e4567-e89b-42d3-a456-426614174000"
AUDIO_FILENAME = f"{AUDIO_UUID}.wav"
SYNTHETIC_AUDIO = b"RIFF\x10\x00\x00\x00WAVEfmt \x00\x00\x00\x00data"


def test_atomic_write_leaves_no_part_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / AUDIO_FILENAME
    events: list[str] = []
    real_fsync = os.fsync
    real_fsync_directory = audio_files._fsync_directory
    real_replace = os.replace

    def tracked_fsync(file_descriptor: int) -> None:
        assert os.fstat(file_descriptor).st_size == len(SYNTHETIC_AUDIO)
        events.append("fsync")
        real_fsync(file_descriptor)

    def tracked_replace(source: Path, target: Path) -> None:
        assert events == ["fsync"]
        assert source.parent == tmp_path
        assert source.name.startswith(f"{AUDIO_FILENAME}.")
        assert source.name.endswith(".part")
        events.append("replace")
        real_replace(source, target)

    def tracked_fsync_directory(directory: Path) -> None:
        assert events == ["fsync", "replace"]
        assert directory == tmp_path
        events.append("directory_fsync")
        real_fsync_directory(directory)

    monkeypatch.setattr(audio_files, "_fsync_file", tracked_fsync)
    monkeypatch.setattr(audio_files, "_replace_file", tracked_replace)
    monkeypatch.setattr(audio_files, "_fsync_directory", tracked_fsync_directory)

    published_path = atomic_write(destination, BytesIO(SYNTHETIC_AUDIO))

    assert published_path == destination
    assert destination.read_bytes() == SYNTHETIC_AUDIO
    assert events == ["fsync", "replace", "directory_fsync"]
    assert not list(tmp_path.glob("*.part"))


def test_failed_write_does_not_publish_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / AUDIO_FILENAME

    def fail_directory_fsync(_directory: Path) -> None:
        raise OSError("injected directory fsync failure")

    monkeypatch.setattr(audio_files, "_fsync_directory", fail_directory_fsync)

    with pytest.raises(OSError, match="injected directory fsync failure"):
        atomic_write(destination, BytesIO(SYNTHETIC_AUDIO))

    assert not destination.exists()
    assert not list(tmp_path.glob("*.part"))


def test_cross_volume_copy_deletes_source_after_success(tmp_path: Path) -> None:
    source_directory = tmp_path / "pending"
    destination_directory = tmp_path / "processing"
    source_directory.mkdir()
    destination_directory.mkdir()
    source = source_directory / AUDIO_FILENAME
    source.write_bytes(SYNTHETIC_AUDIO)

    destination = move_audio(source, destination_directory)

    assert destination == destination_directory / AUDIO_FILENAME
    assert destination.read_bytes() == SYNTHETIC_AUDIO
    assert not source.exists()


def test_cross_volume_copy_preserves_source_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_directory = tmp_path / "pending"
    destination_directory = tmp_path / "processing"
    source_directory.mkdir()
    destination_directory.mkdir()
    source = source_directory / AUDIO_FILENAME
    source.write_bytes(SYNTHETIC_AUDIO)

    def fail_copy(
        _source: BinaryIO,
        destination: BinaryIO,
        _buffer_size: int,
    ) -> None:
        destination.write(b"partial")
        raise OSError("injected copy failure")

    monkeypatch.setattr(audio_files, "_copy_stream", fail_copy)

    with pytest.raises(OSError, match="injected copy failure"):
        move_audio(source, destination_directory)

    assert source.read_bytes() == SYNTHETIC_AUDIO
    assert not (destination_directory / AUDIO_FILENAME).exists()
    assert not list(destination_directory.glob("*.part"))


def test_source_unlink_failure_rolls_back_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_directory = tmp_path / "pending"
    destination_directory = tmp_path / "processing"
    source_directory.mkdir()
    destination_directory.mkdir()
    source = source_directory / AUDIO_FILENAME
    source.write_bytes(SYNTHETIC_AUDIO)
    destination = destination_directory / AUDIO_FILENAME

    def fail_source_unlink(path: Path) -> None:
        assert path == source
        assert destination.read_bytes() == SYNTHETIC_AUDIO
        raise PermissionError("injected unlink failure")

    monkeypatch.setattr(audio_files, "_unlink_source", fail_source_unlink)

    with pytest.raises(PermissionError, match="injected unlink failure"):
        move_audio(source, destination_directory)

    assert source.read_bytes() == SYNTHETIC_AUDIO
    assert not destination.exists()


def test_existing_destination_is_never_overwritten(tmp_path: Path) -> None:
    destination = tmp_path / AUDIO_FILENAME
    destination.write_bytes(b"existing-audio")

    with pytest.raises(FileExistsError):
        atomic_write(destination, BytesIO(SYNTHETIC_AUDIO))

    assert destination.read_bytes() == b"existing-audio"
    assert not list(tmp_path.glob("*.part"))


def test_failed_transition_publishes_audio_and_sidecar_before_deleting_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processing_directory = tmp_path / "processing"
    failed_directory = tmp_path / "failed"
    processing_directory.mkdir()
    failed_directory.mkdir()
    source = processing_directory / AUDIO_FILENAME
    source.write_bytes(SYNTHETIC_AUDIO)
    sidecar_payload = {
        "uuid": AUDIO_UUID,
        "step": "transcription",
        "category": "terminal",
    }
    expected_audio = failed_directory / AUDIO_FILENAME
    expected_sidecar = failed_directory / f"{AUDIO_UUID}.error.json"
    real_atomic_write = audio_files.atomic_write
    real_unlink_source = audio_files._unlink_source
    publication_order: list[Path] = []
    deletion_observed = False

    def tracked_atomic_write(
        destination: Path,
        stream: BinaryIO,
        *,
        buffer_size: int = audio_files.DEFAULT_BUFFER_SIZE,
    ) -> Path:
        published = real_atomic_write(
            destination,
            stream,
            buffer_size=buffer_size,
        )
        assert source.exists()
        publication_order.append(destination)
        return published

    def tracked_unlink_source(path: Path) -> None:
        nonlocal deletion_observed
        assert path == source
        assert expected_audio.read_bytes() == SYNTHETIC_AUDIO
        assert (
            json.loads(expected_sidecar.read_text(encoding="utf-8")) == sidecar_payload
        )
        deletion_observed = True
        real_unlink_source(path)

    monkeypatch.setattr(audio_files, "atomic_write", tracked_atomic_write)
    monkeypatch.setattr(audio_files, "_unlink_source", tracked_unlink_source)

    result = move_to_failed(source, failed_directory, sidecar_payload)

    assert result.audio_path == expected_audio
    assert result.sidecar_path == expected_sidecar
    assert publication_order == [expected_audio, expected_sidecar]
    assert deletion_observed
    assert not source.exists()


def test_failed_sidecar_write_failure_keeps_processing_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processing_directory = tmp_path / "processing"
    failed_directory = tmp_path / "failed"
    processing_directory.mkdir()
    failed_directory.mkdir()
    source = processing_directory / AUDIO_FILENAME
    source.write_bytes(SYNTHETIC_AUDIO)
    expected_audio = failed_directory / AUDIO_FILENAME
    expected_sidecar = failed_directory / f"{AUDIO_UUID}.error.json"
    real_atomic_write = audio_files.atomic_write
    call_count = 0

    class FailingSidecarReader(BytesIO):
        def __init__(self) -> None:
            super().__init__(b'{"partial":true}')
            self.read_count = 0

        def read(self, size: int = -1) -> bytes:
            self.read_count += 1
            if self.read_count > 1:
                raise OSError("injected sidecar failure")
            return super().read(size)

    def fail_sidecar_write(
        destination: Path,
        stream: BinaryIO,
        *,
        buffer_size: int = audio_files.DEFAULT_BUFFER_SIZE,
    ) -> Path:
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            return real_atomic_write(
                destination,
                FailingSidecarReader(),
                buffer_size=5,
            )
        return real_atomic_write(destination, stream, buffer_size=buffer_size)

    monkeypatch.setattr(audio_files, "atomic_write", fail_sidecar_write)

    with pytest.raises(OSError, match="injected sidecar failure"):
        move_to_failed(source, failed_directory, {"uuid": AUDIO_UUID})

    assert source.read_bytes() == SYNTHETIC_AUDIO
    assert not expected_audio.exists()
    assert not expected_sidecar.exists()
    assert not list(failed_directory.glob("*.part"))


def test_audio_uuid_and_extension_are_validated() -> None:
    for extension in ("wav", "mp3", "ogg", "m4a"):
        parsed = parse_audio_filename(f"{AUDIO_UUID.upper()}.{extension.upper()}")

        assert str(parsed.audio_uuid) == AUDIO_UUID
        assert parsed.extension == f".{extension}"
        assert parsed.filename == f"{AUDIO_UUID}.{extension}"

    with pytest.raises(UnsupportedAudioExtensionError):
        parse_audio_filename(f"{AUDIO_UUID}.flac")

    invalid_filenames = (
        "not-a-uuid.wav",
        f"{AUDIO_UUID}.wav.part",
        f"../{AUDIO_FILENAME}",
        f"{{{AUDIO_UUID}}}.wav",
    )
    for invalid_filename in invalid_filenames:
        with pytest.raises(InvalidAudioFilenameError):
            parse_audio_filename(invalid_filename)


def test_part_files_are_never_listed_as_ready(tmp_path: Path) -> None:
    ready_audio = tmp_path / AUDIO_FILENAME
    ready_audio.write_bytes(SYNTHETIC_AUDIO)
    (tmp_path / f"{AUDIO_FILENAME}.part").write_bytes(b"partial")
    (tmp_path / f"{AUDIO_UUID}.error.json").write_text("{}")
    (tmp_path / f"{AUDIO_UUID}.txt").write_text("not audio")
    (tmp_path / "123e4567-e89b-42d3-a456-426614174001.mp3").mkdir()

    assert list_ready_audio_files(tmp_path) == [ready_audio]


def test_streaming_copy_uses_a_bounded_buffer(tmp_path: Path) -> None:
    buffer_size = 7

    class TrackingReader(BytesIO):
        def __init__(self, payload: bytes) -> None:
            super().__init__(payload)
            self.requested_sizes: list[int] = []

        def read(self, size: int = -1) -> bytes:
            self.requested_sizes.append(size)
            return super().read(size)

    source = TrackingReader(SYNTHETIC_AUDIO * 8)
    destination = tmp_path / AUDIO_FILENAME

    atomic_write(destination, source, buffer_size=buffer_size)

    assert destination.read_bytes() == SYNTHETIC_AUDIO * 8
    assert source.requested_sizes
    assert set(source.requested_sizes) == {buffer_size}
    assert len(source.requested_sizes) > 2
