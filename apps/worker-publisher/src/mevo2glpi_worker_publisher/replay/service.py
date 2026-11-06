"""Application-owned transactions for operator-requested ingestion replay."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol, TypeVar
from uuid import UUID

from sqlalchemy.orm import Session

from mevo2glpi_database import (
    MailIngestionEventRepository,
    MailIngestionEventType,
    MailIngestionReplayFilter,
    MailIngestionRepository,
    MailIngestionSnapshot,
)

from .models import ReplayMode, ReplayReport, ReplayRequest

_ResultT = TypeVar("_ResultT")


class _IngestionRepositoryProtocol(Protocol):
    def list_replay_candidates(
        self,
        replay_filter: MailIngestionReplayFilter,
    ) -> tuple[MailIngestionSnapshot, ...]: ...

    def rearm_for_operator_replay(
        self,
        ingestion_uuid: UUID,
        *,
        expected: MailIngestionReplayFilter,
        now: datetime,
    ) -> MailIngestionSnapshot: ...


class _EventRepositoryProtocol(Protocol):
    def append(self, ingestion_uuid: UUID, **kwargs: object) -> None: ...


IngestionRepositoryFactory = Callable[[Session], _IngestionRepositoryProtocol]
EventRepositoryFactory = Callable[[Session], _EventRepositoryProtocol]


class PublisherReplayService:
    """Select and rearm ingestions without knowing their attachment format."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        ownership_guard: Callable[[], None] | None = None,
        now: Callable[[], datetime] | None = None,
        ingestion_repository_factory: IngestionRepositoryFactory = (
            MailIngestionRepository
        ),
        event_repository_factory: EventRepositoryFactory = (
            MailIngestionEventRepository
        ),
    ) -> None:
        if not callable(session_factory):
            raise TypeError("session_factory must be callable")
        if ownership_guard is not None and not callable(ownership_guard):
            raise TypeError("ownership_guard must be callable")
        if now is not None and not callable(now):
            raise TypeError("now must be callable")
        if not callable(ingestion_repository_factory):
            raise TypeError("ingestion_repository_factory must be callable")
        if not callable(event_repository_factory):
            raise TypeError("event_repository_factory must be callable")
        self._session_factory = session_factory
        self._ownership_guard = ownership_guard or _no_op
        self._now = now or (lambda: datetime.now(UTC))
        self._ingestion_repository_factory = ingestion_repository_factory
        self._event_repository_factory = event_repository_factory

    def execute(self, request: ReplayRequest) -> ReplayReport:
        """Return candidates only, or rearm each one in its own transaction."""
        if not isinstance(request, ReplayRequest):
            raise TypeError("request must be a ReplayRequest")

        candidates = self._transaction(
            lambda session: self._ingestion_repository_factory(
                session
            ).list_replay_candidates(request.replay_filter)
        )
        candidate_uuids = tuple(candidate.uuid for candidate in candidates)
        if request.mode is ReplayMode.DRY_RUN:
            return ReplayReport(
                mode=request.mode,
                selected_ingestion_uuids=candidate_uuids,
            )

        rearmed_uuids: list[UUID] = []
        for ingestion_uuid in candidate_uuids:
            rearmed = self._transaction(
                lambda session, ingestion_uuid=ingestion_uuid: self._rearm_one(
                    session,
                    ingestion_uuid,
                    replay_filter=request.replay_filter,
                )
            )
            rearmed_uuids.append(rearmed.uuid)
        return ReplayReport(
            mode=request.mode,
            selected_ingestion_uuids=candidate_uuids,
            rearmed_ingestion_uuids=tuple(rearmed_uuids),
        )

    def _rearm_one(
        self,
        session: Session,
        ingestion_uuid: UUID,
        *,
        replay_filter: MailIngestionReplayFilter,
    ) -> MailIngestionSnapshot:
        occurred_at = self._now()
        repository = self._ingestion_repository_factory(session)
        rearmed = repository.rearm_for_operator_replay(
            ingestion_uuid,
            expected=replay_filter,
            now=occurred_at,
        )
        self._event_repository_factory(session).append(
            rearmed.uuid,
            occurred_at=occurred_at,
            status=rearmed.status,
            stage=rearmed.stage,
            disposition=rearmed.disposition,
            event_type=MailIngestionEventType.OPERATOR_REPLAY_REQUESTED,
        )
        return rearmed

    def _transaction(self, operation: Callable[[Session], _ResultT]) -> _ResultT:
        self._ownership_guard()
        session = self._session_factory()
        try:
            with session.begin():
                result = operation(session)
        finally:
            session.close()
        self._ownership_guard()
        return result


def _no_op() -> None:
    return None


__all__ = ["PublisherReplayService"]
