"""Tests unitaires de l'authentification locale."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from mevo2glpi_web.auth.local import (
    GENERIC_AUTHENTICATION_ERROR,
    LocalAccount,
    LocalAuthenticationError,
    LocalAuthenticationFailureReason,
    LocalAuthenticator,
    ProgressiveThrottlePolicy,
)
from mevo2glpi_web.auth.models import ACCESS_ROLE, AuthMode
from mevo2glpi_web.auth.passwords import (
    Argon2PasswordHasher,
    InvalidPasswordError,
    InvalidUsernameError,
    PasswordPolicy,
    normalize_username,
)

ACCOUNT_ID = UUID("7eb80eb4-6e43-49fe-a725-7709ddb43a84")
NOW = datetime(2026, 11, 10, 10, tzinfo=UTC)
PASSWORD = "une passphrase locale robuste"


@dataclass
class FakeHasher:
    verify_calls: list[tuple[str, str]] = field(default_factory=list)

    def hash(self, password: str) -> str:
        return f"hash:{password}"

    def verify(self, password: str, encoded_hash: str) -> bool:
        self.verify_calls.append((password, encoded_hash))
        return encoded_hash == self.hash(password)


@dataclass
class FakeRepository:
    account: LocalAccount | None
    failures: list[dict[str, object]] = field(default_factory=list)
    successes: list[tuple[UUID, datetime]] = field(default_factory=list)
    lookups: list[str] = field(default_factory=list)

    def get_by_username(self, username: str) -> LocalAccount | None:
        self.lookups.append(username)
        return self.account

    def record_failed_authentication(
        self,
        account_id: UUID,
        *,
        failed_login_count: int,
        locked_until: datetime | None,
        occurred_at: datetime,
    ) -> None:
        self.failures.append(
            {
                "account_id": account_id,
                "failed_login_count": failed_login_count,
                "locked_until": locked_until,
                "occurred_at": occurred_at,
            }
        )

    def record_successful_authentication(
        self,
        account_id: UUID,
        *,
        occurred_at: datetime,
    ) -> None:
        self.successes.append((account_id, occurred_at))


def test_username_normalization_is_restrictive_and_canonical() -> None:
    assert normalize_username("  Operator.Test-1 ") == "operator.test-1"
    assert normalize_username("ＯＰＥＲＡＴＯＲ") == "operator"

    for invalid in ("ab", "operateur hopital", "opérateur", "operator/../../"):
        with pytest.raises(InvalidUsernameError):
            normalize_username(invalid)


def test_password_policy_accepts_passphrases_and_blocks_weak_values() -> None:
    policy = PasswordPolicy()

    policy.validate(PASSWORD, username="operator")

    for invalid in (
        "court",
        "x" * 129,
        "development-only",
    ):
        with pytest.raises(InvalidPasswordError):
            policy.validate(invalid, username="operator")


def test_pwdlib_adapter_hashes_with_argon2id() -> None:
    hasher = Argon2PasswordHasher()

    encoded = hasher.hash(PASSWORD)

    assert encoded.startswith("$argon2id$")
    assert hasher.verify(PASSWORD, encoded) is True
    assert hasher.verify("une autre passphrase robuste", encoded) is False


def test_success_returns_minimal_principal_and_resets_failures() -> None:
    account = _account(failed_login_count=3)
    repository = FakeRepository(account)
    hasher = FakeHasher()
    authenticator = _authenticator(repository, hasher)

    principal = authenticator.authenticate(" OPERATOR ", PASSWORD)

    assert principal.auth_mode is AuthMode.LOCAL
    assert principal.subject == str(ACCOUNT_ID)
    assert principal.roles == frozenset({ACCESS_ROLE})
    assert principal.session_version == 4
    assert repository.lookups == ["operator"]
    assert repository.successes == [(ACCOUNT_ID, NOW)]


@pytest.mark.parametrize(
    ("username", "account_active"),
    [
        ("unknown", None),
        ("operator", False),
        ("not valid", None),
    ],
)
def test_unknown_disabled_and_invalid_usernames_share_generic_failure(
    username: str,
    account_active: bool | None,
) -> None:
    account = _account(is_active=account_active) if account_active is not None else None
    repository = FakeRepository(account)
    hasher = FakeHasher()
    authenticator = _authenticator(repository, hasher)

    with pytest.raises(LocalAuthenticationError) as captured:
        authenticator.authenticate(username, "wrong-password")

    assert str(captured.value) == GENERIC_AUTHENTICATION_ERROR
    assert len(hasher.verify_calls) == 1
    assert repository.failures == []


def test_fifth_failure_schedules_initial_lock_and_later_delays_double() -> None:
    hasher = FakeHasher()
    fifth_repository = FakeRepository(_account(failed_login_count=4))

    with pytest.raises(LocalAuthenticationError) as captured:
        _authenticator(fifth_repository, hasher).authenticate("operator", "wrong")

    assert captured.value.reason is LocalAuthenticationFailureReason.THROTTLED
    assert fifth_repository.failures[0]["failed_login_count"] == 5
    assert fifth_repository.failures[0]["locked_until"] == NOW + timedelta(seconds=30)
    policy = ProgressiveThrottlePolicy()
    assert policy.lock_until(6, now=NOW) == NOW + timedelta(seconds=60)
    assert policy.lock_until(20, now=NOW) == NOW + timedelta(minutes=15)


def test_locked_account_is_checked_but_lock_is_not_extended() -> None:
    repository = FakeRepository(_account(locked_until=NOW + timedelta(minutes=1)))
    hasher = FakeHasher()

    with pytest.raises(LocalAuthenticationError) as captured:
        _authenticator(repository, hasher).authenticate("operator", PASSWORD)

    assert captured.value.reason is LocalAuthenticationFailureReason.THROTTLED
    assert hasher.verify_calls == [(PASSWORD, f"hash:{PASSWORD}")]
    assert repository.failures == []
    assert repository.successes == []


def _account(**changes: object) -> LocalAccount:
    values: dict[str, object] = {
        "account_id": ACCOUNT_ID,
        "username": "operator",
        "password_hash": f"hash:{PASSWORD}",
        "is_active": True,
        "session_version": 4,
    }
    values.update(changes)
    return LocalAccount(**values)  # type: ignore[arg-type]


def _authenticator(
    repository: FakeRepository,
    hasher: FakeHasher,
) -> LocalAuthenticator:
    return LocalAuthenticator(
        repository,
        hasher,
        clock=lambda: NOW,
        dummy_hash="hash:dummy-secret-value",
    )
