"""Administration explicite des comptes locaux, sans interface web."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from .local import LocalAccount
from .passwords import PasswordHasher, PasswordPolicy, normalize_username


class LocalAccountNotFoundError(LookupError):
    pass


class LocalAccountAlreadyExistsError(ValueError):
    pass


class LocalAccountAdminRepository(Protocol):
    def create(
        self,
        *,
        username: str,
        password_hash: str,
        occurred_at: datetime,
    ) -> LocalAccount: ...

    def list_all(self) -> Sequence[LocalAccount]: ...

    def get_by_username(self, username: str) -> LocalAccount | None: ...

    def set_active(
        self,
        account_id: UUID,
        *,
        is_active: bool,
        increment_session_version: bool,
        occurred_at: datetime,
    ) -> LocalAccount: ...

    def replace_password(
        self,
        account_id: UUID,
        *,
        password_hash: str,
        increment_session_version: bool,
        occurred_at: datetime,
    ) -> LocalAccount: ...


class LocalAccountAdminService:
    """Use cases reserves a une commande d'administration locale."""

    def __init__(
        self,
        repository: LocalAccountAdminRepository,
        password_hasher: PasswordHasher,
        *,
        password_policy: PasswordPolicy | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._password_hasher = password_hasher
        self._password_policy = password_policy or PasswordPolicy()
        self._clock = clock or (lambda: datetime.now(UTC))

    def create(self, username: str, password: str) -> LocalAccount:
        normalized = normalize_username(username)
        self._password_policy.validate(password, username=normalized)
        if self._repository.get_by_username(normalized) is not None:
            raise LocalAccountAlreadyExistsError("local account already exists")
        return self._repository.create(
            username=normalized,
            password_hash=self._password_hasher.hash(password),
            occurred_at=self._utc_now(),
        )

    def list_accounts(self) -> Sequence[LocalAccount]:
        return self._repository.list_all()

    def enable(self, username: str) -> LocalAccount:
        return self._set_active(username, is_active=True)

    def disable(self, username: str) -> LocalAccount:
        return self._set_active(username, is_active=False)

    def reset_password(self, username: str, password: str) -> LocalAccount:
        account = self._require_account(username)
        self._password_policy.validate(password, username=account.username)
        return self._repository.replace_password(
            account.account_id,
            password_hash=self._password_hasher.hash(password),
            increment_session_version=True,
            occurred_at=self._utc_now(),
        )

    def _set_active(self, username: str, *, is_active: bool) -> LocalAccount:
        account = self._require_account(username)
        return self._repository.set_active(
            account.account_id,
            is_active=is_active,
            increment_session_version=True,
            occurred_at=self._utc_now(),
        )

    def _require_account(self, username: str) -> LocalAccount:
        account = self._repository.get_by_username(normalize_username(username))
        if account is None:
            raise LocalAccountNotFoundError("local account not found")
        return account

    def _utc_now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("admin clock must return an aware timestamp")
        return now.astimezone(UTC)
