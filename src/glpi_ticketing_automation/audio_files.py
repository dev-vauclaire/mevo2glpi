"""Atomic filesystem primitives for the audio lifecycle.

The POC has a single worker and assumes one writer for a given audio UUID.
"""

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import BinaryIO
from uuid import UUID, uuid4

SUPPORTED_AUDIO_EXTENSIONS = frozenset({".wav", ".mp3", ".ogg", ".m4a"})
DEFAULT_BUFFER_SIZE = 64 * 1024


class InvalidAudioFilenameError(ValueError):
    """Raised when an audio filename does not follow the UUID convention."""


class UnsupportedAudioExtensionError(InvalidAudioFilenameError):
    """Raised when an audio filename uses an unsupported extension."""


@dataclass(frozen=True, slots=True)
class AudioFileName:
    """Validated and normalized components of an audio filename."""

    audio_uuid: UUID
    extension: str

    @property
    def filename(self) -> str:
        """Return the canonical filename for these components."""
        return f"{self.audio_uuid}{self.extension}"


@dataclass(frozen=True, slots=True)
class FailedTransition:
    """Paths published by a successful transition to the failed directory."""

    audio_path: Path
    sidecar_path: Path


def parse_audio_filename(name_or_path: str | Path) -> AudioFileName:
    """Validate an audio basename and return its normalized components."""
    raw_name = str(name_or_path)
    path = Path(raw_name)

    if not raw_name or path.name != raw_name:
        msg = "Audio filename must be a basename without directory components"
        raise InvalidAudioFilenameError(msg)

    extension = path.suffix.lower()
    if extension not in SUPPORTED_AUDIO_EXTENSIONS:
        msg = f"Unsupported audio extension: {path.suffix or '<missing>'}"
        raise UnsupportedAudioExtensionError(msg)

    uuid_text = raw_name[: -len(path.suffix)]
    try:
        audio_uuid = UUID(uuid_text)
    except ValueError as error:
        msg = "Audio filename must start with a valid UUID"
        raise InvalidAudioFilenameError(msg) from error

    if str(audio_uuid) != uuid_text.lower():
        msg = "Audio UUID must use the canonical hyphenated representation"
        raise InvalidAudioFilenameError(msg)

    return AudioFileName(audio_uuid=audio_uuid, extension=extension)


def atomic_write(
    destination: Path,
    source: BinaryIO,
    *,
    buffer_size: int = DEFAULT_BUFFER_SIZE,
) -> Path:
    """Publish a stream through a flushed and synchronized ``.part`` file."""
    destination = Path(destination)
    _validate_buffer_size(buffer_size)
    _require_directory(destination.parent)
    _require_absent(destination)

    part_path = _new_part_path(destination)
    part_created = False
    destination_published = False

    try:
        with part_path.open("xb") as destination_stream:
            part_created = True
            _copy_stream(source, destination_stream, buffer_size)
            destination_stream.flush()
            _fsync_file(destination_stream.fileno())

        _require_absent(destination)
        _replace_file(part_path, destination)
        part_created = False
        destination_published = True
        _fsync_directory(destination.parent)
    except BaseException:
        if part_created:
            _unlink_if_exists(part_path)
        if destination_published:
            _unlink_if_exists(destination)
        raise

    return destination


def move_audio(
    source: Path,
    destination_directory: Path,
    *,
    buffer_size: int = DEFAULT_BUFFER_SIZE,
) -> Path:
    """Copy an audio to another volume, then remove its source."""
    source = Path(source)
    destination_directory = Path(destination_directory)
    audio_name = _validate_audio_source(source)
    _require_directory(destination_directory)

    destination = destination_directory / audio_name.filename
    with source.open("rb") as source_stream:
        atomic_write(destination, source_stream, buffer_size=buffer_size)

    try:
        _unlink_source(source)
    except BaseException:
        if source.exists():
            _unlink_if_exists(destination)
            _fsync_directory(destination_directory)
        raise
    return destination


