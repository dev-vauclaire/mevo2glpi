"""Validation helpers shared by model-specific repositories."""

from __future__ import annotations

import re
from datetime import datetime
from uuid import UUID

_SAFE_IDENTIFIER = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


def validate_uuid(value: UUID, field_name: str) -> UUID:
    """Return a UUID after rejecting permissive or implicit conversions."""
    if not isinstance(value, UUID):
        raise TypeError(f"{field_name} must be a UUID")
    return value


def validate_positive_integer(value: int, field_name: str) -> int:
    """Return a strictly positive integer, excluding booleans."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be an integer")
    if value <= 0:
        raise ValueError(f"{field_name} must be positive")
    return value


def validate_page_limit(value: int, field_name: str = "limit") -> int:
    """Return a repository page size constrained to a safe bounded range."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be an integer")
    if not 1 <= value <= 100:
        raise ValueError(f"{field_name} must be between 1 and 100")
    return value


def validate_aware_datetime(value: datetime, field_name: str) -> datetime:
    """Return a timezone-aware datetime."""
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    if value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


def validate_future_datetime(
    value: datetime,
    *,
    now: datetime,
    field_name: str,
) -> datetime:
    """Return an aware datetime strictly later than ``now``."""
    value = validate_aware_datetime(value, field_name)
    if value <= now:
        raise ValueError(f"{field_name} must be later than now")
    return value


def validate_safe_identifier(value: str, field_name: str) -> str:
    """Validate a sanitized identifier suitable for persisted diagnostics."""
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if _SAFE_IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a safe identifier")
    return value


def validate_optional_error(
    error_category: str | None,
    error_code: str | None,
) -> tuple[str | None, str | None]:
    """Validate a pair of optional sanitized error identifiers."""
    if (error_category is None) != (error_code is None):
        raise ValueError("error_category and error_code must be provided together")
    if error_category is not None and error_code is not None:
        return (
            validate_safe_identifier(error_category, "error_category"),
            validate_safe_identifier(error_code, "error_code"),
        )
    return None, None


__all__ = [
    "validate_aware_datetime",
    "validate_future_datetime",
    "validate_optional_error",
    "validate_page_limit",
    "validate_positive_integer",
    "validate_safe_identifier",
    "validate_uuid",
]
