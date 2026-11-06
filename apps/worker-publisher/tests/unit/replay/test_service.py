from __future__ import annotations

from contextlib import AbstractContextManager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

from mevo2glpi_database import (
    MailIngestionDisposition,
    MailIngestionEventType,
    MailIngestionReplayFilter,
    MailIngestionStage,
    MailIngestionStatus,
)
from mevo2glpi_worker_publisher.replay import (
    PublisherReplayService,
    ReplayMode,
    ReplayRequest,
)

FIRST_UUID = UUID("00000000-0000-0000-0000-000000000201")
SECOND_UUID = UUID("00000000-0000-0000-0000-000000000202")
NOW = datetime(2026, 10, 26, 12, tzinfo=UTC)


class _Transaction(AbstractContextManager[None]):
    def __init__(self, session: _Session) -> None:
        self._session = session

    def __enter__(self) -> None:
        self._session.transactions_started += 1

    def __exit__(self, *_args: object) -> None:
        self._session.transactions_finished += 1


class _Session:
    def __init__(self) -> None:
        self.transactions_started = 0
        self.transactions_finished = 0
        self.closed = False

    def begin(self) -> _Transaction:
        return _Transaction(self)

    def close(self) -> None:
        self.closed = True


class _IngestionRepository:
    def __init__(self, candidates: tuple[UUID, ...]) -> None:
        self.candidates = candidates
        self.listed_filters: list[MailIngestionReplayFilter] = []
        self.rearmed: list[tuple[UUID, MailIngestionReplayFilter, datetime]] = []

    def list_replay_candidates(
        self,
        replay_filter: MailIngestionReplayFilter,
    ) -> tuple[SimpleNamespace, ...]:
        self.listed_filters.append(replay_filter)
        return tuple(SimpleNamespace(uuid=value) for value in self.candidates)

    def rearm_for_operator_replay(
        self,
        ingestion_uuid: UUID,
        *,
        expected: MailIngestionReplayFilter,
        now: datetime,
    ) -> SimpleNamespace:
        self.rearmed.append((ingestion_uuid, expected, now))
        return SimpleNamespace(
            uuid=ingestion_uuid,
            status=MailIngestionStatus.PENDING,
            stage=MailIngestionStage.DISCOVERY,
            disposition=None,
        )


class _EventRepository:
    def __init__(self) -> None:
        self.events: list[tuple[UUID, dict[str, object]]] = []

    def append(self, ingestion_uuid: UUID, **kwargs: object) -> None:
        self.events.append((ingestion_uuid, kwargs))


def _filter() -> MailIngestionReplayFilter:
    return MailIngestionReplayFilter(
        status=MailIngestionStatus.FAILED,
        disposition=MailIngestionDisposition.REJECTED,
        rejection_category="invalid_attachment",
        rejection_code="wav_attachment_missing",
        job_uuid_is_null=True,
        attachment_id_is_not_null=True,
        received_after=datetime(2026, 9, 1, tzinfo=UTC),
        received_before=datetime(2026, 10, 1, tzinfo=UTC),
        limit=100,
    )


def _service(
    repository: _IngestionRepository,
    events: _EventRepository,
) -> tuple[PublisherReplayService, list[_Session], list[str]]:
    sessions: list[_Session] = []
    ownership_checks: list[str] = []

    def open_session() -> _Session:
        session = _Session()
        sessions.append(session)
        return session

    service = PublisherReplayService(
        session_factory=open_session,  # type: ignore[arg-type]
        ownership_guard=lambda: ownership_checks.append("checked"),
        now=lambda: NOW,
        ingestion_repository_factory=lambda _session: repository,
        event_repository_factory=lambda _session: events,
    )
    return service, sessions, ownership_checks


def test_dry_run_only_lists_candidates() -> None:
    repository = _IngestionRepository((FIRST_UUID, SECOND_UUID))
    events = _EventRepository()
    service, sessions, ownership_checks = _service(repository, events)

    report = service.execute(
        ReplayRequest(replay_filter=_filter(), mode=ReplayMode.DRY_RUN)
    )

    assert report.selected_ingestion_uuids == (FIRST_UUID, SECOND_UUID)
    assert report.rearmed_ingestion_uuids == ()
    assert repository.rearmed == []
    assert events.events == []
    assert len(sessions) == 1
    assert sessions[0].closed is True
    assert sessions[0].transactions_started == 1
    assert sessions[0].transactions_finished == 1
    assert ownership_checks == ["checked", "checked"]


def test_apply_rearms_each_candidate_and_appends_an_audit_event() -> None:
    replay_filter = _filter()
    repository = _IngestionRepository((FIRST_UUID, SECOND_UUID))
    events = _EventRepository()
    service, sessions, ownership_checks = _service(repository, events)

    report = service.execute(
        ReplayRequest(replay_filter=replay_filter, mode=ReplayMode.APPLY)
    )

    assert report.selected_ingestion_uuids == (FIRST_UUID, SECOND_UUID)
    assert report.rearmed_ingestion_uuids == (FIRST_UUID, SECOND_UUID)
    assert repository.rearmed == [
        (FIRST_UUID, replay_filter, NOW),
        (SECOND_UUID, replay_filter, NOW),
    ]
    assert [event[0] for event in events.events] == [FIRST_UUID, SECOND_UUID]
    for _, event in events.events:
        assert event == {
            "occurred_at": NOW,
            "status": MailIngestionStatus.PENDING,
            "stage": MailIngestionStage.DISCOVERY,
            "disposition": None,
            "event_type": MailIngestionEventType.OPERATOR_REPLAY_REQUESTED,
        }
    assert len(sessions) == 3
    assert all(session.closed for session in sessions)
    assert all(session.transactions_started == 1 for session in sessions)
    assert all(session.transactions_finished == 1 for session in sessions)
    assert ownership_checks == ["checked", "checked"] * 3
