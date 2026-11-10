"""Tests du cookie de session minimal et de sa protection CSRF."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from mevo2glpi_web.auth.models import ACCESS_ROLE, AuthMode, Principal
from mevo2glpi_web.auth.sessions import (
    InvalidSessionError,
    ItsDangerousSessionSigner,
    LocalSessionAccountState,
    SessionCookieCodec,
    SessionManager,
)

ACCOUNT_ID = UUID("7eb80eb4-6e43-49fe-a725-7709ddb43a84")
NOW = datetime(2026, 11, 10, 10, tzinfo=UTC)


class JsonSigner:
    def dumps(self, payload: Mapping[str, object]) -> str:
        return json.dumps(dict(payload), separators=(",", ":"), sort_keys=True)

    def loads(self, signed_value: str) -> object:
        return json.loads(signed_value)


@dataclass
class FakeAccountReader:
    state: LocalSessionAccountState | None
    calls: list[UUID] = field(default_factory=list)

    def get_session_state(self, account_id: UUID) -> LocalSessionAccountState | None:
        self.calls.append(account_id)
        return self.state


def test_local_session_is_minimal_and_account_is_checked_on_every_request() -> None:
    reader = FakeAccountReader(LocalSessionAccountState(True, 7))
    manager = _local_manager(reader)

    issued = manager.issue(_local_principal())
    principal = manager.authenticate(issued.cookie_value)
    manager.authenticate(issued.cookie_value)

    serialized = json.loads(issued.cookie_value)
    assert set(serialized) == {
        "auth_mode",
        "subject",
        "roles",
        "session_version",
        "iat",
        "exp",
        "csrf",
    }
    assert principal == _local_principal()
    assert reader.calls == [ACCOUNT_ID, ACCOUNT_ID]
    assert "username" not in serialized
    assert "access_token" not in serialized


@pytest.mark.parametrize(
    "state",
    [
        None,
        LocalSessionAccountState(False, 7),
        LocalSessionAccountState(True, 8),
    ],
)
def test_disabled_deleted_or_version_changed_local_account_revokes_cookie(
    state: LocalSessionAccountState | None,
) -> None:
    manager = _local_manager(FakeAccountReader(state))
    issued = manager.issue(_local_principal())

    with pytest.raises(InvalidSessionError):
        manager.authenticate(issued.cookie_value)


def test_expired_cookie_and_cookie_from_other_auth_mode_are_rejected() -> None:
    reader = FakeAccountReader(LocalSessionAccountState(True, 7))
    manager = _local_manager(reader, lifetime=timedelta(seconds=1))
    issued = manager.issue(_local_principal())
    expired_manager = _local_manager(
        reader,
        now=NOW + timedelta(seconds=2),
        lifetime=timedelta(seconds=1),
    )

    with pytest.raises(InvalidSessionError):
        expired_manager.authenticate(issued.cookie_value)

    oidc_manager = SessionManager(
        SessionCookieCodec(JsonSigner()),
        auth_mode=AuthMode.OIDC,
        clock=lambda: NOW,
    )
    with pytest.raises(InvalidSessionError):
        oidc_manager.issue(_local_principal())


def test_csrf_token_is_bound_to_the_authenticated_cookie() -> None:
    manager = _local_manager(FakeAccountReader(LocalSessionAccountState(True, 7)))
    issued = manager.issue(_local_principal())

    assert manager.validate_csrf(issued.cookie_value, issued.csrf_token) == (
        _local_principal()
    )
    with pytest.raises(InvalidSessionError, match="CSRF"):
        manager.validate_csrf(issued.cookie_value, "wrong-token")


def test_real_signer_rejects_cookie_tampering() -> None:
    signer = ItsDangerousSessionSigner("s" * 32)
    manager = SessionManager(
        SessionCookieCodec(signer),
        auth_mode=AuthMode.OIDC,
        clock=lambda: NOW,
        random_bytes=lambda size: b"x" * size,
    )
    principal = Principal(
        auth_mode=AuthMode.OIDC,
        subject="pseudonymous-subject",
        roles=frozenset({ACCESS_ROLE}),
    )
    issued = manager.issue(principal)

    replacement = "A" if issued.cookie_value[-1] != "A" else "B"
    tampered = f"{issued.cookie_value[:-1]}{replacement}"
    with pytest.raises(InvalidSessionError):
        manager.authenticate(tampered)


def _local_principal() -> Principal:
    return Principal(
        auth_mode=AuthMode.LOCAL,
        subject=str(ACCOUNT_ID),
        roles=frozenset({ACCESS_ROLE}),
        session_version=7,
    )


def _local_manager(
    reader: FakeAccountReader,
    *,
    now: datetime = NOW,
    lifetime: timedelta = timedelta(minutes=30),
) -> SessionManager:
    return SessionManager(
        SessionCookieCodec(JsonSigner()),
        auth_mode=AuthMode.LOCAL,
        local_accounts=reader,
        clock=lambda: now,
        lifetime=lifetime,
        random_bytes=lambda size: b"x" * size,
    )
