"""Application-level lifecycle access to the disposable E2E PostgreSQL database."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from glpi_ticketing_database import (
    AudioRetention,
    AudioRetentionRepository,
    AudioRetentionSnapshot,
    JobSnapshot,
    RetentionStatus,
    TicketCreationJobRepository,
    create_database_engine,
    create_session_factory,
)
from glpi_ticketing_worker_automation.worker import AutomationJobLifecycleService


@dataclass(slots=True)
class E2eDatabase:
    """Own one host-side engine used for application transitions and test time."""

    _engine: Engine = field(repr=False)
    session_factory: sessionmaker[Session] = field(repr=False)

    @classmethod
    def connect(cls, database_url: str) -> E2eDatabase:
        """Open a bounded host-side connection pool without exposing its URL."""
        engine = create_database_engine(
            database_url,
            connect_timeout_seconds=5,
        )
        return cls(
            _engine=engine,
            session_factory=create_session_factory(engine),
        )

    @property
    def lifecycle(self) -> AutomationJobLifecycleService:
        """Return the real automation application service over this database."""
        return AutomationJobLifecycleService(session_factory=self.session_factory)

    def get_job_snapshot(self, audio_uuid: UUID) -> JobSnapshot | None:
        """Read one typed job snapshot through its model repository."""
        if not isinstance(audio_uuid, UUID):
            raise TypeError("audio_uuid must be a UUID")
        with self.session_factory() as session:
            return TicketCreationJobRepository(session).get_by_uuid(audio_uuid)

    def get_retention_snapshot(
        self,
        audio_uuid: UUID,
    ) -> AudioRetentionSnapshot | None:
        """Read one typed retention snapshot through its model repository."""
        if not isinstance(audio_uuid, UUID):
            raise TypeError("audio_uuid must be a UUID")
        with self.session_factory() as session:
            return AudioRetentionRepository(session).get_by_job_uuid(audio_uuid)

    def force_retention_timestamps(
        self,
        audio_uuid: UUID,
        *,
        hard_delete_at: datetime,
        next_check_at: datetime,
    ) -> None:
        """Move a scheduled retention clock without changing lifecycle state."""
        if not isinstance(audio_uuid, UUID):
            raise TypeError("audio_uuid must be a UUID")
        normalized_hard_delete_at = _utc_datetime(
            hard_delete_at,
            "hard_delete_at",
        )
        normalized_next_check_at = _utc_datetime(
            next_check_at,
            "next_check_at",
        )
        if normalized_next_check_at > normalized_hard_delete_at:
            raise ValueError("next_check_at must not be later than hard_delete_at")

        with self.session_factory.begin() as session:
            result = session.execute(
                update(AudioRetention)
                .where(
                    AudioRetention.job_uuid == audio_uuid,
                    AudioRetention.status == RetentionStatus.SCHEDULED.value,
                )
                .values(
                    hard_delete_at=normalized_hard_delete_at,
                    next_check_at=normalized_next_check_at,
                )
            )
            if result.rowcount != 1:
                raise AssertionError(
                    "retention timestamp control requires one scheduled retention"
                )

    def close(self) -> None:
        """Dispose every host-side database connection."""
        self._engine.dispose()


@contextmanager
def open_e2e_database(database_url: str) -> Iterator[E2eDatabase]:
    """Yield one database handle and always dispose its connection pool."""
    database = E2eDatabase.connect(database_url)
    try:
        yield database
    finally:
        database.close()


def _utc_datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    if value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


__all__ = ["E2eDatabase", "open_e2e_database"]
