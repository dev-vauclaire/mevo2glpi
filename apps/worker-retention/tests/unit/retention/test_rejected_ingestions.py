"""Scheduling policy for rejected-ingestion PostgreSQL retention."""

from datetime import UTC, datetime, timedelta

import pytest

from mevo2glpi_worker_retention.retention.outcomes import (
    RejectedIngestionsPurgedOutcome,
)
from mevo2glpi_worker_retention.retention.rejected_ingestions import (
    RejectedIngestionRetentionService,
)

NOW = datetime(2026, 11, 9, 12, tzinfo=UTC)
CUTOFF = NOW - timedelta(days=90)


class FakeLifecycle:
    """Expose deterministic purge results."""

    def __init__(self, *, purged: tuple[int, ...]) -> None:
        self.purged = list(purged)
        self.calls: list[tuple[object, ...]] = []

    def purge_expired(self, cutoff: datetime, *, limit: int) -> int:
        self.calls.append(("purge_expired", cutoff, limit))
        if not self.purged:
            raise AssertionError("unexpected purge")
        return self.purged.pop(0)


def _service(
    lifecycle: FakeLifecycle,
) -> RejectedIngestionRetentionService:
    return RejectedIngestionRetentionService(
        lifecycle=lifecycle,
        retention_period=timedelta(days=90),
        scan_interval=timedelta(hours=6),
        batch_size=100,
    )


def test_scan_is_not_repeated_before_its_interval() -> None:
    lifecycle = FakeLifecycle(purged=(0,))
    service = _service(lifecycle)
    service.run_if_due(NOW)

    outcome = service.run_if_due(NOW + timedelta(hours=5, minutes=59))

    assert outcome is None
    assert lifecycle.calls == [("purge_expired", CUTOFF, 100)]


def test_purge_drains_a_full_batch_without_waiting_six_hours() -> None:
    lifecycle = FakeLifecycle(purged=(100, 7))
    service = _service(lifecycle)

    first = service.run_if_due(NOW)
    second = service.run_if_due(NOW)
    third = service.run_if_due(NOW + timedelta(seconds=1))

    assert first == RejectedIngestionsPurgedOutcome(
        purged_count=100,
        cutoff=CUTOFF,
        next_scan_at=NOW,
    )
    assert second == RejectedIngestionsPurgedOutcome(
        purged_count=7,
        cutoff=CUTOFF,
        next_scan_at=NOW + timedelta(hours=6),
    )
    assert third is None
    assert lifecycle.calls == [
        ("purge_expired", CUTOFF, 100),
        ("purge_expired", CUTOFF, 100),
    ]


@pytest.mark.parametrize(
    ("field", "value", "exception"),
    [
        ("retention_period", timedelta(0), ValueError),
        ("scan_interval", timedelta(0), ValueError),
        ("batch_size", 0, ValueError),
        ("batch_size", True, TypeError),
    ],
)
def test_configuration_rejects_invalid_values(
    field: str,
    value: object,
    exception: type[Exception],
) -> None:
    arguments: dict[str, object] = {
        "lifecycle": FakeLifecycle(purged=(0,)),
        "retention_period": timedelta(days=90),
        "scan_interval": timedelta(hours=6),
        "batch_size": 100,
    }
    arguments[field] = value

    with pytest.raises(exception):
        RejectedIngestionRetentionService(**arguments)  # type: ignore[arg-type]


def test_naive_scan_time_is_rejected() -> None:
    service = _service(FakeLifecycle(purged=(0,)))

    with pytest.raises(ValueError, match="timezone-aware"):
        service.run_if_due(datetime(2026, 11, 9, 12))
