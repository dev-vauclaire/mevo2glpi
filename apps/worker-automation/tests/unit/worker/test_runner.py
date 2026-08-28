from collections import deque
from datetime import UTC, datetime
from uuid import UUID

import pytest

from glpi_ticketing_worker_automation.worker import (
    BlockedOutcome,
    CompletedOutcome,
    ErrorScope,
    FailedOutcome,
    FailureDetails,
    IdleOutcome,
    InterruptedOutcome,
    JobStage,
    RetryOutcome,
    RunOutcome,
    SystemBlockedOutcome,
    run_forever,
)

AUDIO_UUID = UUID("123e4567-e89b-42d3-a456-426614174000")
NOW = datetime(2026, 8, 28, 10, 0, tzinfo=UTC)


class ScriptedWorker:
    def __init__(self, outcomes: list[RunOutcome]) -> None:
        self._outcomes = deque(outcomes)
        self.calls = 0

    def run_once(self) -> RunOutcome:
        self.calls += 1
        return self._outcomes.popleft()


class StopLoop(Exception):
    pass


def test_run_forever_uses_idle_outcome_wait() -> None:
    worker = ScriptedWorker([IdleOutcome(wait_seconds=2.5)])
    sleep_calls: list[float] = []

    def stop_after_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        raise StopLoop

    with pytest.raises(StopLoop):
        run_forever(worker, sleep=stop_after_sleep)

    assert sleep_calls == [2.5]


def test_retry_does_not_sleep_and_allows_another_job_immediately() -> None:
    worker = ScriptedWorker(
        [
            RetryOutcome(
                audio_uuid=AUDIO_UUID,
                stage=JobStage.TRANSCRIPTION,
                failure=FailureDetails(
                    scope=ErrorScope.SYSTEM,
                    category="unavailable",
                    technical_code="transport_error",
                ),
                next_attempt_at=NOW,
            ),
            CompletedOutcome(audio_uuid=AUDIO_UUID, ticket_id=42),
            IdleOutcome(wait_seconds=1),
        ]
    )

    with pytest.raises(StopLoop):
        run_forever(worker, sleep=lambda _seconds: (_ for _ in ()).throw(StopLoop))

    assert worker.calls == 3


def test_failed_and_job_blocked_outcomes_do_not_stop_consumption() -> None:
    failure = FailureDetails(
        scope=ErrorScope.JOB,
        category="terminal",
        technical_code="remote_job_failed",
    )
    worker = ScriptedWorker(
        [
            FailedOutcome(
                audio_uuid=AUDIO_UUID,
                stage=JobStage.TRANSCRIPTION,
                failure=failure,
            ),
            BlockedOutcome(
                audio_uuid=AUDIO_UUID,
                stage=JobStage.GLPI_CREATION,
                failure=FailureDetails(
                    scope=ErrorScope.JOB,
                    category="conflict",
                    technical_code="duplicate_external_id",
                ),
            ),
            IdleOutcome(wait_seconds=1),
        ]
    )

    with pytest.raises(StopLoop):
        run_forever(worker, sleep=lambda _seconds: (_ for _ in ()).throw(StopLoop))

    assert worker.calls == 3


def test_system_block_waits_before_worker_probes_same_job() -> None:
    blocked = SystemBlockedOutcome(
        audio_uuid=AUDIO_UUID,
        stage=JobStage.TRANSCRIPTION,
        failure=FailureDetails(
            scope=ErrorScope.SYSTEM,
            category="authentication",
            technical_code="http_401",
        ),
        probe_after_seconds=60,
    )
    worker = ScriptedWorker(
        [
            blocked,
            CompletedOutcome(audio_uuid=AUDIO_UUID, ticket_id=42),
            IdleOutcome(wait_seconds=1),
        ]
    )
    sleep_calls: list[float] = []

    def sleep_then_stop(seconds: float) -> None:
        sleep_calls.append(seconds)
        if len(sleep_calls) == 2:
            raise StopLoop

    with pytest.raises(StopLoop):
        run_forever(worker, sleep=sleep_then_stop)

    assert sleep_calls == [60, 1]
    assert worker.calls == 3


def test_latched_system_block_never_reprocesses_the_job() -> None:
    worker = ScriptedWorker(
        [
            SystemBlockedOutcome(
                audio_uuid=AUDIO_UUID,
                stage=JobStage.TRANSCRIPTION,
                failure=FailureDetails(
                    scope=ErrorScope.SYSTEM,
                    category="persistence",
                    technical_code="submission_persistence_failed",
                ),
                probe_after_seconds=60,
                automatic_probe=False,
            ),
            CompletedOutcome(audio_uuid=AUDIO_UUID, ticket_id=42),
        ]
    )
    stop_requested = False
    sleep_calls: list[float] = []

    def request_stop(seconds: float) -> None:
        nonlocal stop_requested
        sleep_calls.append(seconds)
        stop_requested = True

    run_forever(
        worker,
        sleep=request_stop,
        should_stop=lambda: stop_requested,
    )

    assert worker.calls == 1
    assert sleep_calls == [60]


def test_run_forever_returns_when_cooperative_stop_is_requested() -> None:
    worker = ScriptedWorker([IdleOutcome(wait_seconds=10)])
    stop_requested = False

    def request_stop(_seconds: float) -> None:
        nonlocal stop_requested
        stop_requested = True

    run_forever(
        worker,
        sleep=request_stop,
        should_stop=lambda: stop_requested,
    )

    assert worker.calls == 1


def test_interrupted_outcome_stops_without_sleeping_or_scheduling() -> None:
    worker = ScriptedWorker(
        [
            InterruptedOutcome(
                audio_uuid=AUDIO_UUID,
                stage=JobStage.TRANSCRIPTION,
            ),
            CompletedOutcome(audio_uuid=AUDIO_UUID, ticket_id=42),
        ]
    )
    sleep_calls: list[float] = []

    run_forever(worker, sleep=sleep_calls.append)

    assert worker.calls == 1
    assert sleep_calls == []
