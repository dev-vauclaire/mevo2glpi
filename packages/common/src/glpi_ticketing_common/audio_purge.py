"""Crash-safe deletion of one canonical audio through a private quarantine."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from .audio_files import InvalidAudioStorageKeyError, parse_audio_storage_key

PURGE_DIRECTORY_NAME = ".purge"


class AudioPurgeDisposition(StrEnum):
    """Observable way in which a purge reached its durable final state."""

    PURGED = "purged"
    RESUMED = "resumed"
    ALREADY_ABSENT = "already_absent"


class AudioPurgeErrorCode(StrEnum):
    """Stable, non-sensitive technical codes exposed to applications."""

    INVALID_STORAGE_KEY = "purge_invalid_storage_key"
    AUDIO_ROOT_UNAVAILABLE = "purge_audio_root_unavailable"
    UNSAFE_AUDIO_ROOT = "purge_unsafe_audio_root"
    QUARANTINE_UNAVAILABLE = "purge_quarantine_unavailable"
    UNSAFE_QUARANTINE = "purge_unsafe_quarantine"
    CROSS_DEVICE_QUARANTINE = "purge_cross_device_quarantine"
    ENTRY_INSPECTION_FAILED = "purge_entry_inspection_failed"
    UNSAFE_CANONICAL_ENTRY = "purge_unsafe_canonical_entry"
    UNSAFE_QUARANTINE_ENTRY = "purge_unsafe_quarantine_entry"
    AMBIGUOUS_STATE = "purge_ambiguous_state"
    STATE_CHANGED = "purge_state_changed"
    MOVE_FAILED = "purge_move_failed"
    DIRECTORY_SYNC_FAILED = "purge_directory_sync_failed"
    UNLINK_FAILED = "purge_unlink_failed"


class AudioPurgeError(Exception):
    """Base error whose public representation never contains a filesystem path."""

    def __init__(
        self,
        *,
        code: AudioPurgeErrorCode,
        retryable: bool,
        errno: int | None = None,
    ) -> None:
        if not isinstance(code, AudioPurgeErrorCode):
            raise TypeError("code must be an AudioPurgeErrorCode")
        if not isinstance(retryable, bool):
            raise TypeError("retryable must be a boolean")
        if errno is not None and (
            isinstance(errno, bool) or not isinstance(errno, int) or errno < 0
        ):
            raise ValueError("errno must be a non-negative integer or None")

        self.code = code
        self.retryable = retryable
        self.errno = errno
        super().__init__(code.value)


class InvalidAudioPurgeTargetError(AudioPurgeError):
    """Raised when persisted input cannot designate a safe purge target."""


class UnsafeAudioPurgeStateError(AudioPurgeError):
    """Raised when an entry type or directory layout is unsafe."""


class AmbiguousAudioPurgeStateError(AudioPurgeError):
    """Raised when both canonical and quarantined entries exist."""


class AudioPurgeFilesystemError(AudioPurgeError):
    """Raised when a local filesystem operation cannot be confirmed durable."""


@dataclass(frozen=True, slots=True)
class AudioPurgeResult:
    """Sanitized result of a completed or recovered audio purge."""

    storage_key: str
    disposition: AudioPurgeDisposition


def purge_audio(audio_root: Path, storage_key: str) -> AudioPurgeResult:
    """Durably remove exactly one canonical audio via ``.purge``.

    The caller must serialize purges for a given volume. The function never
    scans the volume and never accepts an arbitrary relative path. It recovers
    when either the create-only quarantine publication or the unlink completed
    before a crash.
    """

    canonical_key = _validated_storage_key(storage_key)
    root, root_device = _validated_audio_root(audio_root)
    quarantine = _prepare_quarantine(root, root_device=root_device)
    canonical_path = root / canonical_key
    quarantine_path = quarantine / canonical_key

    canonical_metadata = _validate_audio_entry(
        canonical_path,
        unsafe_code=AudioPurgeErrorCode.UNSAFE_CANONICAL_ENTRY,
    )
    quarantine_metadata = _validate_audio_entry(
        quarantine_path,
        unsafe_code=AudioPurgeErrorCode.UNSAFE_QUARANTINE_ENTRY,
    )

    if canonical_metadata is not None and quarantine_metadata is not None:
        if not os.path.samestat(canonical_metadata, quarantine_metadata):
            raise AmbiguousAudioPurgeStateError(
                code=AudioPurgeErrorCode.AMBIGUOUS_STATE,
                retryable=False,
            )
        # A create-only move is a hard-link publication followed by removal of
        # the canonical name. Both names can therefore survive a crash, but
        # only when they still designate the exact same inode.
        _unlink_canonical_audio(canonical_path)
        _synchronize_directory(root)
        _synchronize_directory(quarantine)
        disposition = AudioPurgeDisposition.RESUMED
    elif canonical_metadata is not None:
        _move_to_quarantine(
            canonical_path,
            quarantine_path,
            expected_metadata=canonical_metadata,
        )
        _synchronize_directory(root)
        _synchronize_directory(quarantine)
        disposition = AudioPurgeDisposition.PURGED
    elif quarantine_metadata is not None:
        # A previous process may have crashed before either directory entry was
        # made durable. Synchronize both sides before resuming the unlink.
        _synchronize_directory(root)
        _synchronize_directory(quarantine)
        disposition = AudioPurgeDisposition.RESUMED
    else:
        # This also closes the crash window where unlink succeeded but the
        # quarantine directory fsync did not.
        _synchronize_directory(root)
        _synchronize_directory(quarantine)
        return AudioPurgeResult(
            storage_key=canonical_key,
            disposition=AudioPurgeDisposition.ALREADY_ABSENT,
        )

    _unlink_quarantined_audio(quarantine_path)
    _synchronize_directory(quarantine)
    return AudioPurgeResult(
        storage_key=canonical_key,
        disposition=disposition,
    )


def _validated_storage_key(storage_key: object) -> str:
    try:
        return parse_audio_storage_key(storage_key).storage_key  # type: ignore[arg-type]
    except (InvalidAudioStorageKeyError, TypeError):
        raise InvalidAudioPurgeTargetError(
            code=AudioPurgeErrorCode.INVALID_STORAGE_KEY,
            retryable=False,
        ) from None


def _validated_audio_root(audio_root: object) -> tuple[Path, int]:
    try:
        root = Path(audio_root)  # type: ignore[arg-type]
        root_metadata = root.lstat()
    except (TypeError, ValueError):
        raise InvalidAudioPurgeTargetError(
            code=AudioPurgeErrorCode.UNSAFE_AUDIO_ROOT,
            retryable=False,
        ) from None
    except OSError as error:
        raise AudioPurgeFilesystemError(
            code=AudioPurgeErrorCode.AUDIO_ROOT_UNAVAILABLE,
            retryable=True,
            errno=error.errno,
        ) from None

    if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
        raise UnsafeAudioPurgeStateError(
            code=AudioPurgeErrorCode.UNSAFE_AUDIO_ROOT,
            retryable=False,
        )

    try:
        return root.resolve(strict=True), root_metadata.st_dev
    except OSError as error:
        raise AudioPurgeFilesystemError(
            code=AudioPurgeErrorCode.AUDIO_ROOT_UNAVAILABLE,
            retryable=True,
            errno=error.errno,
        ) from None


def _prepare_quarantine(root: Path, *, root_device: int) -> Path:
    quarantine = root / PURGE_DIRECTORY_NAME
    created = False
    try:
        metadata = quarantine.lstat()
    except FileNotFoundError:
        try:
            quarantine.mkdir(mode=0o700)
            created = True
            metadata = quarantine.lstat()
        except FileExistsError:
            try:
                metadata = quarantine.lstat()
            except OSError as error:
                raise AudioPurgeFilesystemError(
                    code=AudioPurgeErrorCode.QUARANTINE_UNAVAILABLE,
                    retryable=True,
                    errno=error.errno,
                ) from None
        except OSError as error:
            raise AudioPurgeFilesystemError(
                code=AudioPurgeErrorCode.QUARANTINE_UNAVAILABLE,
                retryable=True,
                errno=error.errno,
            ) from None
    except OSError as error:
        raise AudioPurgeFilesystemError(
            code=AudioPurgeErrorCode.QUARANTINE_UNAVAILABLE,
            retryable=True,
            errno=error.errno,
        ) from None

    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise UnsafeAudioPurgeStateError(
            code=AudioPurgeErrorCode.UNSAFE_QUARANTINE,
            retryable=False,
        )
    if metadata.st_dev != root_device:
        raise UnsafeAudioPurgeStateError(
            code=AudioPurgeErrorCode.CROSS_DEVICE_QUARANTINE,
            retryable=False,
        )
    if created:
        _synchronize_directory(root)
    return quarantine


def _validate_audio_entry(
    path: Path,
    *,
    unsafe_code: AudioPurgeErrorCode,
) -> os.stat_result | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise AudioPurgeFilesystemError(
            code=AudioPurgeErrorCode.ENTRY_INSPECTION_FAILED,
            retryable=True,
            errno=error.errno,
        ) from None

    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise UnsafeAudioPurgeStateError(
            code=unsafe_code,
            retryable=False,
        )
    return metadata


def _move_to_quarantine(
    source: Path,
    destination: Path,
    *,
    expected_metadata: os.stat_result,
) -> None:
    """Publish the quarantine name create-only, then remove the canonical one."""
    try:
        os.link(source, destination, follow_symlinks=False)
    except (FileExistsError, FileNotFoundError) as error:
        # Another fenced instance may have completed part of the move after our
        # inspection. A retry performs a fresh, safe state classification.
        raise AudioPurgeFilesystemError(
            code=AudioPurgeErrorCode.STATE_CHANGED,
            retryable=True,
            errno=error.errno,
        ) from None
    except OSError as error:
        raise AudioPurgeFilesystemError(
            code=AudioPurgeErrorCode.MOVE_FAILED,
            retryable=True,
            errno=error.errno,
        ) from None

    linked_source = _validate_audio_entry(
        source,
        unsafe_code=AudioPurgeErrorCode.UNSAFE_CANONICAL_ENTRY,
    )
    linked_destination = _validate_audio_entry(
        destination,
        unsafe_code=AudioPurgeErrorCode.UNSAFE_QUARANTINE_ENTRY,
    )
    if linked_source is None or linked_destination is None:
        raise AudioPurgeFilesystemError(
            code=AudioPurgeErrorCode.STATE_CHANGED,
            retryable=True,
        )
    if not (
        os.path.samestat(expected_metadata, linked_source)
        and os.path.samestat(linked_source, linked_destination)
    ):
        raise AmbiguousAudioPurgeStateError(
            code=AudioPurgeErrorCode.AMBIGUOUS_STATE,
            retryable=False,
        )
    _unlink_canonical_audio(source)


def _unlink_canonical_audio(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as error:
        raise AudioPurgeFilesystemError(
            code=AudioPurgeErrorCode.MOVE_FAILED,
            retryable=True,
            errno=error.errno,
        ) from None


def _unlink_quarantined_audio(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        # Another recovery may already have removed the bytes. The following
        # directory fsync still establishes the durable postcondition.
        return
    except OSError as error:
        raise AudioPurgeFilesystemError(
            code=AudioPurgeErrorCode.UNLINK_FAILED,
            retryable=True,
            errno=error.errno,
        ) from None


def _synchronize_directory(directory: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        file_descriptor = os.open(directory, flags)
        try:
            os.fsync(file_descriptor)
        finally:
            os.close(file_descriptor)
    except OSError as error:
        raise AudioPurgeFilesystemError(
            code=AudioPurgeErrorCode.DIRECTORY_SYNC_FAILED,
            retryable=True,
            errno=error.errno,
        ) from None


__all__ = [
    "AmbiguousAudioPurgeStateError",
    "AudioPurgeDisposition",
    "AudioPurgeError",
    "AudioPurgeErrorCode",
    "AudioPurgeFilesystemError",
    "AudioPurgeResult",
    "InvalidAudioPurgeTargetError",
    "PURGE_DIRECTORY_NAME",
    "UnsafeAudioPurgeStateError",
    "purge_audio",
]
