"""Read-only observability use case for the internal dashboard."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy.orm import Session, sessionmaker

from glpi_ticketing_database import (
    AudioRetentionRepository,
    TicketCreationJobRepository,
    WorkerRuntimeStatusRepository,
)

from .models import (
    ObservabilitySnapshot,
    ObservabilityThresholds,
    ObservabilityUnavailableError,
)
from .policies import ObservabilityPolicy


class GetObservability:
    """Read all bounded operational summaries from one short DB session."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        thresholds: ObservabilityThresholds,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not callable(session_factory):
            raise TypeError("session_factory must be callable")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._session_factory = session_factory
        self._thresholds = thresholds
        self._policy = ObservabilityPolicy(thresholds)
        self._clock = clock

    def execute(self) -> ObservabilitySnapshot:
        """Return an internally consistent snapshot or a sanitized failure."""
        session: Session | None = None
        try:
            observed_at = _utc_datetime(self._clock())
            session = self._session_factory()
            runtimes = WorkerRuntimeStatusRepository(session).list_all()
            jobs = TicketCreationJobRepository(session).get_alert_summary(
                pending_before=(observed_at - self._thresholds.pending_alert_after),
                retry_before=(observed_at - self._thresholds.retry_overdue_grace),
            )
            retentions = AudioRetentionRepository(session).get_alert_summary(
                retry_before=(observed_at - self._thresholds.retry_overdue_grace),
                deadline_before=(
                    observed_at - self._thresholds.retention_deadline_grace
                ),
            )
            return self._policy.evaluate(
                observed_at=observed_at,
                runtimes=runtimes,
                jobs=jobs,
                retentions=retentions,
            )
        except Exception as error:
            raise ObservabilityUnavailableError(
                "Operational state is temporarily unavailable"
            ) from error
        finally:
            _close_session(session)


def _utc_datetime(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("clock must return a datetime")
    if value.utcoffset() is None:
        raise ValueError("clock must return a timezone-aware datetime")
    return value.astimezone(UTC)


def _close_session(session: Session | None) -> None:
    if session is None:
        return
    try:
        session.close()
    except Exception:
        pass


__all__ = ["GetObservability"]
