"""Application boundary for sanitized authentication audit events."""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import Protocol

from .models import AuthMode

LOGGER = logging.getLogger("mevo2glpi_web.authentication")

_ALLOWED_CATEGORIES = frozenset(
    {"authentication", "authorization", "availability", "throttling"}
)
_ALLOWED_TECHNICAL_CODES = frozenset(
    {
        "credentials_invalid",
        "local_account_store_unavailable",
        "login_temporarily_locked",
        "oidc_provider_unavailable",
        "oidc_response_invalid",
        "required_role_missing",
    }
)


class AuthAuditEvent(StrEnum):
    LOGIN_SUCCEEDED = "login_succeeded"
    LOGIN_FAILED = "login_failed"
    LOGIN_THROTTLED = "login_throttled"
    LOGOUT = "logout"


class AuthAuditRecorder(Protocol):
    """Emit one sanitized event without receiving identity or credential data."""

    def record(
        self,
        event_type: AuthAuditEvent,
        *,
        auth_mode: AuthMode,
        succeeded: bool,
        category: str | None = None,
        technical_code: str | None = None,
    ) -> None: ...


class LoggingAuthAuditRecorder:
    """Write bounded authentication diagnostics without an identity field."""

    def record(
        self,
        event_type: AuthAuditEvent,
        *,
        auth_mode: AuthMode,
        succeeded: bool,
        category: str | None = None,
        technical_code: str | None = None,
    ) -> None:
        level = logging.INFO if succeeded else logging.WARNING
        safe_category = category if category in _ALLOWED_CATEGORIES else "none"
        safe_technical_code = (
            technical_code if technical_code in _ALLOWED_TECHNICAL_CODES else "none"
        )
        LOGGER.log(
            level,
            "web authentication event event_type=%s auth_mode=%s outcome=%s "
            "category=%s technical_code=%s",
            event_type.value,
            auth_mode.value,
            "success" if succeeded else "failure",
            safe_category,
            safe_technical_code,
        )


__all__ = ["AuthAuditEvent", "AuthAuditRecorder", "LoggingAuthAuditRecorder"]