def move_to_failed(
    source: Path,
    failed_directory: Path,
    sidecar_payload: Mapping[str, object],
    *,
    buffer_size: int = DEFAULT_BUFFER_SIZE,
) -> FailedTransition:
    """Publish an audio and error sidecar before removing the source."""
    source = Path(source)
    failed_directory = Path(failed_directory)
    audio_name = _validate_audio_source(source)
    _require_directory(failed_directory)
    serialized_sidecar = _serialize_sidecar(sidecar_payload)

    audio_destination = failed_directory / audio_name.filename
    sidecar_destination = failed_directory / f"{audio_name.audio_uuid}.error.json"
    attempt_paths = (
        audio_destination,
        sidecar_destination,
    )
    for attempt_path in attempt_paths:
        _require_absent(attempt_path)

    published_paths: list[Path] = []
    try:
        with source.open("rb") as source_stream:
            atomic_write(
                audio_destination,
                source_stream,
                buffer_size=buffer_size,
            )
        published_paths.append(audio_destination)

        atomic_write(
            sidecar_destination,
            BytesIO(serialized_sidecar),
            buffer_size=buffer_size,
        )
        published_paths.append(sidecar_destination)
        _unlink_source(source)
    except BaseException:
        if source.exists():
            _cleanup_failed_attempt(published_paths, failed_directory)
        raise

    return FailedTransition(
        audio_path=audio_destination,
        sidecar_path=sidecar_destination,
    )


def list_ready_audio_files(directory: Path) -> list[Path]:
    """List valid, complete audio files in deterministic filename order."""
    directory = Path(directory)
    _require_directory(directory)
    ready_files: list[Path] = []

    for candidate in directory.iterdir():
        if candidate.name.endswith(".part"):
            continue
        if candidate.is_symlink() or not candidate.is_file():
            continue

        try:
            parse_audio_filename(candidate.name)
        except InvalidAudioFilenameError:
            continue
        ready_files.append(candidate)

    return sorted(ready_files, key=lambda path: path.name)


def _validate_audio_source(source: Path) -> AudioFileName:
    audio_name = parse_audio_filename(source.name)
    if source.is_symlink():
        msg = f"Audio source must not be a symbolic link: {source}"
        raise ValueError(msg)
    if not source.exists():
        raise FileNotFoundError(source)
    if not source.is_file():
        msg = f"Audio source must be a regular file: {source}"
        raise ValueError(msg)
    return audio_name


def _validate_buffer_size(buffer_size: int) -> None:
    if isinstance(buffer_size, bool) or not isinstance(buffer_size, int):
        raise TypeError("buffer_size must be an integer")
    if buffer_size <= 0:
        raise ValueError("buffer_size must be greater than zero")


def _copy_stream(source: BinaryIO, destination: BinaryIO, buffer_size: int) -> None:
    while True:
        chunk = source.read(buffer_size)
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise TypeError("Binary source.read() must return bytes")
        if len(chunk) == 0:
            return
        if len(chunk) > buffer_size:
            raise OSError("Source returned more data than the requested buffer size")

        remaining = memoryview(chunk)
        while remaining:
            written = destination.write(remaining)
            if written is None or written <= 0:
                raise OSError("Destination did not accept the complete audio chunk")
            remaining = remaining[written:]


def _serialize_sidecar(sidecar_payload: Mapping[str, object]) -> bytes:
    """Serialize a small sidecar payload already sanitized by its caller."""
    try:
        serialized = json.dumps(
            dict(sidecar_payload),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise ValueError("Sidecar payload must contain valid JSON values") from error
    return f"{serialized}\n".encode()


def _cleanup_failed_attempt(
    published_paths: list[Path],
    destination_directory: Path,
) -> None:
    for path in reversed(published_paths):
        _unlink_if_exists(path)
    if published_paths:
        _fsync_directory(destination_directory)


def _new_part_path(destination: Path) -> Path:
    return destination.with_name(f"{destination.name}.{uuid4()}.part")


def _require_directory(directory: Path) -> None:
    if not directory.exists():
        raise FileNotFoundError(directory)
    if not directory.is_dir():
        raise NotADirectoryError(directory)


def _require_absent(path: Path) -> None:
    if path.exists() or path.is_symlink():
        raise FileExistsError(path)


def _unlink_if_exists(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _fsync_file(file_descriptor: int) -> None:
    os.fsync(file_descriptor)


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    file_descriptor = os.open(directory, flags)
    try:
        os.fsync(file_descriptor)
    finally:
        os.close(file_descriptor)


def _replace_file(source: Path, destination: Path) -> None:
    os.replace(source, destination)


def _unlink_source(source: Path) -> None:
    source.unlink()
