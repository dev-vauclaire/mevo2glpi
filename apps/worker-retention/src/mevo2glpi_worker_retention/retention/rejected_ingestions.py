"""Periodic PostgreSQL retention for terminal rejected mail ingestions."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from .outcomes import RejectedIngestionsPurgedOutcome
from .ports import RejectedIngestionRetentionLifecycleProtocol

LOGGER = logging.getLogger(__name__)


class RejectedIngestionRetentionService:
    """Schedule bounded rejected-ingestion housekeeping without external I/O."""

    def __init__(
        self,
        *,
        lifecycle: RejectedIngestionRetentionLifecycleProtocol,
        retention_period: timedelta,
        scan_interval: timedelta,
        batch_size: int,
    ) -> None:
        if not callable(getattr(lifecycle, "purge_expired", None)):
            raise TypeError("lifecycle must expose purge_expired()")
        self._retention_period = _positive_timedelta(
            retention_period,
            "retention_period",
        )
        self._scan_interval = _positive_timedelta(scan_interval, "scan_interval")
        if isinstance(batch_size, bool) or not isinstance(batch_size, int):
            raise TypeError("batch_size must be an integer")
        if batch_size <= 0:
            raise ValueError("batch_size must be greater than zero")

        self._lifecycle = lifecycle
        self._batch_size = batch_size
        self._next_scan_at: datetime | None = None

    def run_if_due(
        self,
        now: datetime,
    ) -> RejectedIngestionsPurgedOutcome | None:
        """Run one due scan, draining full batches without a busy loop."""
        _require_aware_datetime(now, "now")
        if self._next_scan_at is not None and now < self._next_scan_at:
            return None

        cutoff = now - self._retention_period
        purged_count = self._lifecycle.purge_expired(
            cutoff,
            limit=self._batch_size,
        )
        next_scan_at = (
            now if purged_count == self._batch_size else now + self._scan_interval
        )
        self._next_scan_at = next_scan_at
        LOGGER.info(
            "rejected ingestion retention applied purged_count=%s cutoff=%s",
            purged_count,
            cutoff.isoformat(),
        )
        return RejectedIngestionsPurgedOutcome(
            purged_count=purged_count,
            cutoff=cutoff,
            next_scan_at=next_scan_at,
        )


def _positive_timedelta(value: object, field_name: str) -> timedelta:
    if not isinstance(value, timedelta):
        raise TypeError(f"{field_name} must be a timedelta")
    if value <= timedelta(0):
        raise ValueError(f"{field_name} must be greater than zero")
    return value


def _require_aware_datetime(value: object, field_name: str) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    if value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


__all__ = [
    "RejectedIngestionRetentionService",
]
