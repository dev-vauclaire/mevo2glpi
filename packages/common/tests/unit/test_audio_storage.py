import os
from io import BytesIO
from pathlib import Path
from uuid import UUID

import pytest

from glpi_ticketing_common import audio_files
from glpi_ticketing_common.audio_files import (
    InvalidAudioFilenameError,
    InvalidAudioStorageKeyError,
    UnsupportedAudioExtensionError,
    build_audio_storage_key,
    delete_audio,
    parse_audio_filename,
    parse_audio_storage_key,
    publish_audio,
    resolve_audio_storage_path,
)

AUDIO_UUID = UUID("123e4567-e89b-42d3-a456-426614174000")
STORAGE_KEY = f"{AUDIO_UUID}.wav"
SYNTHETIC_AUDIO = b"RIFF\x10\x00\x00\x00WAVEfmt \x00\x00\x00\x00data"


@pytest.mark.parametrize("extension", ["wav", ".wav", "WAV", ".WAV"])
def test_build_audio_storage_key_normalizes_upload_extension(extension: str) -> None:
    assert build_audio_storage_key(AUDIO_UUID, extension) == STORAGE_KEY


@pytest.mark.parametrize(
    "storage_key",
    [
        "",
        "../123e4567-e89b-42d3-a456-426614174000.wav",
        "directory/123e4567-e89b-42d3-a456-426614174000.wav",
        "directory\\123e4567-e89b-42d3-a456-426614174000.wav",
        "/123e4567-e89b-42d3-a456-426614174000.wav",
        "123e4567-e89b-42d3-a456-426614174000",
        "123e4567-e89b-42d3-a456-426614174000.wav.part",
        "123e4567e89b42d3a456426614174000.wav",
        "123E4567-E89B-42D3-A456-426614174000.wav",
        "123e4567-e89b-42d3-a456-426614174000.WAV",
        "123e4567-e89b-42d3-a456-426614174000.flac",
    ],
)
def test_parse_audio_storage_key_rejects_non_canonical_values(
    storage_key: str,
) -> None:
    with pytest.raises(InvalidAudioStorageKeyError):
        parse_audio_storage_key(storage_key)


def test_parse_audio_storage_key_returns_typed_components() -> None:
    parsed = parse_audio_storage_key(STORAGE_KEY)

    assert parsed.audio_uuid == AUDIO_UUID
    assert parsed.extension == "wav"
    assert parsed.storage_key == STORAGE_KEY


@pytest.mark.parametrize("extension", ["", ".", "flac", "wav.part", "../wav"])
def test_build_audio_storage_key_rejects_unsupported_extension(
    extension: str,
) -> None:
    with pytest.raises(UnsupportedAudioExtensionError):
        build_audio_storage_key(AUDIO_UUID, extension)


def test_resolve_audio_storage_path_is_confined_to_resolved_root(
    tmp_path: Path,
) -> None:
    audio_root = tmp_path / "audio-root"
    audio_root.mkdir()

    path = resolve_audio_storage_path(audio_root, STORAGE_KEY)

    assert path == audio_root.resolve() / STORAGE_KEY
    assert path.parent == audio_root.resolve()


def test_publish_audio_returns_size_and_leaves_no_part_file(tmp_path: Path) -> None:
    published = publish_audio(tmp_path, STORAGE_KEY, BytesIO(SYNTHETIC_AUDIO))

    assert published.storage_key == STORAGE_KEY
    assert published.path == tmp_path.resolve() / STORAGE_KEY
    assert published.size_bytes == len(SYNTHETIC_AUDIO)
    assert published.path.read_bytes() == SYNTHETIC_AUDIO
    assert not list(tmp_path.glob("*.part"))


def test_publish_audio_accepts_an_empty_bounded_stream(tmp_path: Path) -> None:
    published = publish_audio(tmp_path, STORAGE_KEY, BytesIO(b""))

    assert published.size_bytes == 0
    assert published.path.read_bytes() == b""


def test_publish_audio_never_overwrites_a_concurrent_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / STORAGE_KEY
    competing_audio = b"competing-audio"
    real_replace_file = audio_files._replace_file

    def publish_competing_audio(part_path: Path, target_path: Path) -> None:
        assert target_path == destination
        destination.write_bytes(competing_audio)
        real_replace_file(part_path, target_path)

    monkeypatch.setattr(audio_files, "_replace_file", publish_competing_audio)

    with pytest.raises(FileExistsError):
        publish_audio(tmp_path, STORAGE_KEY, BytesIO(SYNTHETIC_AUDIO))

    assert destination.read_bytes() == competing_audio
    assert not list(tmp_path.glob("*.part"))


