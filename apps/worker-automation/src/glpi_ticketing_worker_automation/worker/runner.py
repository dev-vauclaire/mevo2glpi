"""Scheduling loop for the stateful audio processor."""

import time
from collections.abc import Callable
from typing import Never, Protocol, assert_never

from .models import (
    BlockedOutcome,
    CompletedOutcome,
    FailedOutcome,
    IdleOutcome,
    RetryOutcome,
    RunOutcome,
)


class _RunOnceProtocol(Protocol):
    def run_once(self) -> RunOutcome:
        """Advance one audio to an observable boundary."""
        ...


class WorkerBlockedError(RuntimeError):
    """Stop the process when automatic progress is no longer safe."""

    def __init__(self, outcome: BlockedOutcome) -> None:
        self.outcome = outcome
        super().__init__(
            "worker blocked "
            f"audio_uuid={outcome.audio_uuid} "
            f"stage={outcome.stage.value} "
            f"code={outcome.failure.technical_code}"
        )


def run_forever(
    processor: _RunOnceProtocol,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> Never:
    """Run until an unexpected exception or an explicit blocked outcome."""
    while True:
        outcome = processor.run_once()
        if isinstance(outcome, IdleOutcome):
            sleep(outcome.wait_seconds)
        elif isinstance(outcome, RetryOutcome):
            sleep(outcome.retry_after_seconds)
        elif isinstance(outcome, CompletedOutcome | FailedOutcome):
            continue
        elif isinstance(outcome, BlockedOutcome):
            raise WorkerBlockedError(outcome)
        else:
            assert_never(outcome)
