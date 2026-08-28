"""Application services for persistent audio jobs."""

from __future__ import annotations

import logging
import os
import stat
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Protocol
from uuid import UUID, uuid4

from sqlalchemy.orm import Session, sessionmaker

from glpi_ticketing_common.audio_files import (
    AudioStorageKey,
    InvalidAudioStorageKeyError,
    build_audio_storage_key,
    delete_audio,
    parse_audio_storage_key,
    publish_audio,
    resolve_audio_storage_path,
)
from glpi_ticketing_database import (
    AudioExtension,
    JobRepository,
    JobSnapshot,
    JobStatus,
    PendingJob,
)

LOGGER = logging.getLogger(__name__)

_AUDIO_STREAM_BUFFER_SIZE = 64 * 1024


class UploadTooLargeError(ValueError):
    """Report an upload exceeding the configured byte limit."""


class UnsupportedAudioUploadError(ValueError):
    """Report an upload filename whose extension is not supported."""


class EmptyAudioError(ValueError):
    """Report an empty upload, which cannot become a lifecycle job."""


class AudioStorageUnavailableError(RuntimeError):
    """Report a sanitized failure to publish or clean audio storage."""


class AudioStorageConflictError(RuntimeError):
    """Report that a generated canonical storage destination already exists."""


class AudioStorageInvariantError(RuntimeError):
    """Report that persisted metadata and canonical storage are inconsistent."""


class JobPersistenceUnavailableError(RuntimeError):
    """Report a sanitized persistence failure after audio publication."""

    def __init__(self, technical_code: str) -> None:
        super().__init__("Job persistence is unavailable")
        self.technical_code = technical_code


class JobQueryUnavailableError(RuntimeError):
    """Report a sanitized failure to read lifecycle state."""


@dataclass(frozen=True, slots=True)
class SubmittedJob:
    """Stable result returned after accepting a new audio job."""

    audio_uuid: UUID
    state: JobStatus = JobStatus.PENDING


@dataclass(frozen=True, slots=True)
class DownloadableAudio:
    """Validated audio descriptor whose stream stays open until consumption."""

    uuid: UUID
    extension: AudioExtension
    size_bytes: int
    _stream: BinaryIO = field(repr=False, compare=False)

    def iter_bytes(self) -> Iterator[bytes]:
        """Yield the validated audio and always close its descriptor."""
        try:
            while chunk := self._stream.read(_AUDIO_STREAM_BUFFER_SIZE):
                yield chunk
        finally:
            self._stream.close()

    def close(self) -> None:
        """Close an audio that could not be handed to the response iterator."""
        self._stream.close()


class _AudioJobReader(Protocol):
    def execute(self, audio_uuid: UUID) -> JobSnapshot | None:
        """Return one persisted job snapshot."""


class _SizeLimitedReader:
    """Bound reads from an upload stream without buffering the whole audio."""

    def __init__(self, source: BinaryIO, max_bytes: int) -> None:
        self._source = source
        self._max_bytes = max_bytes
        self._bytes_read = 0

    def read(self, size: int = -1) -> bytes:
        """Read at most the remaining allowance plus one detection byte."""
        if size == 0:
            return b""

        remaining_bytes = self._max_bytes - self._bytes_read
        probe_size = remaining_bytes + 1
        requested_size = probe_size if size < 0 else min(size, probe_size)
        chunk = self._source.read(requested_size)
        if not isinstance(chunk, bytes):
            raise TypeError("Upload stream must return bytes")

        self._bytes_read += len(chunk)
        if self._bytes_read > self._max_bytes:
            raise UploadTooLargeError("Audio file exceeds the upload size limit")
        return chunk


