"""PostgreSQL contracts for local web users."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError

from mevo2glpi_database import (
    WebUserRepository,
    create_session_factory,
)

pytestmark = pytest.mark.database

NOW = datetime(2026, 11, 10, 12, tzinfo=UTC)
USER_UUID = UUID("00000000-0000-0000-0000-000000001201")
PASSWORD_HASH = "$argon2id$v=19$m=65536,t=3,p=4$synthetic$safehash"
NEW_PASSWORD_HASH = "$argon2id$v=19$m=65536,t=4,p=4$synthetic$newhash"


def test_local_user_lifecycle_invalidates_signed_sessions(
    migrated_engine: Engine,
) -> None:
    session_factory = create_session_factory(migrated_engine)
    with session_factory.begin() as session:
        created = WebUserRepository(session).create(
            USER_UUID,
            username=" Synthetic.Operator ",
            password_hash=PASSWORD_HASH,
            now=NOW,
        )

    assert created.normalized_username == "synthetic.operator"
    assert created.session_version == 0

    with session_factory.begin() as session:
        repository = WebUserRepository(session)
        failed = repository.record_failed_login(
            USER_UUID,
            now=NOW + timedelta(seconds=1),
            locked_until=NOW + timedelta(minutes=15),
        )
        reset = repository.reset_password(
            USER_UUID,
            password_hash=NEW_PASSWORD_HASH,
            now=NOW + timedelta(seconds=2),
        )

    assert failed.failed_login_count == 1
    assert failed.locked_until == NOW + timedelta(minutes=15)
    assert reset.session_version == 1
    assert reset.failed_login_count == 0
    assert reset.locked_until is None

    with session_factory.begin() as session:
        repository = WebUserRepository(session)
        disabled = repository.set_active(
            USER_UUID,
            is_active=False,
            now=NOW + timedelta(seconds=3),
        )
        enabled = repository.set_active(
            USER_UUID,
            is_active=True,
            now=NOW + timedelta(seconds=4),
        )

    assert disabled.session_version == 2
    assert enabled.session_version == 3
    with session_factory() as session:
        persisted = WebUserRepository(session).get_by_username("SYNTHETIC.OPERATOR")
    assert persisted is not None
    assert persisted.is_active is True
    assert persisted.session_version == 3


def test_normalized_username_is_unique(migrated_engine: Engine) -> None:
    session_factory = create_session_factory(migrated_engine)
    with session_factory.begin() as session:
        WebUserRepository(session).create(
            USER_UUID,
            username="Synthetic.Operator",
            password_hash=PASSWORD_HASH,
            now=NOW,
        )

    with pytest.raises(IntegrityError):
        with session_factory.begin() as session:
            WebUserRepository(session).create(
                UUID("00000000-0000-0000-0000-000000001202"),
                username=" synthetic.operator ",
                password_hash=PASSWORD_HASH,
                now=NOW,
            )
