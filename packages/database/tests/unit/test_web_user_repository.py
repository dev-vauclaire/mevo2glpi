from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import create_autospec
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session

import mevo2glpi_database as database_api
from mevo2glpi_database import (
    WebUser,
    WebUserNotFoundError,
    WebUserRepository,
    normalize_web_username,
)

NOW = datetime(2026, 11, 10, 12, tzinfo=UTC)
USER_UUID = UUID("00000000-0000-0000-0000-000000001001")
PASSWORD_HASH = "$argon2id$v=19$m=65536,t=3,p=4$synthetic$safehash"
NEW_PASSWORD_HASH = "$argon2id$v=19$m=65536,t=4,p=4$synthetic$newhash"


def test_create_normalizes_username_and_never_owns_transaction() -> None:
    session = _session()

    snapshot = WebUserRepository(session).create(
        USER_UUID,
        username="  Synthetic.Operator  ",
        password_hash=PASSWORD_HASH,
        now=NOW,
    )

    user = session.add.call_args.args[0]
    assert isinstance(user, WebUser)
    assert user.normalized_username == "synthetic.operator"
    assert snapshot.normalized_username == "synthetic.operator"
    assert snapshot.password_hash == PASSWORD_HASH
    assert snapshot.session_version == 0
    session.flush.assert_called_once_with()
    session.commit.assert_not_called()
    session.rollback.assert_not_called()
    session.close.assert_not_called()


def test_get_by_username_applies_the_same_normalization() -> None:
    session = _session()
    session.scalars.return_value.first.return_value = _user()

    snapshot = WebUserRepository(session).get_by_username(" SYNTHETIC.OPERATOR ")

    assert snapshot is not None
    assert snapshot.uuid == USER_UUID
    statement = session.scalars.call_args.args[0]
    assert "synthetic.operator" in statement.compile().params.values()
    session.flush.assert_not_called()


def test_list_all_returns_deterministic_typed_snapshots() -> None:
    session = _session()
    second = _user()
    second.uuid = uuid4()
    second.normalized_username = "synthetic.second"
    session.scalars.return_value.all.return_value = [_user(), second]

    snapshots = WebUserRepository(session).list_all()

    assert tuple(user.normalized_username for user in snapshots) == (
        "synthetic.operator",
        "synthetic.second",
    )
    session.flush.assert_not_called()


def test_disabling_user_invalidates_sessions_and_clears_login_lock() -> None:
    session = _session()
    user = _user()
    user.failed_login_count = 4
    user.locked_until = NOW + timedelta(minutes=10)
    session.get.return_value = user

    snapshot = WebUserRepository(session).set_active(
        USER_UUID,
        is_active=False,
        now=NOW,
    )

    assert snapshot.is_active is False
    assert snapshot.session_version == 1
    assert snapshot.failed_login_count == 0
    assert snapshot.locked_until is None
    assert snapshot.updated_at == NOW
    session.flush.assert_called_once_with()
    session.commit.assert_not_called()


def test_set_active_is_idempotent_when_state_is_unchanged() -> None:
    session = _session()
    session.get.return_value = _user()

    snapshot = WebUserRepository(session).set_active(
        USER_UUID,
        is_active=True,
        now=NOW,
    )

    assert snapshot.session_version == 0
    session.flush.assert_not_called()


def test_failed_login_is_atomic_and_may_apply_a_temporary_lock() -> None:
    session = _session()
    session.get.return_value = _user()
    locked_until = NOW + timedelta(minutes=15)

    snapshot = WebUserRepository(session).record_failed_login(
        USER_UUID,
        now=NOW,
        locked_until=locked_until,
    )

    assert snapshot.failed_login_count == 1
    assert snapshot.locked_until == locked_until
    assert session.get.call_args.kwargs["with_for_update"] is True
    session.flush.assert_called_once_with()


def test_reset_password_invalidates_sessions_and_resets_failures() -> None:
    session = _session()
    user = _user()
    user.session_version = 7
    user.failed_login_count = 2
    user.locked_until = NOW + timedelta(minutes=5)
    session.get.return_value = user

    snapshot = WebUserRepository(session).reset_password(
        USER_UUID,
        password_hash=NEW_PASSWORD_HASH,
        now=NOW,
    )

    assert snapshot.password_hash == NEW_PASSWORD_HASH
    assert snapshot.session_version == 8
    assert snapshot.failed_login_count == 0
    assert snapshot.locked_until is None
    session.flush.assert_called_once_with()


def test_reset_failed_logins_is_idempotent() -> None:
    session = _session()
    session.get.return_value = _user()

    snapshot = WebUserRepository(session).reset_failed_logins(USER_UUID, now=NOW)

    assert snapshot.failed_login_count == 0
    session.flush.assert_not_called()


def test_unknown_user_mutation_raises_without_flushing() -> None:
    session = _session()
    session.get.return_value = None

    with pytest.raises(WebUserNotFoundError, match="does not exist"):
        WebUserRepository(session).set_active(
            USER_UUID,
            is_active=False,
            now=NOW,
        )

    session.flush.assert_not_called()


@pytest.mark.parametrize("password_hash", ["plaintext", "$argon2i$not-argon2id"])
def test_password_hash_must_be_argon2id(password_hash: str) -> None:
    session = _session()

    with pytest.raises(ValueError, match="Argon2id"):
        WebUserRepository(session).create(
            USER_UUID,
            username="synthetic.operator",
            password_hash=password_hash,
            now=NOW,
        )

    session.add.assert_not_called()


def test_username_normalization_contract_is_exported() -> None:
    assert normalize_web_username("  Synthetic.Operator  ") == "synthetic.operator"
    assert database_api.WebUserRepository is WebUserRepository
    assert database_api.normalize_web_username is normalize_web_username


def _session() -> Session:
    return create_autospec(Session, instance=True)


def _user() -> WebUser:
    return WebUser(
        uuid=USER_UUID,
        normalized_username="synthetic.operator",
        password_hash=PASSWORD_HASH,
        is_active=True,
        session_version=0,
        failed_login_count=0,
        locked_until=None,
        created_at=NOW - timedelta(days=1),
        updated_at=NOW - timedelta(days=1),
    )