class SubmitAudioJob:
    """Publish an audio before atomically creating its pending database state."""

    def __init__(
        self,
        *,
        audio_root: Path,
        session_factory: sessionmaker[Session],
        max_upload_bytes: int,
        uuid_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        if isinstance(max_upload_bytes, bool) or not isinstance(max_upload_bytes, int):
            raise TypeError("max_upload_bytes must be an integer")
        if max_upload_bytes <= 0:
            raise ValueError("max_upload_bytes must be greater than zero")
        resolved_audio_root = Path(audio_root).expanduser().resolve(strict=True)
        if not resolved_audio_root.is_dir():
            raise NotADirectoryError(resolved_audio_root)
        self._audio_root = resolved_audio_root
        self._session_factory = session_factory
        self._max_upload_bytes = max_upload_bytes
        self._uuid_factory = uuid_factory

    def prevalidate(self, filename: str | None) -> str:
        """Validate an upload filename and return its normalized extension."""
        extension = Path(filename).suffix.removeprefix(".").lower() if filename else ""
        try:
            return AudioExtension(extension).value
        except ValueError:
            raise UnsupportedAudioUploadError(
                "Unsupported audio file extension"
            ) from None

    def execute(self, filename: str | None, source: BinaryIO) -> SubmittedJob:
        """Accept one stream and return only after durable file and SQL commits."""
        extension = self.prevalidate(filename)
        audio_uuid = self._uuid_factory()
        storage = parse_audio_storage_key(
            build_audio_storage_key(audio_uuid, extension)
        )
        limited_source = _SizeLimitedReader(source, self._max_upload_bytes)

        try:
            published = publish_audio(
                self._audio_root,
                storage.storage_key,
                limited_source,
            )
        except FileExistsError as error:
            raise AudioStorageConflictError(
                "Audio storage destination already exists"
            ) from error
        except UploadTooLargeError:
            raise
        except OSError as error:
            raise AudioStorageUnavailableError(
                "Audio storage is unavailable"
            ) from error

        if published.size_bytes == 0:
            try:
                delete_audio(self._audio_root, storage.storage_key)
            except OSError as error:
                LOGGER.error(
                    "empty audio cleanup failed audio_uuid=%s "
                    "error_code=audio_cleanup_failed",
                    audio_uuid,
                )
                raise AudioStorageUnavailableError(
                    "Audio storage is unavailable"
                ) from error
            raise EmptyAudioError("Audio file must not be empty")

        pending_job = PendingJob(
            uuid=audio_uuid,
            storage_key=storage.storage_key,
            audio_extension=AudioExtension(storage.extension),
            audio_size_bytes=published.size_bytes,
        )
        self._persist_pending(pending_job, storage)
        return SubmittedJob(audio_uuid=audio_uuid)

    def _persist_pending(
        self,
        pending_job: PendingJob,
        storage: AudioStorageKey,
    ) -> None:
        session: Session | None = None
        try:
            session = self._session_factory()
            session.begin()
            JobRepository(session).add_pending(pending_job)
        except Exception as error:
            _rollback_and_close(session)
            self._compensate_confirmed_failure(storage, pending_job.uuid, error)

        try:
            session.commit()
        except Exception as error:
            _close_session(session)
            self._reconcile_uncertain_commit(pending_job, error)
        else:
            _close_session(session)

    def _reconcile_uncertain_commit(
        self,
        pending_job: PendingJob,
        commit_error: Exception,
    ) -> None:
        snapshot: JobSnapshot | None
        reconciliation_session: Session | None = None
        try:
            reconciliation_session = self._session_factory()
            snapshot = JobRepository(reconciliation_session).get_by_uuid(
                pending_job.uuid
            )
        except Exception:
            LOGGER.error(
                "job commit outcome unknown audio_uuid=%s "
                "error_code=database_outcome_unknown",
                pending_job.uuid,
            )
            raise JobPersistenceUnavailableError(
                "database_outcome_unknown"
            ) from commit_error
        finally:
            _close_session(reconciliation_session)

        if snapshot is not None and _snapshot_matches(snapshot, pending_job):
            LOGGER.warning(
                "job commit reconciled audio_uuid=%s "
                "event_code=database_commit_reconciled",
                pending_job.uuid,
            )
            return

        if snapshot is not None:
            LOGGER.error(
                "job commit invariant unresolved audio_uuid=%s "
                "error_code=database_commit_conflict",
                pending_job.uuid,
            )
            raise JobPersistenceUnavailableError(
                "database_commit_conflict"
            ) from commit_error

        LOGGER.error(
            "job commit outcome unknown audio_uuid=%s "
            "error_code=database_outcome_unknown",
            pending_job.uuid,
        )
        raise JobPersistenceUnavailableError(
            "database_outcome_unknown"
        ) from commit_error

    def _compensate_confirmed_failure(
        self,
        storage: AudioStorageKey,
        audio_uuid: UUID,
        persistence_error: Exception,
    ) -> None:
        try:
            delete_audio(self._audio_root, storage.storage_key)
        except OSError:
            LOGGER.error(
                "job persistence and compensation failed audio_uuid=%s "
                "error_code=audio_compensation_failed",
                audio_uuid,
            )
            raise JobPersistenceUnavailableError(
                "audio_compensation_failed"
            ) from persistence_error

        LOGGER.warning(
            "job persistence failed audio_uuid=%s error_code=database_write_failed",
            audio_uuid,
        )
        raise JobPersistenceUnavailableError(
            "database_write_failed"
        ) from persistence_error


class GetAudioJob:
    """Read one immutable job snapshot using a short-lived session."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def execute(self, audio_uuid: UUID) -> JobSnapshot | None:
        """Return a job snapshot, or ``None`` when its UUID is unknown."""
        session: Session | None = None
        try:
            session = self._session_factory()
            return JobRepository(session).get_by_uuid(audio_uuid)
        except Exception as error:
            raise JobQueryUnavailableError("Job persistence is unavailable") from error
        finally:
            _close_session(session)


class GetDownloadableAudio:
    """Open a completed audio through its persisted canonical storage key."""

    def __init__(self, audio_root: Path, get_audio_job: _AudioJobReader) -> None:
        resolved_audio_root = Path(audio_root).expanduser().resolve(strict=True)
        if not resolved_audio_root.is_dir():
            raise NotADirectoryError(resolved_audio_root)
        self._audio_root = resolved_audio_root
        self._get_audio_job = get_audio_job

    def execute(self, audio_uuid: UUID) -> DownloadableAudio | None:
        """Return a validated completed audio, or ``None`` when unavailable."""
        snapshot = self._get_audio_job.execute(audio_uuid)
        if snapshot is None or snapshot.status is not JobStatus.COMPLETED:
            return None

        try:
            audio_path = resolve_audio_storage_path(
                self._audio_root,
                snapshot.storage_key,
            )
            audio_stream, size_bytes = _open_audio_stream(audio_path)
        except FileNotFoundError:
            return None
        except (InvalidAudioStorageKeyError, OSError) as error:
            raise AudioStorageInvariantError(
                "Audio storage invariant violated"
            ) from error

        return DownloadableAudio(
            uuid=snapshot.uuid,
            extension=snapshot.audio_extension,
            size_bytes=size_bytes,
            _stream=audio_stream,
        )


def _open_audio_stream(audio_path: Path) -> tuple[BinaryIO, int]:
    """Open one regular file without following a path swapped to a symlink."""
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
    file_descriptor = os.open(audio_path, flags)
    try:
        file_stat = os.fstat(file_descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise InvalidAudioStorageKeyError("Audio storage path is not regular")
        return os.fdopen(file_descriptor, "rb", closefd=True), file_stat.st_size
    except BaseException:
        os.close(file_descriptor)
        raise


def _snapshot_matches(snapshot: JobSnapshot, pending_job: PendingJob) -> bool:
    return (
        snapshot.uuid == pending_job.uuid
        and snapshot.storage_key == pending_job.storage_key
        and snapshot.audio_extension is pending_job.audio_extension
        and snapshot.audio_size_bytes == pending_job.audio_size_bytes
    )


def _rollback_and_close(session: Session | None) -> None:
    if session is None:
        return
    try:
        session.rollback()
    except Exception:
        pass
    _close_session(session)


def _close_session(session: Session | None) -> None:
    if session is None:
        return
    try:
        session.close()
    except Exception:
        pass


__all__ = [
    "AudioStorageConflictError",
    "AudioStorageInvariantError",
    "AudioStorageUnavailableError",
    "DownloadableAudio",
    "EmptyAudioError",
    "GetAudioJob",
    "GetDownloadableAudio",
    "JobPersistenceUnavailableError",
    "JobQueryUnavailableError",
    "SubmitAudioJob",
    "SubmittedJob",
    "UnsupportedAudioUploadError",
    "UploadTooLargeError",
]
