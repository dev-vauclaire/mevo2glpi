"""Pure, deterministic retry backoff shared by runtime applications."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True, slots=True, kw_only=True)
class ExponentialBackoffPolicy:
    """Compute a capped exponential delay without sleeping.

    ``consecutive_retry_count`` is the number of consecutive failures already
    persisted before the failure currently being scheduled. A value of zero
    therefore returns ``initial_seconds``.
    """

    initial_seconds: float
    maximum_seconds: float

    def __post_init__(self) -> None:
        initial = _positive_finite_seconds(
            self.initial_seconds,
            field_name="initial_seconds",
        )
        maximum = _positive_finite_seconds(
            self.maximum_seconds,
            field_name="maximum_seconds",
        )
        if initial > maximum:
            raise ValueError("initial_seconds must not exceed maximum_seconds")

        object.__setattr__(self, "initial_seconds", initial)
        object.__setattr__(self, "maximum_seconds", maximum)

    def delay_seconds(self, *, consecutive_retry_count: int) -> float:
        """Return the deterministic delay for the next retry."""

        count = _non_negative_integer(
            consecutive_retry_count,
            field_name="consecutive_retry_count",
        )
        delay = self.initial_seconds

        # Iterating only until the cap avoids both ``2**count`` overflow and a
        # loop proportional to a corrupt, unexpectedly large persisted count.
        while count > 0 and delay < self.maximum_seconds:
            delay = min(delay * 2, self.maximum_seconds)
            count -= 1

        return delay

    def next_attempt_at(
        self,
        *,
        now: datetime,
        consecutive_retry_count: int,
    ) -> datetime:
        """Return the timezone-aware instant at which the retry becomes due."""

        _require_aware_datetime(now, field_name="now")
        delay = self.delay_seconds(
            consecutive_retry_count=consecutive_retry_count,
        )
        try:
            return now + timedelta(seconds=delay)
        except OverflowError:
            raise ValueError(
                "The retry instant cannot be represented by datetime"
            ) from None


def _positive_finite_seconds(value: object, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{field_name} must be a number")
    try:
        normalized = float(value)
    except OverflowError:
        raise ValueError(f"{field_name} must be finite and positive") from None
    if not math.isfinite(normalized) or normalized <= 0:
        raise ValueError(f"{field_name} must be finite and positive")
    return normalized


def _non_negative_integer(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be an integer")
    if value < 0:
        raise ValueError(f"{field_name} must be non-negative")
    return value


def _require_aware_datetime(value: object, *, field_name: str) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    if value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


__all__ = ["ExponentialBackoffPolicy"]
