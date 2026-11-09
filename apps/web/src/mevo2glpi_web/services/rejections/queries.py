"""Read-only use case for rejected mail ingestions."""

from __future__ import annotations

import base64
import binascii
import json
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from mevo2glpi_database import MailIngestionCursor, MailIngestionRepository

from ..jobs import InvalidCursorError
from .models import (
    RejectedIngestionListPage,
    RejectedIngestionQueryUnavailableError,
    RejectedIngestionView,
)

_CURSOR_KIND = "rejected_ingestions"
_MAX_CURSOR_LENGTH = 512


class ListRejectedIngestions:
    """Return a bounded page without exposing Exchange identifiers."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def execute(
        self,
        *,
        limit: int,
        cursor: str | None = None,
    ) -> RejectedIngestionListPage:
        """List terminal rejections ordered by completion, newest first."""
        repository_cursor = _decode_cursor(cursor) if cursor else None
        session: Session | None = None
        try:
            session = self._session_factory()
            page = MailIngestionRepository(session).list_rejected(
                page_size=limit,
                cursor=repository_cursor,
            )
            return RejectedIngestionListPage(
                items=tuple(
                    RejectedIngestionView(
                        ingestion_uuid=item.uuid,
                        rejection_category=item.rejection_category,
                        rejection_code=item.rejection_code,
                        received_at=item.received_at,
                        rejected_at=item.finished_at,
                    )
                    for item in page.items
                ),
                next_cursor=(
                    _encode_cursor(page.next_cursor)
                    if page.next_cursor is not None
                    else None
                ),
            )
        except (InvalidCursorError, RejectedIngestionQueryUnavailableError):
            raise
        except Exception as error:
            raise RejectedIngestionQueryUnavailableError(
                "Rejected-ingestion persistence is unavailable"
            ) from error
        finally:
            _close_session(session)


def _encode_cursor(cursor: MailIngestionCursor) -> str:
    payload = json.dumps(
        [
            _CURSOR_KIND,
            cursor.finished_at.astimezone(UTC).isoformat(),
            str(cursor.uuid),
        ],
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def _decode_cursor(cursor: str) -> MailIngestionCursor:
    if not cursor or len(cursor) > _MAX_CURSOR_LENGTH:
        raise InvalidCursorError("Invalid rejected-ingestion cursor")
    try:
        raw = cursor.encode("ascii")
        raw += b"=" * (-len(raw) % 4)
        payload = json.loads(base64.b64decode(raw, altchars=b"-_", validate=True))
        if not isinstance(payload, list) or len(payload) != 3:
            raise ValueError
        kind, timestamp_value, identifier = payload
        if kind != _CURSOR_KIND or not isinstance(timestamp_value, str):
            raise ValueError
        timestamp = datetime.fromisoformat(timestamp_value)
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError
        return MailIngestionCursor(
            finished_at=timestamp,
            uuid=UUID(identifier),
        )
    except (
        UnicodeError,
        binascii.Error,
        json.JSONDecodeError,
        TypeError,
        ValueError,
    ):
        raise InvalidCursorError("Invalid rejected-ingestion cursor") from None


def _close_session(session: Session | None) -> None:
    if session is None:
        return
    try:
        session.close()
    except Exception:
        pass


__all__ = ["ListRejectedIngestions"]
