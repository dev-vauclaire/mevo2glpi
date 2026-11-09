"""Scheduling loop for the singleton retention worker."""

import time
from collections.abc import Callable
from typing import assert_never

from .outcomes import (
    AudioDeletedOutcome,
    CheckRecoveredOutcome,
    DependencyWaitingOutcome,
    GlpiCheckRescheduledOutcome,
    IdleOutcome,
    RejectedIngestionsPurgedOutcome,
    RetentionBlockedOutcome,
    RetryScheduledOutcome,
    StaleCandidateOutcome,
    SystemBlockedOutcome,
)
from .ports import RetentionWorkerProtocol


def _never_stop() -> bool:
    return False


def run_forever(
    worker: RetentionWorkerProtocol,
    *,
    sleep: Callable[[float], None] = time.sleep,
    should_stop: Callable[[], bool] = _never_stop,
) -> None:
    """Run until cooperative shutdown, sleeping only when no work is due."""
    if not callable(getattr(worker, "run_once", None)):
        raise TypeError("worker must expose run_once()")
    if not callable(sleep):
        raise TypeError("sleep must be callable")
    if not callable(should_stop):
        raise TypeError("should_stop must be callable")

    while not should_stop():
        outcome = worker.run_once()
        if isinstance(outcome, IdleOutcome | DependencyWaitingOutcome):
            sleep(outcome.wait_seconds)
        elif isinstance(
            outcome,
            CheckRecoveredOutcome
            | GlpiCheckRescheduledOutcome
            | RetryScheduledOutcome
            | SystemBlockedOutcome
            | RetentionBlockedOutcome
            | AudioDeletedOutcome
            | RejectedIngestionsPurgedOutcome
            | StaleCandidateOutcome,
        ):
            continue
        else:
            assert_never(outcome)


__all__ = ["run_forever"]
