"""Synthetic authenticated web context shared by HTTP tests."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi.testclient import TestClient

from mevo2glpi_web.auth.audit import AuthAuditEvent
from mevo2glpi_web.auth.http import CSRF_HEADER_NAME, SessionCookiePolicy
from mevo2glpi_web.auth.models import ACCESS_ROLE, AuthMode, Principal
from mevo2glpi_web.auth.sessions import (
    ItsDangerousSessionSigner,
    SessionCookieCodec,
    SessionManager,
)
from mevo2glpi_web.controllers import AuthenticationServices

_SESSION_SECRET = "synthetic-http-test-session-secret-at-least-32-characters"


class SyntheticAuditRecorder:
    def record(self, _event_type: AuthAuditEvent, **_kwargs: object) -> None:
        return None


class UnusedOidcAuthenticator:
    async def begin(self, _request: object) -> object:
        raise AssertionError("OIDC login is not exercised by this fixture")

    async def complete(self, _request: object) -> Principal:
        raise AssertionError("OIDC callback is not exercised by this fixture")


@dataclass(frozen=True, slots=True)
class SyntheticWebAuthentication:
    services: AuthenticationServices
    cookie_policy: SessionCookiePolicy
    cookie_value: str
    csrf_token: str
    oidc_transaction_secret: str = _SESSION_SECRET

    def authenticate(self, client: TestClient) -> None:
        client.cookies.set(self.cookie_policy.name, self.cookie_value)
        client.headers[CSRF_HEADER_NAME] = self.csrf_token


def build_synthetic_web_authentication() -> SyntheticWebAuthentication:
    manager = SessionManager(
        SessionCookieCodec(ItsDangerousSessionSigner(_SESSION_SECRET)),
        auth_mode=AuthMode.OIDC,
    )
    issued = manager.issue(
        Principal(
            auth_mode=AuthMode.OIDC,
            subject="oidc:" + "a" * 64,
            roles=frozenset({ACCESS_ROLE}),
        )
    )
    return SyntheticWebAuthentication(
        services=AuthenticationServices(
            mode=AuthMode.OIDC,
            sessions=manager,
            audit=SyntheticAuditRecorder(),
            oidc=UnusedOidcAuthenticator(),  # type: ignore[arg-type]
        ),
        cookie_policy=SessionCookiePolicy(secure=False, max_age_seconds=1800),
        cookie_value=issued.cookie_value,
        csrf_token=issued.csrf_token,
    )


__all__ = ["SyntheticWebAuthentication", "build_synthetic_web_authentication"]
