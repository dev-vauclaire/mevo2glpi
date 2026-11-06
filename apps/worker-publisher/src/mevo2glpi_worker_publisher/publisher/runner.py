"""Scheduling loop for the singleton publisher worker."""

import time
from collections.abc import Callable
from typing import assert_never

from .outcomes import (
    BlockedOutcome,
    CompletedOutcome,
    DiscoveryRetryOutcome,
    IdleOutcome,
    RejectedOutcome,
    RetryScheduledOutcome,
    SystemBlockedOutcome,
)
from .ports import PublisherWorkerProtocol


def _never_stop() -> bool:
    return False


def run_forever(
    worker: PublisherWorkerProtocol,
    *,
    sleep: Callable[[float], None] = time.sleep,
    should_stop: Callable[[], bool] = _never_stop,
) -> None:
    """Run until shutdown, keeping terminal systemic failures latched."""
    if not callable(getattr(worker, "run_once", None)):
        raise TypeError("worker must expose run_once()")
    if not callable(sleep):
        raise TypeError("sleep must be callable")
    if not callable(should_stop):
        raise TypeError("should_stop must be callable")

    while not should_stop():
        outcome = worker.run_once()
        if isinstance(outcome, IdleOutcome | DiscoveryRetryOutcome):
            sleep(outcome.wait_seconds)
        elif isinstance(
            outcome,
            RetryScheduledOutcome | CompletedOutcome | RejectedOutcome | BlockedOutcome,
        ):
            continue
        elif isinstance(outcome, SystemBlockedOutcome):
            if outcome.automatic_probe:
                sleep(outcome.probe_after_seconds)
                continue
            while not should_stop():
                sleep(outcome.probe_after_seconds)
        else:
            assert_never(outcome)


__all__ = ["run_forever"]
