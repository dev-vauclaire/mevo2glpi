from __future__ import annotations

from collections.abc import Iterator

from glpi_ticketing_database import MailIngestionErrorScope
from glpi_ticketing_worker_publisher.publisher import (
    DiscoveryRetryOutcome,
    IdleOutcome,
    PublisherFailure,
    SystemBlockedOutcome,
    run_forever,
)


class SequenceWorker:
    def __init__(self, outcomes: Iterator[object]) -> None:
        self.outcomes = outcomes
        self.calls = 0

    def run_once(self) -> object:
        self.calls += 1
        return next(self.outcomes)


def _failure() -> PublisherFailure:
    return PublisherFailure(
        scope=MailIngestionErrorScope.SYSTEM,
        category="graph_unavailable",
        technical_code="transport_error",
    )


def test_idle_and_discovery_retry_use_injected_sleep() -> None:
    worker = SequenceWorker(
        iter(
            (
                IdleOutcome(wait_seconds=30),
                DiscoveryRetryOutcome(failure=_failure(), wait_seconds=5),
            )
        )
    )
    sleeps: list[float] = []

    run_forever(
        worker,  # type: ignore[arg-type]
        sleep=sleeps.append,
        should_stop=lambda: worker.calls == 2,
    )

    assert sleeps == [30.0, 5.0]


def test_terminal_system_block_never_calls_worker_again() -> None:
    worker = SequenceWorker(
        iter(
            (
                SystemBlockedOutcome(
                    failure=_failure(),
                    probe_after_seconds=60,
                ),
            )
        )
    )
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    run_forever(
        worker,  # type: ignore[arg-type]
        sleep=sleep,
        should_stop=lambda: len(sleeps) == 2,
    )

    assert worker.calls == 1
    assert sleeps == [60.0, 60.0]
