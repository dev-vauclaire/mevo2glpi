"""Repository dedicated to the ``WebUser`` model."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import WebUser
from ._validation import validate_utc_datetime, validate_uuid

_USERNAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]{2,63}", re.ASCII)
_MAX_PASSWORD_HASH_LENGTH = 512


class WebUserNotFoundError(LookupError):
    """Report a mutation requested for an unknown local web user."""


@dataclass(frozen=True, slots=True, kw_only=True)
class WebUserSnapshot:
    """Immutable local-account state required by authentication services."""

    uuid: UUID
    normalized_username: str
    password_hash: str = field(repr=False)
    is_active: bool
    session_version: int
    failed_login_count: int
    locked_until: datetime | None
    created_at: datetime
    updated_at: datetime


class WebUserRepository:
    """Manage local web users inside a caller-owned transaction."""

    def __init__(self, session: Session) -> None:
        if not isinstance(session, Session):
            raise TypeError("session must be a SQLAlchemy Session")
        self._session = session

    def create(
        self,
        user_uuid: UUID,
        *,
        username: str,
        password_hash: str,
        now: datetime,
    ) -> WebUserSnapshot:
        """Persist one active local account with normalized source input."""
        validate_uuid(user_uuid, "user_uuid")
        normalized_username = normalize_web_username(username)
        password_hash = _validate_password_hash(password_hash)
        now = validate_utc_datetime(now, "now")

        user = WebUser(
            uuid=user_uuid,
            normalized_username=normalized_username,
            password_hash=password_hash,
            is_active=True,
            session_version=0,
            failed_login_count=0,
            locked_until=None,
            created_at=now,
            updated_at=now,
        )
        self._session.add(user)
        self._session.flush()
        return _to_snapshot(user)

    def get_by_uuid(self, user_uuid: UUID) -> WebUserSnapshot | None:
        """Return one local user by primary key."""
        validate_uuid(user_uuid, "user_uuid")
        user = self._session.get(WebUser, user_uuid)
        return _to_snapshot(user) if user is not None else None

    def get_by_username(self, username: str) -> WebUserSnapshot | None:
        """Find one user after applying the canonical username normalization."""
        normalized_username = normalize_web_username(username)
        user = self._session.scalars(
            select(WebUser)
            .where(WebUser.normalized_username == normalized_username)
            .limit(1)
        ).first()
        return _to_snapshot(user) if user is not None else None

    def list_all(self) -> tuple[WebUserSnapshot, ...]:
        """List the deliberately small local account set deterministically."""
        users = self._session.scalars(
            select(WebUser).order_by(WebUser.normalized_username, WebUser.uuid)
        ).all()
        return tuple(_to_snapshot(user) for user in users)

    def set_active(
        self,
        user_uuid: UUID,
        *,
        is_active: bool,
        now: datetime,
    ) -> WebUserSnapshot:
        """Enable or disable a user and invalidate existing signed sessions."""
        if not isinstance(is_active, bool):
            raise TypeError("is_active must be a boolean")
        now = validate_utc_datetime(now, "now")
        user = self._get_for_update(user_uuid)
        if user.is_active != is_active:
            user.is_active = is_active
            user.session_version += 1
            user.failed_login_count = 0
            user.locked_until = None
            user.updated_at = now
            self._session.flush()
        return _to_snapshot(user)

    def record_failed_login(
        self,
        user_uuid: UUID,
        *,
        now: datetime,
        locked_until: datetime | None = None,
    ) -> WebUserSnapshot:
        """Atomically increment a known user's consecutive login failures."""
        now = validate_utc_datetime(now, "now")
        if locked_until is not None:
            locked_until = validate_utc_datetime(locked_until, "locked_until")
            if locked_until <= now:
                raise ValueError("locked_until must be later than now")
        user = self._get_for_update(user_uuid)
        user.failed_login_count += 1
        if locked_until is not None:
            user.locked_until = locked_until
        user.updated_at = now
        self._session.flush()
        return _to_snapshot(user)

    def reset_failed_logins(
        self,
        user_uuid: UUID,
        *,
        now: datetime,
    ) -> WebUserSnapshot:
        """Clear a user's failed-login counter and temporary lock."""
        now = validate_utc_datetime(now, "now")
        user = self._get_for_update(user_uuid)
        if user.failed_login_count != 0 or user.locked_until is not None:
            user.failed_login_count = 0
            user.locked_until = None
            user.updated_at = now
            self._session.flush()
        return _to_snapshot(user)

    def reset_password(
        self,
        user_uuid: UUID,
        *,
        password_hash: str,
        now: datetime,
    ) -> WebUserSnapshot:
        """Replace an Argon2id hash and invalidate every existing session."""
        password_hash = _validate_password_hash(password_hash)
        now = validate_utc_datetime(now, "now")
        user = self._get_for_update(user_uuid)
        user.password_hash = password_hash
        user.session_version += 1
        user.failed_login_count = 0
        user.locked_until = None
        user.updated_at = now
        self._session.flush()
        return _to_snapshot(user)

    def _get_for_update(self, user_uuid: UUID) -> WebUser:
        validate_uuid(user_uuid, "user_uuid")
        user = self._session.get(WebUser, user_uuid, with_for_update=True)
        if user is None:
            raise WebUserNotFoundError(f"web user {user_uuid} does not exist")
        return user


def normalize_web_username(username: str) -> str:
    """Return the NFKC/casefolded identity used for unique local lookup."""
    if not isinstance(username, str):
        raise TypeError("username must be a string")
    normalized = unicodedata.normalize("NFKC", username.strip()).casefold()
    if _USERNAME_PATTERN.fullmatch(normalized) is None:
        raise ValueError("username must use 3-64 ASCII safe characters")
    return normalized


def _validate_password_hash(password_hash: str) -> str:
    if not isinstance(password_hash, str):
        raise TypeError("password_hash must be a string")
    if not password_hash.startswith("$argon2id$"):
        raise ValueError("password_hash must use Argon2id")
    if len(password_hash) > _MAX_PASSWORD_HASH_LENGTH:
        raise ValueError(
            f"password_hash must contain at most {_MAX_PASSWORD_HASH_LENGTH} characters"
        )
    return password_hash


def _to_snapshot(user: WebUser) -> WebUserSnapshot:
    return WebUserSnapshot(
        uuid=user.uuid,
        normalized_username=user.normalized_username,
        password_hash=user.password_hash,
        is_active=user.is_active,
        session_version=user.session_version,
        failed_login_count=user.failed_login_count,
        locked_until=user.locked_until,
        created_at=user.created_at,
        updated_at=user.updated_at,
    )


__all__ = [
    "WebUserNotFoundError",
    "WebUserRepository",
    "WebUserSnapshot",
    "normalize_web_username",
]
