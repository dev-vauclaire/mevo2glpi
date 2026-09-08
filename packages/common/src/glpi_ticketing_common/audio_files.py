"""Canonical, atomic filesystem storage for lifecycle audio files."""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO
from uuid import UUID, uuid4

SUPPORTED_AUDIO_EXTENSIONS = frozenset({".wav", ".mp3", ".ogg", ".m4a"})
SUPPORTED_AUDIO_STORAGE_EXTENSIONS = frozenset(
    extension.removeprefix(".") for extension in SUPPORTED_AUDIO_EXTENSIONS
)
DEFAULT_BUFFER_SIZE = 64 * 1024


class InvalidAudioFilenameError(ValueError):
    """Raised when an audio filename does not follow the UUID convention."""


class UnsupportedAudioExtensionError(InvalidAudioFilenameError):
    """Raised when an audio filename uses an unsupported extension."""


class InvalidAudioStorageKeyError(ValueError):
    """Raised when an audio storage key is not strictly canonical."""


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
class AudioStorageKey:
    """Validated components of a canonical MVP audio storage key."""

    audio_uuid: UUID
    extension: str

    @property
    def storage_key(self) -> str:
        """Return ``{canonical UUID}.{extension without a leading dot}``."""
        return f"{self.audio_uuid}.{self.extension}"


@dataclass(frozen=True, slots=True)
class PublishedAudio:
    """Metadata for an audio durably published in the canonical volume."""

    storage_key: str
    path: Path
    size_bytes: int


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


def build_audio_storage_key(audio_uuid: UUID, extension: str) -> str:
    """Build a canonical key from a UUID and a source-file extension.

    The ingestion boundary may provide an extension with a leading dot or
    uppercase characters. The persisted key is always normalized and is
    validated again before being returned.
    """
    if not isinstance(audio_uuid, UUID):
        raise TypeError("audio_uuid must be a UUID")
    if not isinstance(extension, str):
        raise TypeError("extension must be a string")

    normalized_extension = extension.removeprefix(".").lower()
    if normalized_extension not in SUPPORTED_AUDIO_STORAGE_EXTENSIONS:
        raise UnsupportedAudioExtensionError("Unsupported audio storage extension")
    storage_key = f"{audio_uuid}.{normalized_extension}"
    return parse_audio_storage_key(storage_key).storage_key


def parse_audio_storage_key(storage_key: str) -> AudioStorageKey:
    """Validate a persisted storage key without accepting normalization.

    Persisted keys form a security boundary: unlike upload extensions, they
    must already use their canonical lowercase representation.
    """
    if not isinstance(storage_key, str):
        raise TypeError("storage_key must be a string")
    if (
        not storage_key
        or "/" in storage_key
        or "\\" in storage_key
        or storage_key.count(".") != 1
    ):
        raise InvalidAudioStorageKeyError("Audio storage key is not canonical")

    uuid_text, extension = storage_key.split(".", maxsplit=1)
    if extension not in SUPPORTED_AUDIO_STORAGE_EXTENSIONS:
        raise InvalidAudioStorageKeyError("Audio storage extension is unsupported")

    try:
        audio_uuid = UUID(uuid_text)
    except ValueError as error:
        raise InvalidAudioStorageKeyError("Audio storage UUID is invalid") from error
    if str(audio_uuid) != uuid_text:
        raise InvalidAudioStorageKeyError("Audio storage UUID is not canonical")

    parsed = AudioStorageKey(audio_uuid=audio_uuid, extension=extension)
    if parsed.storage_key != storage_key:
        raise InvalidAudioStorageKeyError("Audio storage key is not canonical")
    return parsed


def resolve_audio_storage_path(audio_root: Path, storage_key: str) -> Path:
    """Resolve a canonical storage key to a path confined to ``audio_root``."""
    parsed = parse_audio_storage_key(storage_key)
    root = Path(audio_root)
    _require_directory(root)
    resolved_root = root.resolve(strict=True)
    candidate = resolved_root / parsed.storage_key
    if candidate.parent != resolved_root or candidate.is_symlink():
        raise InvalidAudioStorageKeyError("Audio storage path is not confined")
    return candidate


def publish_audio(
    audio_root: Path,
    storage_key: str,
    source: BinaryIO,
    *,
    buffer_size: int = DEFAULT_BUFFER_SIZE,
) -> PublishedAudio:
    """Publish a binary stream atomically without replacing an existing audio."""
    destination = resolve_audio_storage_path(audio_root, storage_key)
    size_bytes = _atomic_write(destination, source, buffer_size=buffer_size)
    return PublishedAudio(
        storage_key=storage_key,
        path=destination,
        size_bytes=size_bytes,
    )


def delete_audio(audio_root: Path, storage_key: str) -> bool:
    """Delete exactly one canonical audio and durably record the removal.

    ``False`` means the key was already absent. Other filesystem failures remain
    observable so callers do not mistake an uncertain compensation for success.
    """
    path = resolve_audio_storage_path(audio_root, storage_key)
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    _fsync_directory(path.parent)
    return True


def _atomic_write(
    destination: Path,
    source: BinaryIO,
    *,
    buffer_size: int,
) -> int:
    """Implement atomic publication and return the number of copied bytes."""
    _validate_buffer_size(buffer_size)
    _require_directory(destination.parent)
    _require_absent(destination)

    part_path = _new_part_path(destination)
    part_created = False
    destination_published = False

    try:
        with part_path.open("xb") as destination_stream:
            part_created = True
            size_bytes = _copy_stream(source, destination_stream, buffer_size)
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

    return size_bytes


def _validate_buffer_size(buffer_size: int) -> None:
    if isinstance(buffer_size, bool) or not isinstance(buffer_size, int):
        raise TypeError("buffer_size must be an integer")
    if buffer_size <= 0:
        raise ValueError("buffer_size must be greater than zero")


def _copy_stream(source: BinaryIO, destination: BinaryIO, buffer_size: int) -> int:
    size_bytes = 0
    while True:
        chunk = source.read(buffer_size)
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise TypeError("Binary source.read() must return bytes")
        if len(chunk) == 0:
            return size_bytes
        if len(chunk) > buffer_size:
            raise OSError("Source returned more data than the requested buffer size")
        size_bytes += len(chunk)

        remaining = memoryview(chunk)
        while remaining:
            written = destination.write(remaining)
            if written is None or written <= 0:
                raise OSError("Destination did not accept the complete audio chunk")
            remaining = remaining[written:]


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
    os.link(source, destination, follow_symlinks=False)
    try:
        source.unlink()
    except BaseException:
        _unlink_if_exists(destination)
        raise
