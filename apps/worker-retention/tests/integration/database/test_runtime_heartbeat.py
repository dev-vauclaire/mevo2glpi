"""Retention heartbeat persistence on the singleton PostgreSQL connection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import Engine, text

from glpi_ticketing_common.cooperative_wait import wait_with_periodic_checkpoint
from glpi_ticketing_database import (
    AdvisoryLockConnectionLostError,
    AudioExtension,
    ErrorScope,
    JobStatus,
    TicketCreationJob,
    WorkerRuntimeState,
    WorkerRuntimeStatus,
    WorkerServiceName,
    create_session_factory,
    try_acquire_advisory_lock,
)
from glpi_ticketing_worker_retention.retention import (
    RetentionFailure,
    RetentionOperation,
    RetryScheduledOutcome,
    SystemBlockedOutcome,
)
from glpi_ticketing_worker_retention.runtime.heartbeat import (
    RetentionRuntimeHeartbeatReporter,
)

pytestmark = pytest.mark.database

JOB_UUID = UUID("123e4567-e89b-42d3-a456-426614174301")


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


def test_wait_persists_sticky_health_and_fails_closed_after_lock_loss(
    migrated_engine: Engine,
) -> None:
    lock = try_acquire_advisory_lock(
        migrated_engine,
        WorkerServiceName.RETENTION,
    )
    assert lock is not None
    clocks = _MutableClocks()
    reporter = RetentionRuntimeHeartbeatReporter(
        lock,
        heartbeat_interval_seconds=10,
        now=clocks.now,
        monotonic=clocks.monotonic,
    )
    session_factory = create_session_factory(migrated_engine)

    try:
        with session_factory() as session, session.begin():
            session.add(
                TicketCreationJob(
                    uuid=JOB_UUID,
                    status=JobStatus.PENDING.value,
                    stage=None,
                    storage_key=f"{JOB_UUID}.wav",
                    audio_extension=AudioExtension.WAV.value,
                    audio_size_bytes=1,
                    created_at=clocks.now(),
                    updated_at=clocks.now(),
                )
            )

        reporter.record_state(WorkerRuntimeState.RUNNING)
        reporter.start_job(JOB_UUID)
        with session_factory() as session:
            active = session.get(
                WorkerRuntimeStatus,
                WorkerServiceName.RETENTION.value,
            )
            assert active is not None
            assert active.state == WorkerRuntimeState.RUNNING.value
            assert active.current_job_uuid == JOB_UUID

        reporter.report_outcome(
            RetryScheduledOutcome(
                job_uuid=JOB_UUID,
                operation=RetentionOperation.GLPI_CHECK,
                failure=RetentionFailure(
                    scope=ErrorScope.SYSTEM,
                    category="unavailable",
                    technical_code="glpi_unavailable",
                ),
                next_attempt_at=clocks.now() + timedelta(minutes=1),
            )
        )

        def checkpoint() -> None:
            lock.ensure_held()
            assert reporter.pulse_if_due() is True

        wait_with_periodic_checkpoint(
            20,
            checkpoint_interval_seconds=10,
            wait_once=clocks.advance,
            should_stop=lambda: False,
            checkpoint=checkpoint,
        )

        with session_factory() as session:
            degraded = session.get(
                WorkerRuntimeStatus,
                WorkerServiceName.RETENTION.value,
            )
            assert degraded is not None
            assert degraded.state == WorkerRuntimeState.DEGRADED.value
            assert degraded.last_heartbeat_at == datetime(
                2026,
                8,
                29,
                10,
                0,
                20,
                tzinfo=UTC,
            )
            assert degraded.current_job_uuid is None
            assert degraded.last_error_category == "unavailable"
            assert degraded.last_error_code == "glpi_unavailable"

        reporter.report_outcome(
            SystemBlockedOutcome(
                job_uuid=JOB_UUID,
                operation=RetentionOperation.AUDIO_PURGE,
                failure=RetentionFailure(
                    scope=ErrorScope.SYSTEM,
                    category="storage_unavailable",
                    technical_code="purge_forbidden",
                ),
                next_probe_at=clocks.now() + timedelta(minutes=1),
            )
        )
        wait_with_periodic_checkpoint(
            10,
            checkpoint_interval_seconds=10,
            wait_once=clocks.advance,
            should_stop=lambda: False,
            checkpoint=checkpoint,
        )

        with session_factory() as session:
            blocked = session.get(
                WorkerRuntimeStatus,
                WorkerServiceName.RETENTION.value,
            )
            assert blocked is not None
            assert blocked.state == WorkerRuntimeState.BLOCKED.value
            assert blocked.last_heartbeat_at == datetime(
                2026,
                8,
                29,
                10,
                0,
                30,
                tzinfo=UTC,
            )
            assert blocked.current_job_uuid is None
            assert blocked.last_error_category == "storage_unavailable"
            assert blocked.last_error_code == "purge_forbidden"

        lock_session = lock.open_session()
        try:
            backend_pid = lock_session.scalar(text("SELECT pg_backend_pid()"))
        finally:
            lock_session.close()
        assert isinstance(backend_pid, int)
        with migrated_engine.begin() as connection:
            assert (
                connection.scalar(
                    text("SELECT pg_terminate_backend(:backend_pid)"),
                    {"backend_pid": backend_pid},
                )
                is True
            )

        clocks.advance(10)
        with pytest.raises(AdvisoryLockConnectionLostError):
            reporter.pulse_if_due()
    finally:
        if not lock.is_released:
            lock.release()
