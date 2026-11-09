"""Unit contracts for rejected-ingestion queries and opaque cursors."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

import mevo2glpi_web.services.rejections.queries as queries
from mevo2glpi_database import (
    MailIngestionCursor,
    MailIngestionPage,
    RejectedMailIngestionSnapshot,
)
from mevo2glpi_web.services import (
    InvalidCursorError,
    ListRejectedIngestions,
    RejectedIngestionQueryUnavailableError,
)

INGESTION_UUID = UUID("d952968c-36af-51d9-989c-c136bd2f13d2")
FINISHED_AT = datetime(2026, 10, 30, 9, 17, tzinfo=UTC)


@dataclass
class FakeSession:
    closed: bool = False

    def close(self) -> None:
        self.closed = True


@dataclass
class FakeFactory:
    session: FakeSession = field(default_factory=FakeSession)
    calls: int = 0

    def __call__(self) -> FakeSession:
        self.calls += 1
        return self.session


class FakeRepository:
    pages: list[MailIngestionPage] = []
    list_calls: list[dict[str, object]] = []
    error: Exception | None = None

    def __init__(self, _session: FakeSession) -> None:
        pass

    def list_rejected(self, **kwargs: object) -> MailIngestionPage:
        self.list_calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.pages.pop(0)


@pytest.fixture(autouse=True)
def fake_repository(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeRepository.pages = []
    FakeRepository.list_calls = []
    FakeRepository.error = None
    monkeypatch.setattr(queries, "MailIngestionRepository", FakeRepository)


def test_rejected_ingestions_round_trip_a_separate_opaque_cursor() -> None:
    snapshot = _snapshot()
    cursor = MailIngestionCursor(
        finished_at=snapshot.finished_at,
        uuid=snapshot.uuid,
    )
    FakeRepository.pages = [
        MailIngestionPage(items=(snapshot,), next_cursor=cursor, has_more=True),
        MailIngestionPage(items=(), next_cursor=None, has_more=False),
    ]
    factory = FakeFactory()
    service = ListRejectedIngestions(factory)  # type: ignore[arg-type]

    first_page = service.execute(limit=25)
    second_page = service.execute(limit=25, cursor=first_page.next_cursor)

    assert first_page.items[0].ingestion_uuid == INGESTION_UUID
    assert first_page.items[0].rejection_code == "audio_format_not_supported"
    assert first_page.next_cursor is not None
    assert str(INGESTION_UUID) not in first_page.next_cursor
    assert second_page.items == ()
    assert FakeRepository.list_calls == [
        {"page_size": 25, "cursor": None},
        {"page_size": 25, "cursor": cursor},
    ]
    assert factory.session.closed


@pytest.mark.parametrize("cursor", ["not-base64!", "e30", "a" * 513])
def test_invalid_rejection_cursor_is_rejected_before_opening_session(
    cursor: str,
) -> None:
    factory = FakeFactory()

    with pytest.raises(InvalidCursorError):
        ListRejectedIngestions(factory).execute(  # type: ignore[arg-type]
            limit=25,
            cursor=cursor,
        )

    assert factory.calls == 0


def test_job_cursor_cannot_be_used_for_rejected_ingestions() -> None:
    foreign_cursor = queries._encode_cursor(
        MailIngestionCursor(finished_at=FINISHED_AT, uuid=INGESTION_UUID)
    )
    raw = queries._decode_cursor(foreign_cursor)
    assert raw.uuid == INGESTION_UUID

    payload = (
        base64.urlsafe_b64encode(
            json.dumps(
                ["jobs", FINISHED_AT.isoformat(), str(INGESTION_UUID)],
                separators=(",", ":"),
            ).encode()
        )
        .rstrip(b"=")
        .decode("ascii")
    )

    with pytest.raises(InvalidCursorError):
        ListRejectedIngestions(FakeFactory()).execute(  # type: ignore[arg-type]
            limit=25,
            cursor=payload,
        )


def test_rejection_query_sanitizes_repository_failure_and_closes_session() -> None:
    FakeRepository.error = RuntimeError("secret database URL")
    factory = FakeFactory()

    with pytest.raises(RejectedIngestionQueryUnavailableError) as captured_error:
        ListRejectedIngestions(factory).execute(limit=25)  # type: ignore[arg-type]

    assert "secret database URL" not in str(captured_error.value)
    assert factory.session.closed


def _snapshot() -> RejectedMailIngestionSnapshot:
    return RejectedMailIngestionSnapshot(
        uuid=INGESTION_UUID,
        rejection_category="invalid_attachment",
        rejection_code="audio_format_not_supported",
        received_at=FINISHED_AT - timedelta(minutes=3),
        finished_at=FINISHED_AT,
    )
