from collections import deque
from pathlib import Path
from uuid import UUID

import pytest

from glpi_ticketing_worker_automation.worker import (
    BlockedOutcome,
    CompletedOutcome,
    FailedOutcome,
    FailureDetails,
    IdleOutcome,
    ProcessingStage,
    RetryOutcome,
    RunOutcome,
    WorkerBlockedError,
    run_forever,
)

AUDIO_UUID = UUID("123e4567-e89b-42d3-a456-426614174000")
AUDIO_FILENAME = f"{AUDIO_UUID}.m4a"


class ScriptedProcessor:
    def __init__(self, outcomes: list[RunOutcome]) -> None:
        self._outcomes = deque(outcomes)
        self.calls = 0

    def run_once(self) -> RunOutcome:
        self.calls += 1
        if not self._outcomes:
            raise AssertionError("unexpected run_once call")
        return self._outcomes.popleft()


class StopLoop(Exception):
    pass


def test_run_forever_uses_idle_outcome_wait() -> None:
    processor = ScriptedProcessor([IdleOutcome(wait_seconds=2.5)])
    sleep_calls: list[float] = []

    def stop_after_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        raise StopLoop

    with pytest.raises(StopLoop):
        run_forever(processor, sleep=stop_after_sleep)

    assert sleep_calls == [2.5]
    assert processor.calls == 1


def test_run_forever_uses_retry_outcome_wait() -> None:
    blocked = BlockedOutcome(
        audio_uuid=AUDIO_UUID,
        processing_path=Path("processing") / AUDIO_FILENAME,
        stage=ProcessingStage.TICKETING,
        failure=FailureDetails(
            category="authentication",
            technical_code="http_401",
        ),
    )
    processor = ScriptedProcessor(
        [
            RetryOutcome(
                audio_uuid=AUDIO_UUID,
                processing_path=Path("processing") / AUDIO_FILENAME,
                stage=ProcessingStage.TICKETING,
                failure=FailureDetails(
                    category="unavailable",
                    technical_code="transport_error",
                ),
                retry_after_seconds=3.5,
            ),
            CompletedOutcome(
                audio_uuid=AUDIO_UUID,
                completed_path=Path("completed") / AUDIO_FILENAME,
                ticket_id=42,
            ),
            blocked,
        ]
    )
    sleep_calls: list[float] = []

    with pytest.raises(WorkerBlockedError) as error_info:
        run_forever(processor, sleep=sleep_calls.append)

    assert sleep_calls == [3.5]
    assert processor.calls == 3
    assert error_info.value.outcome is blocked


def test_run_forever_continues_immediately_after_terminal_outcomes() -> None:
    blocked = BlockedOutcome(
        audio_uuid=AUDIO_UUID,
        processing_path=Path("processing") / AUDIO_FILENAME,
        stage=ProcessingStage.TICKETING,
        failure=FailureDetails(
            category="authentication",
            technical_code="http_401",
        ),
    )
    processor = ScriptedProcessor(
        [
            CompletedOutcome(
                audio_uuid=AUDIO_UUID,
                completed_path=Path("completed") / AUDIO_FILENAME,
                ticket_id=42,
            ),
            FailedOutcome(
                audio_uuid=AUDIO_UUID,
                failed_path=Path("failed") / AUDIO_FILENAME,
                error_sidecar_path=Path("failed") / f"{AUDIO_UUID}.error.json",
                stage=ProcessingStage.TRANSCRIPTION,
                failure=FailureDetails(
                    category="terminal",
                    technical_code="remote_job_failed",
                ),
            ),
            blocked,
        ]
    )
    sleep_calls: list[float] = []

    with pytest.raises(WorkerBlockedError) as error_info:
        run_forever(processor, sleep=sleep_calls.append)

    assert error_info.value.outcome is blocked
    assert processor.calls == 3
    assert sleep_calls == []
