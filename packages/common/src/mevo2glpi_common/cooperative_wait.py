"""Interruptible waiting split by a periodic application checkpoint."""

from __future__ import annotations

import math
from collections.abc import Callable


def wait_with_periodic_checkpoint(
    duration_seconds: float,
    *,
    checkpoint_interval_seconds: float,
    wait_once: Callable[[float], object],
    should_stop: Callable[[], bool],
    checkpoint: Callable[[], object],
) -> None:
    """Wait for a duration while keeping one cooperative runtime observable.

    The function does not create a thread. ``wait_once`` is expected to wait for
    the requested slice or return early only when ``should_stop`` becomes true,
    as :meth:`threading.Event.wait` does. Checkpoint failures are deliberately
    propagated so a worker fails closed when it can no longer prove ownership.
    """
    duration = _non_negative_finite_seconds(duration_seconds, "duration_seconds")
    interval = _positive_finite_seconds(
        checkpoint_interval_seconds,
        "checkpoint_interval_seconds",
    )
    for callback, field_name in (
        (wait_once, "wait_once"),
        (should_stop, "should_stop"),
        (checkpoint, "checkpoint"),
    ):
        if not callable(callback):
            raise TypeError(f"{field_name} must be callable")

    # Derive every slice from its absolute boundary instead of repeatedly
    # subtracting floats. ``nextafter`` prevents an exact mathematical multiple
    # such as 1.0 / 0.1 from producing one final near-zero slice.
    duration_boundary = math.nextafter(duration, 0.0)
    slice_index = 0
    while duration > 0 and (
        slice_index == 0 or slice_index * interval < duration_boundary
    ):
        if _stop_requested(should_stop):
            return
        elapsed_seconds = slice_index * interval
        slice_seconds = min(duration - elapsed_seconds, interval)
        wait_once(slice_seconds)
        if _stop_requested(should_stop):
            return
        checkpoint()
        slice_index += 1


def _stop_requested(should_stop: Callable[[], bool]) -> bool:
    requested = should_stop()
    if not isinstance(requested, bool):
        raise TypeError("should_stop must return a boolean")
    return requested


def _non_negative_finite_seconds(value: object, field_name: str) -> float:
    normalized = _finite_number(value, field_name)
    if normalized < 0:
        raise ValueError(f"{field_name} must be non-negative")
    return normalized


def _positive_finite_seconds(value: object, field_name: str) -> float:
    normalized = _finite_number(value, field_name)
    if normalized <= 0:
        raise ValueError(f"{field_name} must be greater than zero")
    return normalized


def _finite_number(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{field_name} must be a number")
    normalized = float(value)
    if not math.isfinite(normalized):
        raise ValueError(f"{field_name} must be finite")
    return normalized


__all__ = ["wait_with_periodic_checkpoint"]
