"""Small HTTP security helpers shared by authentication controllers."""

from __future__ import annotations

import base64
import hmac
import secrets
from dataclasses import dataclass
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import Response

LOGIN_CSRF_COOKIE_NAME = "mevo2glpi_login_csrf"
SESSION_COOKIE_NAME = "mevo2glpi_session"
CSRF_HEADER_NAME = "X-CSRF-Token"


class CsrfValidationError(ValueError):
    """Report a missing or mismatched authenticated-request token."""


class AuthenticatedCsrfProtection:
    """Validate unsafe API calls against the token bound to the session."""

    def validate(self, request: Request) -> None:
        expected = getattr(request.state, "auth_csrf_token", None)
        submitted = request.headers.get(CSRF_HEADER_NAME)
        if (
            not isinstance(expected, str)
            or not isinstance(submitted, str)
            or not hmac.compare_digest(expected, submitted)
        ):
            raise CsrfValidationError("invalid CSRF token")


@dataclass(frozen=True, slots=True)
class SessionCookiePolicy:
    """Security attributes shared by session creation and invalidation."""

    secure: bool
    max_age_seconds: int
    name: str = SESSION_COOKIE_NAME

    def __post_init__(self) -> None:
        if not isinstance(self.secure, bool):
            raise TypeError("secure must be a boolean")
        if (
            isinstance(self.max_age_seconds, bool)
            or not isinstance(self.max_age_seconds, int)
            or self.max_age_seconds <= 0
        ):
            raise ValueError("max_age_seconds must be a positive integer")
        if not self.name or not self.name.isascii():
            raise ValueError("session cookie name is invalid")

    def set(self, response: Response, value: str) -> None:
        response.set_cookie(
            key=self.name,
            value=value,
            max_age=self.max_age_seconds,
            secure=self.secure,
            httponly=True,
            samesite="lax",
            path="/",
        )

    def delete(self, response: Response) -> None:
        response.delete_cookie(
            key=self.name,
            secure=self.secure,
            httponly=True,
            samesite="lax",
            path="/",
        )


@dataclass(frozen=True, slots=True)
class LoginCsrfProtection:
    """Short-lived double-submit token protecting the unauthenticated form."""

    secure: bool
    max_age_seconds: int = 600

    def issue(self) -> str:
        return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()

    def validate(self, cookie_token: str | None, submitted_token: str) -> bool:
        return (
            isinstance(cookie_token, str)
            and isinstance(submitted_token, str)
            and 32 <= len(submitted_token) <= 128
            and len(cookie_token) <= 128
            and hmac.compare_digest(cookie_token, submitted_token)
        )

    def set_cookie(self, response: Response, token: str) -> None:
        response.set_cookie(
            key=LOGIN_CSRF_COOKIE_NAME,
            value=token,
            max_age=self.max_age_seconds,
            secure=self.secure,
            httponly=True,
            samesite="strict",
            path="/auth/login",
        )

    def delete_cookie(self, response: Response) -> None:
        response.delete_cookie(
            key=LOGIN_CSRF_COOKIE_NAME,
            secure=self.secure,
            httponly=True,
            samesite="strict",
            path="/auth/login",
        )


def safe_next_path(value: str | None) -> str:
    """Allow only a bounded same-origin absolute path after authentication."""
    if not isinstance(value, str) or not value or len(value) > 2048:
        return "/"
    parsed = urlsplit(value)
    if (
        parsed.scheme
        or parsed.netloc
        or not parsed.path.startswith("/")
        or parsed.path.startswith("//")
        or "\\" in value
    ):
        return "/"
    return value


__all__ = [
    "LOGIN_CSRF_COOKIE_NAME",
    "SESSION_COOKIE_NAME",
    "CSRF_HEADER_NAME",
    "AuthenticatedCsrfProtection",
    "CsrfValidationError",
    "LoginCsrfProtection",
    "SessionCookiePolicy",
    "safe_next_path",
]
