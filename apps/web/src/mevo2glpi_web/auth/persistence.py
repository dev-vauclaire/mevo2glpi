"""Adapters between web-authentication use cases and shared repositories."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy.orm import Session, sessionmaker

from mevo2glpi_database import (
    WebUserRepository,
    WebUserSnapshot,
)

from .admin import LocalAccountAdminRepository
from .local import LocalAccount, LocalAccountRepository
from .sessions import LocalSessionAccountReader, LocalSessionAccountState


def _utc_now() -> datetime:
    return datetime.now(UTC)


class SqlAlchemyLocalAccountStore(
    LocalAccountRepository,
    LocalAccountAdminRepository,
    LocalSessionAccountReader,
):
    """Provide bounded transactions for local account operations."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock

    def get_by_username(self, username: str) -> LocalAccount | None:
        with self._session_factory() as session:
            snapshot = WebUserRepository(session).get_by_username(username)
            return _local_account(snapshot) if snapshot is not None else None

    def get_session_state(
        self,
        account_id: UUID,
    ) -> LocalSessionAccountState | None:
        with self._session_factory() as session:
            snapshot = WebUserRepository(session).get_by_uuid(account_id)
            if snapshot is None:
                return None
            return LocalSessionAccountState(
                is_active=snapshot.is_active,
                session_version=snapshot.session_version,
            )

    def record_failed_authentication(
        self,
        account_id: UUID,
        *,
        failed_login_count: int,
        locked_until: datetime | None,
        occurred_at: datetime,
    ) -> None:
        if failed_login_count <= 0:
            raise ValueError("failed_login_count must be positive")
        with self._session_factory.begin() as session:
            WebUserRepository(session).record_failed_login(
                account_id,
                now=occurred_at,
                locked_until=locked_until,
            )

    def record_successful_authentication(
        self,
        account_id: UUID,
        *,
        occurred_at: datetime,
    ) -> None:
        with self._session_factory.begin() as session:
            WebUserRepository(session).reset_failed_logins(
                account_id,
                now=occurred_at,
            )

    def create(
        self,
        *,
        username: str,
        password_hash: str,
        occurred_at: datetime,
    ) -> LocalAccount:
        user_uuid = uuid4()
        with self._session_factory.begin() as session:
            snapshot = WebUserRepository(session).create(
                user_uuid,
                username=username,
                password_hash=password_hash,
                now=occurred_at,
            )
        return _local_account(snapshot)

    def list_all(self) -> Sequence[LocalAccount]:
        with self._session_factory() as session:
            snapshots = WebUserRepository(session).list_all()
            return tuple(_local_account(snapshot) for snapshot in snapshots)

    def set_active(
        self,
        account_id: UUID,
        *,
        is_active: bool,
        increment_session_version: bool,
        occurred_at: datetime,
    ) -> LocalAccount:
        if not increment_session_version:
            raise ValueError("account state changes must invalidate sessions")
        with self._session_factory.begin() as session:
            snapshot = WebUserRepository(session).set_active(
                account_id,
                is_active=is_active,
                now=occurred_at,
            )
        return _local_account(snapshot)

    def replace_password(
        self,
        account_id: UUID,
        *,
        password_hash: str,
        increment_session_version: bool,
        occurred_at: datetime,
    ) -> LocalAccount:
        if not increment_session_version:
            raise ValueError("password reset must invalidate sessions")
        with self._session_factory.begin() as session:
            snapshot = WebUserRepository(session).reset_password(
                account_id,
                password_hash=password_hash,
                now=occurred_at,
            )
        return _local_account(snapshot)


def _local_account(snapshot: WebUserSnapshot) -> LocalAccount:
    return LocalAccount(
        account_id=snapshot.uuid,
        username=snapshot.normalized_username,
        password_hash=snapshot.password_hash,
        is_active=snapshot.is_active,
        session_version=snapshot.session_version,
        failed_login_count=snapshot.failed_login_count,
        locked_until=snapshot.locked_until,
    )


__all__ = ["SqlAlchemyLocalAccountStore"]
