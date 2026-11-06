"""Read-only use cases for the job dashboard and event history."""

from __future__ import annotations

import base64
import binascii
import json
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from mevo2glpi_database import (
    AudioRetentionRepository,
    AudioRetentionSnapshot,
    JobCursor,
    JobEventCursor,
    JobSnapshot,
    JobStatus,
    TicketCreationJobEventRepository,
    TicketCreationJobRepository,
)

from .models import (
    InvalidCursorError,
    JobEventListPage,
    JobEventView,
    JobListPage,
    JobQueryUnavailableError,
    JobView,
)

_MAX_CURSOR_LENGTH = 512
_MAX_BIGINT = (1 << 63) - 1


class ListAudioJobs:
    """Return a bounded keyset page without exposing persistence metadata."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def execute(
        self,
        *,
        limit: int,
        state: str | None = None,
        audio_uuid: UUID | None = None,
        cursor: str | None = None,
    ) -> JobListPage:
        """List jobs ordered by their latest transition, newest first."""
        repository_cursor = _decode_job_cursor(cursor) if cursor else None
        status = JobStatus(state) if state is not None else None
        session: Session | None = None
        try:
            session = self._session_factory()
            page = TicketCreationJobRepository(session).list_page(
                limit=limit,
                status=status,
                job_uuid=audio_uuid,
                cursor=repository_cursor,
            )
            return JobListPage(
                items=tuple(_job_view(item) for item in page.items),
                next_cursor=(
                    _encode_job_cursor(page.next_cursor)
                    if page.next_cursor is not None
                    else None
                ),
            )
        except Exception as error:
            raise JobQueryUnavailableError("Job persistence is unavailable") from error
        finally:
            _close_session(session)


class GetAudioJob:
    """Read one job and its optional retention state in one short session."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def execute(self, audio_uuid: UUID) -> JobView | None:
        """Return a sanitized job view, or ``None`` when unknown."""
        session: Session | None = None
        try:
            session = self._session_factory()
            snapshot = TicketCreationJobRepository(session).get_by_uuid(audio_uuid)
            if snapshot is None:
                return None
            retention = AudioRetentionRepository(session).get_by_job_uuid(audio_uuid)
            return _job_view(snapshot, retention=retention)
        except Exception as error:
            raise JobQueryUnavailableError("Job persistence is unavailable") from error
        finally:
            _close_session(session)


class ListAudioJobEvents:
    """Return one bounded keyset page of a known job's audit history."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def execute(
        self,
        audio_uuid: UUID,
        *,
        limit: int,
        cursor: str | None = None,
    ) -> JobEventListPage | None:
        """Return ``None`` for an unknown job, otherwise its event page."""
        repository_cursor = _decode_event_cursor(cursor) if cursor else None
        session: Session | None = None
        try:
            session = self._session_factory()
            if TicketCreationJobRepository(session).get_by_uuid(audio_uuid) is None:
                return None
            page = TicketCreationJobEventRepository(session).list_page(
                audio_uuid,
                limit=limit,
                cursor=repository_cursor,
            )
            return JobEventListPage(
                items=tuple(
                    JobEventView(
                        event_id=item.event_id,
                        event_type=item.event_type,
                        occurred_at=item.occurred_at,
                        state=item.status,
                        stage=item.stage,
                        actor=item.actor,
                        error_scope=item.error_scope,
                        error_category=item.error_category,
                        technical_code=item.technical_code,
                    )
                    for item in page.items
                ),
                next_cursor=(
                    _encode_event_cursor(page.next_cursor)
                    if page.next_cursor is not None
                    else None
                ),
            )
        except Exception as error:
            raise JobQueryUnavailableError("Job persistence is unavailable") from error
        finally:
            _close_session(session)


def _job_view(
    snapshot: JobSnapshot,
    *,
    retention: AudioRetentionSnapshot | None = None,
) -> JobView:
    return JobView(
        audio_uuid=snapshot.uuid,
        state=snapshot.status,
        stage=snapshot.stage,
        audio_extension=snapshot.audio_extension,
        audio_size_bytes=snapshot.audio_size_bytes,
        glpi_ticket_id=snapshot.glpi_ticket_id,
        attempt_count=snapshot.attempt_count,
        consecutive_retry_count=snapshot.consecutive_retry_count,
        next_attempt_at=snapshot.next_attempt_at,
        last_error_scope=snapshot.last_error_scope,
        last_error_category=snapshot.last_error_category,
        last_error_code=snapshot.last_error_code,
        created_at=snapshot.created_at,
        first_started_at=snapshot.first_started_at,
        updated_at=snapshot.updated_at,
        finished_at=snapshot.finished_at,
        retention_status=retention.status if retention is not None else None,
        audio_deleted_at=(
            retention.audio_deleted_at if retention is not None else None
        ),
    )


def _encode_job_cursor(cursor: JobCursor) -> str:
    return _encode_cursor("jobs", cursor.updated_at, str(cursor.uuid))


def _decode_job_cursor(cursor: str) -> JobCursor:
    timestamp, identifier = _decode_cursor(cursor, expected_kind="jobs")
    try:
        return JobCursor(updated_at=timestamp, uuid=UUID(identifier))
    except (TypeError, ValueError):
        raise InvalidCursorError("Invalid job cursor") from None


def _encode_event_cursor(cursor: JobEventCursor) -> str:
    return _encode_cursor("events", cursor.occurred_at, cursor.event_id)


def _decode_event_cursor(cursor: str) -> JobEventCursor:
    timestamp, identifier = _decode_cursor(cursor, expected_kind="events")
    if (
        isinstance(identifier, bool)
        or not isinstance(identifier, int)
        or identifier <= 0
        or identifier > _MAX_BIGINT
    ):
        raise InvalidCursorError("Invalid event cursor")
    return JobEventCursor(occurred_at=timestamp, event_id=identifier)


def _encode_cursor(kind: str, timestamp: datetime, identifier: object) -> str:
    payload = json.dumps(
        [kind, timestamp.astimezone(UTC).isoformat(), identifier],
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def _decode_cursor(cursor: str, *, expected_kind: str) -> tuple[datetime, object]:
    if not cursor or len(cursor) > _MAX_CURSOR_LENGTH:
        raise InvalidCursorError("Invalid pagination cursor")
    try:
        raw = cursor.encode("ascii")
        raw += b"=" * (-len(raw) % 4)
        payload = json.loads(base64.b64decode(raw, altchars=b"-_", validate=True))
        if not isinstance(payload, list) or len(payload) != 3:
            raise ValueError
        kind, timestamp_value, identifier = payload
        if kind != expected_kind or not isinstance(timestamp_value, str):
            raise ValueError
        timestamp = datetime.fromisoformat(timestamp_value)
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError
        return timestamp, identifier
    except (UnicodeError, binascii.Error, json.JSONDecodeError, ValueError):
        raise InvalidCursorError("Invalid pagination cursor") from None


def _close_session(session: Session | None) -> None:
    if session is None:
        return
    try:
        session.close()
    except Exception:
        pass


__all__ = ["GetAudioJob", "ListAudioJobEvents", "ListAudioJobs"]
