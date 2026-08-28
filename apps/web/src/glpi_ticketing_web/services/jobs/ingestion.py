"""Durable ingestion of one canonical audio and its pending lifecycle job."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO
from uuid import UUID, uuid4

from sqlalchemy.orm import Session, sessionmaker

from glpi_ticketing_common.audio_files import (
    AudioStorageKey,
    build_audio_storage_key,
    delete_audio,
    parse_audio_storage_key,
    publish_audio,
)
from glpi_ticketing_database import (
    AudioExtension,
    JobEventActor,
    JobEventType,
    JobSnapshot,
    PendingJob,
    TicketCreationJobEventRepository,
    TicketCreationJobRepository,
)

from .models import (
    AudioStorageConflictError,
    AudioStorageUnavailableError,
    EmptyAudioError,
    JobPersistenceUnavailableError,
    SubmittedJob,
    UnsupportedAudioUploadError,
    UploadTooLargeError,
)

LOGGER = logging.getLogger(__name__)


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
        try:
            published = publish_audio(
                self._audio_root,
                storage.storage_key,
                _SizeLimitedReader(source, self._max_upload_bytes),
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
            self._remove_empty_audio(storage, audio_uuid)
            raise EmptyAudioError("Audio file must not be empty")

        pending_job = PendingJob(
            uuid=audio_uuid,
            storage_key=storage.storage_key,
            audio_extension=AudioExtension(storage.extension),
            audio_size_bytes=published.size_bytes,
        )
        self._persist_pending(pending_job, storage)
        return SubmittedJob(audio_uuid=audio_uuid)

    def _remove_empty_audio(self, storage: AudioStorageKey, audio_uuid: UUID) -> None:
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

    def _persist_pending(
        self,
        pending_job: PendingJob,
        storage: AudioStorageKey,
    ) -> None:
        session: Session | None = None
        try:
            session = self._session_factory()
            session.begin()
            snapshot = TicketCreationJobRepository(session).add_pending(pending_job)
            TicketCreationJobEventRepository(session).append(
                snapshot.uuid,
                occurred_at=snapshot.created_at,
                status=snapshot.status,
                stage=snapshot.stage,
                event_type=JobEventType.JOB_CREATED,
                actor=JobEventActor.WEB,
            )
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
        reconciliation_session: Session | None = None
        try:
            reconciliation_session = self._session_factory()
            snapshot = TicketCreationJobRepository(reconciliation_session).get_by_uuid(
                pending_job.uuid
            )
            created_event_exists = TicketCreationJobEventRepository(
                reconciliation_session
            ).exists(
                pending_job.uuid,
                event_type=JobEventType.JOB_CREATED,
                actor=JobEventActor.WEB,
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

        if (
            snapshot is not None
            and created_event_exists
            and _snapshot_matches(snapshot, pending_job)
        ):
            LOGGER.warning(
                "job commit reconciled audio_uuid=%s "
                "event_code=database_commit_reconciled",
                pending_job.uuid,
            )
            return
        if snapshot is not None:
            technical_code = "database_commit_conflict"
        else:
            technical_code = "database_outcome_unknown"
        LOGGER.error(
            "job commit unresolved audio_uuid=%s error_code=%s",
            pending_job.uuid,
            technical_code,
        )
        raise JobPersistenceUnavailableError(technical_code) from commit_error

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


__all__ = ["SubmitAudioJob"]
