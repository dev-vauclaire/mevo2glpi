"""Atomic operator retry of one failed or blocked lifecycle job."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from mevo2glpi_common.audio_files import (
    InvalidAudioStorageKeyError,
    build_audio_storage_key,
    resolve_audio_storage_path,
)
from mevo2glpi_database import (
    AudioRetentionRepository,
    InvalidJobTransitionError,
    InvalidRetentionTransitionError,
    JobEventActor,
    JobEventType,
    JobNotFoundError,
    JobStatus,
    RetentionNotFoundError,
    RetentionStatus,
    TicketCreationJobEventRepository,
    TicketCreationJobRepository,
)

from ._audio_storage import open_audio_stream
from .models import (
    AudioStorageUnavailableError,
    JobPersistenceUnavailableError,
    JobRetryConflictError,
    JobView,
    RetriedJob,
)


class _AudioJobReader(Protocol):
    def execute(self, audio_uuid: UUID) -> JobView | None:
        """Return a sanitized job view using its own short-lived session."""


class RetryAudioJob:
    """Validate canonical audio, then rearm job, retention and audit atomically."""

    def __init__(
        self,
        *,
        audio_root: Path,
        session_factory: sessionmaker[Session],
        get_audio_job: _AudioJobReader,
        now_factory: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        resolved_audio_root = Path(audio_root).expanduser().resolve(strict=True)
        if not resolved_audio_root.is_dir():
            raise NotADirectoryError(resolved_audio_root)
        self._audio_root = resolved_audio_root
        self._session_factory = session_factory
        self._get_audio_job = get_audio_job
        self._now_factory = now_factory

    def execute(self, audio_uuid: UUID) -> RetriedJob | None:
        """Return ``None`` if unknown, otherwise atomically request a retry."""
        job: JobView | None = self._get_audio_job.execute(audio_uuid)
        if job is None:
            return None
        if job.state not in {JobStatus.FAILED, JobStatus.BLOCKED}:
            raise JobRetryConflictError("Job state does not allow an operator retry")
        self._inspect_canonical_audio(job)

        now = self._now_factory()
        session: Session | None = None
        try:
            session = self._session_factory()
            session.begin()
            rearmed = TicketCreationJobRepository(session).rearm_for_operator_retry(
                audio_uuid, now=now
            )
            AudioRetentionRepository(session).deactivate_for_operator_retry(
                audio_uuid,
                now=now,
            )
            TicketCreationJobEventRepository(session).append(
                audio_uuid,
                occurred_at=now,
                status=rearmed.status,
                stage=rearmed.stage,
                event_type=JobEventType.OPERATOR_RETRY_REQUESTED,
                actor=JobEventActor.WEB,
            )
        except JobNotFoundError:
            _rollback_and_close(session)
            return None
        except (
            InvalidJobTransitionError,
            InvalidRetentionTransitionError,
            RetentionNotFoundError,
        ) as error:
            _rollback_and_close(session)
            raise JobRetryConflictError("Job cannot safely be rearmed") from error
        except Exception as error:
            _rollback_and_close(session)
            raise JobPersistenceUnavailableError("operator_retry_failed") from error

        try:
            session.commit()
        except Exception as error:
            _close_session(session)
            self._reconcile_uncertain_commit(job, now, error)
        else:
            _close_session(session)
        return RetriedJob(audio_uuid=rearmed.uuid, state=rearmed.status)

    def _reconcile_uncertain_commit(
        self,
        original_job: JobView,
        occurred_at: datetime,
        commit_error: Exception,
    ) -> None:
        """Accept an acknowledged retry whose COMMIT response was lost."""
        session: Session | None = None
        try:
            session = self._session_factory()
            job = TicketCreationJobRepository(session).get_by_uuid(
                original_job.audio_uuid
            )
            retention = AudioRetentionRepository(session).get_by_job_uuid(
                original_job.audio_uuid
            )
            event_exists = TicketCreationJobEventRepository(session).exists(
                original_job.audio_uuid,
                event_type=JobEventType.OPERATOR_RETRY_REQUESTED,
                actor=JobEventActor.WEB,
            )
        except Exception:
            raise JobPersistenceUnavailableError(
                "operator_retry_outcome_unknown"
            ) from commit_error
        finally:
            _close_session(session)

        if (
            job is not None
            and job.status is JobStatus.PENDING
            and job.stage is None
            and job.glpi_ticket_id == original_job.glpi_ticket_id
            and job.attempt_count == original_job.attempt_count
            and job.consecutive_retry_count == 0
            and job.updated_at == occurred_at
            and retention is not None
            and retention.status is RetentionStatus.INACTIVE
            and event_exists
        ):
            return
        raise JobPersistenceUnavailableError(
            "operator_retry_outcome_unknown"
        ) from commit_error

    def _inspect_canonical_audio(self, job: JobView) -> None:
        """Verify canonical regular-file bytes without holding a SQL transaction."""
        try:
            storage_key = build_audio_storage_key(
                job.audio_uuid,
                job.audio_extension.value,
            )
            audio_path = resolve_audio_storage_path(self._audio_root, storage_key)
            stream, size_bytes = open_audio_stream(audio_path)
        except (FileNotFoundError, InvalidAudioStorageKeyError) as error:
            raise JobRetryConflictError("Canonical audio is unavailable") from error
        except OSError as error:
            raise AudioStorageUnavailableError(
                "Canonical audio storage is temporarily unavailable"
            ) from error
        stream.close()
        if size_bytes != job.audio_size_bytes:
            raise JobRetryConflictError(
                "Canonical audio does not match persisted metadata"
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


__all__ = ["RetryAudioJob"]