def test_delete_audio_synchronizes_the_root_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / STORAGE_KEY
    destination.write_bytes(SYNTHETIC_AUDIO)
    synchronized_directories: list[Path] = []
    real_fsync_directory = audio_files._fsync_directory

    def tracked_fsync_directory(directory: Path) -> None:
        synchronized_directories.append(directory)
        real_fsync_directory(directory)

    monkeypatch.setattr(audio_files, "_fsync_directory", tracked_fsync_directory)

    assert delete_audio(tmp_path, STORAGE_KEY) is True
    assert not destination.exists()
    assert synchronized_directories == [tmp_path.resolve()]


def test_delete_audio_is_idempotent_when_audio_is_absent(tmp_path: Path) -> None:
    assert delete_audio(tmp_path, STORAGE_KEY) is False


def test_resolve_audio_storage_path_rejects_a_symbolic_link(tmp_path: Path) -> None:
    outside = tmp_path / "outside.wav"
    outside.write_bytes(SYNTHETIC_AUDIO)
    audio_root = tmp_path / "audio-root"
    audio_root.mkdir()
    (audio_root / STORAGE_KEY).symlink_to(outside)

    with pytest.raises(InvalidAudioStorageKeyError):
        resolve_audio_storage_path(audio_root, STORAGE_KEY)

    assert outside.read_bytes() == SYNTHETIC_AUDIO
    assert (audio_root / STORAGE_KEY).is_symlink()


def test_publication_synchronizes_file_before_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    real_fsync = os.fsync
    real_fsync_directory = audio_files._fsync_directory

    def tracked_fsync(file_descriptor: int) -> None:
        events.append("file")
        real_fsync(file_descriptor)

    def tracked_fsync_directory(directory: Path) -> None:
        assert directory == tmp_path.resolve()
        events.append("directory")
        real_fsync_directory(directory)

    monkeypatch.setattr(audio_files, "_fsync_file", tracked_fsync)
    monkeypatch.setattr(audio_files, "_fsync_directory", tracked_fsync_directory)

    publish_audio(tmp_path, STORAGE_KEY, BytesIO(SYNTHETIC_AUDIO))

    assert events == ["file", "directory"]


def test_audio_filename_validation_remains_available_for_provider_adapter() -> None:
    for extension in ("wav", "mp3", "ogg", "m4a"):
        parsed = parse_audio_filename(f"{str(AUDIO_UUID).upper()}.{extension.upper()}")

        assert parsed.audio_uuid == AUDIO_UUID
        assert parsed.extension == f".{extension}"
        assert parsed.filename == f"{AUDIO_UUID}.{extension}"

    with pytest.raises(UnsupportedAudioExtensionError):
        parse_audio_filename(f"{AUDIO_UUID}.flac")

    for invalid_filename in (
        "not-a-uuid.wav",
        f"{AUDIO_UUID}.wav.part",
        f"../{STORAGE_KEY}",
        f"{{{AUDIO_UUID}}}.wav",
    ):
        with pytest.raises(InvalidAudioFilenameError):
            parse_audio_filename(invalid_filename)


def test_publication_rolls_back_after_directory_fsync_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_directory_fsync(_directory: Path) -> None:
        raise OSError("injected directory fsync failure")

    monkeypatch.setattr(audio_files, "_fsync_directory", fail_directory_fsync)

    with pytest.raises(OSError, match="injected directory fsync failure"):
        publish_audio(tmp_path, STORAGE_KEY, BytesIO(SYNTHETIC_AUDIO))

    assert not (tmp_path / STORAGE_KEY).exists()
    assert not list(tmp_path.glob("*.part"))


def test_publication_uses_a_bounded_streaming_buffer(tmp_path: Path) -> None:
    buffer_size = 7

    class TrackingReader(BytesIO):
        def __init__(self, payload: bytes) -> None:
            super().__init__(payload)
            self.requested_sizes: list[int] = []

        def read(self, size: int = -1) -> bytes:
            self.requested_sizes.append(size)
            return super().read(size)

    source = TrackingReader(SYNTHETIC_AUDIO * 8)

    published = publish_audio(
        tmp_path,
        STORAGE_KEY,
        source,
        buffer_size=buffer_size,
    )

    assert published.path.read_bytes() == SYNTHETIC_AUDIO * 8
    assert source.requested_sizes
    assert set(source.requested_sizes) == {buffer_size}
    assert len(source.requested_sizes) > 2
