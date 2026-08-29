"""Automation heartbeat persistence on the singleton PostgreSQL connection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy.engine import Engine

from glpi_ticketing_common.cooperative_wait import wait_with_periodic_checkpoint
from glpi_ticketing_database import (
    AdvisoryLockReleasedError,
    ErrorScope,
    JobStage,
    WorkerRuntimeState,
    WorkerRuntimeStatus,
    WorkerServiceName,
    create_session_factory,
    try_acquire_advisory_lock,
)
from glpi_ticketing_worker_automation.runtime import RuntimeHeartbeatReporter
from glpi_ticketing_worker_automation.worker import (
    FailureDetails,
    RetryOutcome,
)

pytestmark = pytest.mark.database

AUDIO_UUID = UUID("123e4567-e89b-42d3-a456-426614174000")


@dataclass(slots=True)
class _MutableClocks:
    wall_time: datetime = datetime(2026, 8, 29, 10, tzinfo=UTC)
    monotonic_time: float = 100.0

    def now(self) -> datetime:
        return self.wall_time

    def monotonic(self) -> float:
        return self.monotonic_time

    def advance(self, seconds: float) -> None:
        self.wall_time += timedelta(seconds=seconds)
        self.monotonic_time += seconds


def test_due_heartbeat_commits_on_lock_connection_and_is_externally_visible(
    migrated_engine: Engine,
) -> None:
    lock = try_acquire_advisory_lock(
        migrated_engine,
        WorkerServiceName.AUTOMATION,
    )
    assert lock is not None
    clocks = _MutableClocks()
    reporter = RuntimeHeartbeatReporter(
        lock,
        heartbeat_interval_seconds=10,
        now=clocks.now,
        monotonic=clocks.monotonic,
    )
    session_factory = create_session_factory(migrated_engine)

    try:
        reporter.record_state(WorkerRuntimeState.RUNNING)
        clocks.advance(9)
        assert reporter.pulse_if_due() is False

        with session_factory() as session:
            initial = session.get(
                WorkerRuntimeStatus,
                WorkerServiceName.AUTOMATION.value,
            )
            assert initial is not None
            assert initial.last_heartbeat_at == datetime(
                2026,
                8,
                29,
                10,
                tzinfo=UTC,
            )

        clocks.advance(1)
        assert reporter.pulse_if_due() is True

        with session_factory() as session:
            refreshed = session.get(
                WorkerRuntimeStatus,
                WorkerServiceName.AUTOMATION.value,
            )
            assert refreshed is not None
            assert refreshed.state == WorkerRuntimeState.RUNNING.value
            assert refreshed.last_heartbeat_at == datetime(
                2026,
                8,
                29,
                10,
                0,
                10,
                tzinfo=UTC,
            )

        lock.release()
        clocks.advance(10)
        with pytest.raises(AdvisoryLockReleasedError):
            reporter.pulse_if_due()
    finally:
        if not lock.is_released:
            lock.release()


def test_cooperative_wait_refreshes_sticky_sanitized_degraded_context(
    migrated_engine: Engine,
) -> None:
    lock = try_acquire_advisory_lock(
        migrated_engine,
        WorkerServiceName.AUTOMATION,
    )
    assert lock is not None
    clocks = _MutableClocks()
    reporter = RuntimeHeartbeatReporter(
        lock,
        heartbeat_interval_seconds=10,
        now=clocks.now,
        monotonic=clocks.monotonic,
    )
    session_factory = create_session_factory(migrated_engine)
    waits: list[float] = []

    def advance_wait(seconds: float) -> None:
        waits.append(seconds)
        clocks.advance(seconds)

    def checkpoint() -> None:
        lock.ensure_held()
        reporter.pulse_if_due()

    try:
        reporter.report_outcome(
            RetryOutcome(
                audio_uuid=AUDIO_UUID,
                stage=JobStage.GLPI_CREATION,
                failure=FailureDetails(
                    scope=ErrorScope.SYSTEM,
                    category="unavailable",
                    technical_code="provider_unavailable",
                ),
                next_attempt_at=clocks.now() + timedelta(minutes=1),
            )
        )

        wait_with_periodic_checkpoint(
            25,
            checkpoint_interval_seconds=10,
            wait_once=advance_wait,
            should_stop=lambda: False,
            checkpoint=checkpoint,
        )

        with session_factory() as session:
            refreshed = session.get(
                WorkerRuntimeStatus,
                WorkerServiceName.AUTOMATION.value,
            )
            assert refreshed is not None
            assert refreshed.state == WorkerRuntimeState.DEGRADED.value
            assert refreshed.current_job_uuid is None
            assert refreshed.last_error_category == "unavailable"
            assert refreshed.last_error_code == "provider_unavailable"
            assert refreshed.last_heartbeat_at == datetime(
                2026,
                8,
                29,
                10,
                0,
                20,
                tzinfo=UTC,
            )

        assert waits == [10, 10, 5]
    finally:
        if not lock.is_released:
            lock.release()
