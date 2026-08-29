"""Bounded polling helpers that never render remote payloads on failure."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from uuid import UUID

import httpx

from .api import JobDetail, OperationalAlert, get_job, get_observability_alerts

DEFAULT_TIMEOUT_SECONDS = 15 * 60.0
DEFAULT_INTERVAL_SECONDS = 0.5


def poll_until[ResultT](
    probe: Callable[[], ResultT],
    predicate: Callable[[ResultT], bool],
    *,
    description: str,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
) -> ResultT:
    """Return the first accepted probe result or raise one sanitized timeout."""
    if not callable(probe):
        raise TypeError("probe must be callable")
    if not callable(predicate):
        raise TypeError("predicate must be callable")
    if not isinstance(description, str) or not description:
        raise ValueError("description must be a non-empty string")
    timeout_seconds = _positive_seconds(timeout_seconds, "timeout_seconds")
    interval_seconds = _positive_seconds(interval_seconds, "interval_seconds")

    deadline = time.monotonic() + timeout_seconds
    while True:
        result = probe()
        if predicate(result):
            return result
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(f"Timed out while waiting for {description}")
        time.sleep(min(interval_seconds, remaining))


def wait_for_job_state(
    client: httpx.Client,
    audio_uuid: UUID,
    expected_state: str,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> JobDetail:
    """Wait until one job reaches an exact persisted state."""
    if not isinstance(expected_state, str) or not expected_state:
        raise ValueError("expected_state must be a non-empty string")
    return poll_until(
        lambda: get_job(client, audio_uuid),
        lambda job: job.state == expected_state,
        description=f"job {audio_uuid} to reach {expected_state}",
        timeout_seconds=timeout_seconds,
    )


def wait_for_jobs_state(
    client: httpx.Client,
    audio_uuids: tuple[UUID, ...],
    expected_state: str,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[JobDetail, ...]:
    """Wait until every requested job reaches the same exact state."""
    if not audio_uuids:
        raise ValueError("audio_uuids must not be empty")
    if len(audio_uuids) != len(set(audio_uuids)):
        raise ValueError("audio_uuids must be unique")
    if not isinstance(expected_state, str) or not expected_state:
        raise ValueError("expected_state must be a non-empty string")

    return poll_until(
        lambda: tuple(get_job(client, audio_uuid) for audio_uuid in audio_uuids),
        lambda jobs: all(job.state == expected_state for job in jobs),
        description=f"{len(audio_uuids)} jobs to reach {expected_state}",
        timeout_seconds=timeout_seconds,
    )


def wait_for_alert(
    client: httpx.Client,
    kind: str,
    *,
    key: str | None = None,
    minimum_count: int = 1,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> OperationalAlert:
    """Wait for one deduplicated alert without exposing unrelated alerts."""
    if not isinstance(kind, str) or not kind:
        raise ValueError("kind must be a non-empty string")
    if key is not None and (not isinstance(key, str) or not key):
        raise ValueError("key must be a non-empty string or None")
    if (
        isinstance(minimum_count, bool)
        or not isinstance(minimum_count, int)
        or minimum_count <= 0
    ):
        raise ValueError("minimum_count must be a positive integer")

    def matching_alert() -> OperationalAlert | None:
        return next(
            (
                alert
                for alert in get_observability_alerts(client)
                if alert.kind == kind
                and (key is None or alert.key == key)
                and alert.count >= minimum_count
            ),
            None,
        )

    alert = poll_until(
        matching_alert,
        lambda candidate: candidate is not None,
        description=f"the {kind} alert",
        timeout_seconds=timeout_seconds,
    )
    if alert is None:  # pragma: no cover - narrowed by the polling predicate
        raise AssertionError("polling accepted an absent alert")
    return alert


def wait_for_alert_absent(
    client: httpx.Client,
    kind: str,
    *,
    key: str | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> None:
    """Wait until no current alert matches the requested condition."""
    if not isinstance(kind, str) or not kind:
        raise ValueError("kind must be a non-empty string")
    if key is not None and (not isinstance(key, str) or not key):
        raise ValueError("key must be a non-empty string or None")

    poll_until(
        lambda: get_observability_alerts(client),
        lambda alerts: (
            not any(
                alert.kind == kind and (key is None or alert.key == key)
                for alert in alerts
            )
        ),
        description=f"the {kind} alert to clear",
        timeout_seconds=timeout_seconds,
    )


def _positive_seconds(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{field_name} must be a number")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized <= 0:
        raise ValueError(f"{field_name} must be finite and greater than zero")
    return normalized


__all__ = [
    "DEFAULT_INTERVAL_SECONDS",
    "DEFAULT_TIMEOUT_SECONDS",
    "poll_until",
    "wait_for_alert",
    "wait_for_alert_absent",
    "wait_for_job_state",
    "wait_for_jobs_state",
]
