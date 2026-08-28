"""Scheduling loop for the persistent automation worker."""

import time
from collections.abc import Callable
from typing import assert_never

from .outcomes import (
    BlockedOutcome,
    CompletedOutcome,
    FailedOutcome,
    IdleOutcome,
    InterruptedOutcome,
    RetryOutcome,
    SystemBlockedOutcome,
)
from .ports import AutomationWorkerProtocol


def _never_stop() -> bool:
    return False


def run_forever(
    worker: AutomationWorkerProtocol,
    *,
    sleep: Callable[[float], None] = time.sleep,
    should_stop: Callable[[], bool] = _never_stop,
) -> None:
    """Run until shutdown or an unexpected exception.

    Retry deadlines live in PostgreSQL, so a scheduled retry never sleeps here:
    another eligible job may be consumed immediately. A block scoped to one job
    behaves like a terminal boundary and does not stop the singleton worker. A
    recoverable system block sleeps before probing the same processing job.
    A latched block keeps the process alive without repeating the worker.
    """
    while not should_stop():
        outcome = worker.run_once()
        if isinstance(outcome, IdleOutcome):
            sleep(outcome.wait_seconds)
        elif isinstance(outcome, InterruptedOutcome):
            return
        elif isinstance(outcome, RetryOutcome | CompletedOutcome | FailedOutcome):
            continue
        elif isinstance(outcome, BlockedOutcome):
            continue
        elif isinstance(outcome, SystemBlockedOutcome):
            if outcome.automatic_probe:
                sleep(outcome.probe_after_seconds)
            else:
                # Keep the process alive and its blocked heartbeat observable,
                # but never repeat an external side effect after an ambiguous
                # accepted request. A signal remains responsive through the
                # injected Event.wait used by the application.
                while not should_stop():
                    sleep(outcome.probe_after_seconds)
        else:
            assert_never(outcome)
