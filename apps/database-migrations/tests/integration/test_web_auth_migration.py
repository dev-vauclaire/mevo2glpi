"""Upgrade contracts for local web authentication persistence."""

from __future__ import annotations

from collections.abc import Iterator
from uuid import uuid4

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, func, inspect, select, text

from mevo2glpi_database import (
    TicketCreationJob,
    WebUser,
    create_database_engine,
)
from mevo2glpi_database_migrations.application import (
    build_alembic_config,
    upgrade_to_head,
)

pytestmark = pytest.mark.database

PREVIOUS_REVISION = "0009_rejected_retention"
WEB_AUTH_REVISION = "0010_web_authentication"
CURRENT_REVISION = "0011_drop_legacy_polling"


@pytest.fixture
def database_engine(database_url: str) -> Iterator[Engine]:
    """Yield an isolated engine and release its pool before teardown."""
    engine = create_database_engine(database_url, connect_timeout_seconds=5)
    try:
        yield engine
    finally:
        engine.dispose()


def test_fresh_database_creates_local_user_schema_without_session_or_event_table(
    database_engine: Engine,
) -> None:
    assert inspect(database_engine).get_table_names() == []

    upgrade_to_head(database_engine)

    inspector = inspect(database_engine)
    table_names = set(inspector.get_table_names())
    assert "web_users" in table_names
    assert "web_auth_events" not in table_names
    assert "web_sessions" not in table_names
    assert _current_revision(database_engine) == CURRENT_REVISION
    assert _head_revision() == CURRENT_REVISION

    user_columns = {
        column["name"]: column for column in inspector.get_columns("web_users")
    }
    assert user_columns["normalized_username"]["nullable"] is False
    assert user_columns["password_hash"]["nullable"] is False
    assert user_columns["session_version"]["nullable"] is False
    assert user_columns["locked_until"]["nullable"] is True


def test_existing_0009_database_preserves_jobs_and_adds_empty_user_table(
    database_engine: Engine,
) -> None:
    _migrate(database_engine, PREVIOUS_REVISION)
    job_uuid = uuid4()
    with database_engine.begin() as connection:
        connection.execute(
            TicketCreationJob.__table__.insert(),
            {
                "uuid": job_uuid,
                "storage_key": f"{job_uuid}.wav",
                "audio_extension": "wav",
                "audio_size_bytes": 128,
            },
        )

    upgrade_to_head(database_engine)

    with database_engine.connect() as connection:
        assert (
            connection.scalar(
                select(func.count())
                .select_from(TicketCreationJob)
                .where(TicketCreationJob.uuid == job_uuid)
            )
            == 1
        )
        assert connection.scalar(select(func.count()).select_from(WebUser)) == 0
    assert _current_revision(database_engine) == CURRENT_REVISION


def test_empty_web_auth_schema_can_downgrade_and_upgrade_again(
    database_engine: Engine,
) -> None:
    upgrade_to_head(database_engine)

    _migrate(database_engine, PREVIOUS_REVISION, downgrade=True)

    table_names = set(inspect(database_engine).get_table_names())
    assert "web_users" not in table_names
    assert "web_auth_events" not in table_names
    assert _current_revision(database_engine) == PREVIOUS_REVISION

    upgrade_to_head(database_engine)
    assert _current_revision(database_engine) == CURRENT_REVISION


def test_downgrade_refuses_to_discard_local_accounts(
    database_engine: Engine,
) -> None:
    upgrade_to_head(database_engine)
    with database_engine.begin() as connection:
        connection.execute(
            WebUser.__table__.insert(),
            {
                "uuid": uuid4(),
                "normalized_username": "synthetic.operator",
                "password_hash": "$argon2id$v=19$m=65536,t=3,p=4$synthetic$safe",
            },
        )

    with pytest.raises(RuntimeError, match="web users"):
        _migrate(database_engine, PREVIOUS_REVISION, downgrade=True)

    assert _current_revision(database_engine) == CURRENT_REVISION


def _head_revision() -> str:
    heads = ScriptDirectory.from_config(build_alembic_config()).get_heads()
    assert len(heads) == 1
    return heads[0]


def _current_revision(engine: Engine) -> str | None:
    with engine.connect() as connection:
        return connection.scalar(text("SELECT version_num FROM alembic_version"))


def _migrate(engine: Engine, revision: str, *, downgrade: bool = False) -> None:
    config = build_alembic_config()
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        if downgrade:
            command.downgrade(config, revision)
        else:
            command.upgrade(config, revision)
