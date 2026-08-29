"""Shared immutable values returned by model-specific alert queries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class AlertAggregate:
    """Count rows matching one condition and expose its oldest occurrence."""

    count: int
    condition_since: datetime | None


__all__ = ["AlertAggregate"]
