"""Cas d'usage d'authentification par compte applicatif local."""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from .models import ACCESS_ROLE, AuthMode, Principal
from .passwords import InvalidUsernameError, PasswordHasher, normalize_username

GENERIC_AUTHENTICATION_ERROR = "invalid username or password"


class LocalAuthenticationFailureReason(StrEnum):
    """Internal, non-sensitive classification hidden from the client."""

    INVALID_CREDENTIALS = "invalid_credentials"
    THROTTLED = "throttled"


class LocalAuthenticationError(ValueError):
    """Generic public failure carrying only sanitized audit metadata."""

    def __init__(
        self,
        *,
        reason: LocalAuthenticationFailureReason,
    ) -> None:
        super().__init__(GENERIC_AUTHENTICATION_ERROR)
        self.reason = reason


class LocalAuthenticationUnavailableError(RuntimeError):
    """The local account store cannot currently validate credentials."""


@dataclass(frozen=True, slots=True)
class LocalAccount:
    account_id: UUID
    username: str
    password_hash: str
    is_active: bool
    session_version: int
    failed_login_count: int = 0
    locked_until: datetime | None = None

    def __post_init__(self) -> None:
        if self.username != normalize_username(self.username):
            raise ValueError("local account username is not canonical")
        if not self.password_hash:
            raise ValueError("local account password hash is required")
        if (
            isinstance(self.session_version, bool)
            or isinstance(self.failed_login_count, bool)
            or self.session_version < 0
            or self.failed_login_count < 0
        ):
            raise ValueError("local account counters cannot be negative")
        if self.locked_until is not None and (
            self.locked_until.tzinfo is None or self.locked_until.utcoffset() is None
        ):
            raise ValueError("lock timestamp must be timezone-aware")


class LocalAccountRepository(Protocol):
    def get_by_username(self, username: str) -> LocalAccount | None: ...

    def record_failed_authentication(
        self,
        account_id: UUID,
        *,
        failed_login_count: int,
        locked_until: datetime | None,
        occurred_at: datetime,
    ) -> None: ...

    def record_successful_authentication(
        self,
        account_id: UUID,
        *,
        occurred_at: datetime,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class ProgressiveThrottlePolicy:
    """Verrouillage exponentiel apres plusieurs echecs consecutifs."""

    failures_before_lock: int = 5
    initial_delay: timedelta = timedelta(seconds=30)
    maximum_delay: timedelta = timedelta(minutes=15)

    def __post_init__(self) -> None:
        if self.failures_before_lock < 1:
            raise ValueError("failures_before_lock must be positive")
        if self.initial_delay <= timedelta(0):
            raise ValueError("initial_delay must be positive")
        if self.maximum_delay < self.initial_delay:
            raise ValueError("maximum_delay must not be shorter than initial_delay")

    def lock_until(self, failure_count: int, *, now: datetime) -> datetime | None:
        if failure_count < self.failures_before_lock:
            return None
        exponent = failure_count - self.failures_before_lock
        delay_seconds = min(
            self.initial_delay.total_seconds() * (2**exponent),
            self.maximum_delay.total_seconds(),
        )
        return now + timedelta(seconds=delay_seconds)


class LocalAuthenticator:
    """Authentifie un compte local sans reveler son etat au client."""

    def __init__(
        self,
        repository: LocalAccountRepository,
        password_hasher: PasswordHasher,
        *,
        throttle_policy: ProgressiveThrottlePolicy | None = None,
        clock: Callable[[], datetime] | None = None,
        dummy_hash: str | None = None,
        access_role: str = ACCESS_ROLE,
    ) -> None:
        self._repository = repository
        self._password_hasher = password_hasher
        self._throttle_policy = throttle_policy or ProgressiveThrottlePolicy()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._dummy_hash = dummy_hash or password_hasher.hash(secrets.token_urlsafe(32))
        if not access_role or len(access_role) > 128:
            raise ValueError("access_role is invalid")
        self._access_role = access_role

    def authenticate(self, username: str, password: str) -> Principal:
        now = self._utc_now()
        if not isinstance(password, str) or len(password) > 128:
            self._password_hasher.verify("invalid-login-value", self._dummy_hash)
            raise LocalAuthenticationError(
                reason=LocalAuthenticationFailureReason.INVALID_CREDENTIALS
            )

        try:
            normalized_username = normalize_username(username)
        except InvalidUsernameError as error:
            self._password_hasher.verify(password, self._dummy_hash)
            raise LocalAuthenticationError(
                reason=LocalAuthenticationFailureReason.INVALID_CREDENTIALS
            ) from error

        try:
            account = self._repository.get_by_username(normalized_username)
        except Exception:
            raise LocalAuthenticationUnavailableError(
                "The local account store is unavailable."
            ) from None
        if account is None:
            self._password_hasher.verify(password, self._dummy_hash)
            raise LocalAuthenticationError(
                reason=LocalAuthenticationFailureReason.INVALID_CREDENTIALS
            )

        password_matches = self._password_hasher.verify(
            password,
            account.password_hash,
        )
        is_locked = account.locked_until is not None and account.locked_until > now
        if not account.is_active:
            raise LocalAuthenticationError(
                reason=LocalAuthenticationFailureReason.INVALID_CREDENTIALS
            )
        if is_locked:
            raise LocalAuthenticationError(
                reason=LocalAuthenticationFailureReason.THROTTLED
            )
        if not password_matches:
            failures = account.failed_login_count + 1
            locked_until = self._throttle_policy.lock_until(failures, now=now)
            try:
                self._repository.record_failed_authentication(
                    account.account_id,
                    failed_login_count=failures,
                    locked_until=locked_until,
                    occurred_at=now,
                )
            except Exception:
                raise LocalAuthenticationUnavailableError(
                    "The local account store is unavailable."
                ) from None
            raise LocalAuthenticationError(
                reason=(
                    LocalAuthenticationFailureReason.THROTTLED
                    if locked_until is not None
                    else LocalAuthenticationFailureReason.INVALID_CREDENTIALS
                )
            )

        try:
            self._repository.record_successful_authentication(
                account.account_id,
                occurred_at=now,
            )
        except Exception:
            raise LocalAuthenticationUnavailableError(
                "The local account store is unavailable."
            ) from None
        return Principal(
            auth_mode=AuthMode.LOCAL,
            subject=str(account.account_id),
            roles=frozenset({self._access_role}),
            session_version=account.session_version,
        )

    def _utc_now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("authentication clock must return an aware timestamp")
        return now.astimezone(UTC)
