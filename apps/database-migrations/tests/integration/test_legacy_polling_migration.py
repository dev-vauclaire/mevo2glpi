"""Upgrade contracts for removing legacy polling persistence."""

from __future__ import annotations

from collections.abc import Iterator
from uuid import uuid4

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, func, inspect, select, text
from sqlalchemy.exc import IntegrityError

from mevo2glpi_database import (
    TicketCreationJob,
    TicketCreationJobEvent,
    create_database_engine,
)
from mevo2glpi_database_migrations.application import (
    build_alembic_config,
    upgrade_to_head,
)

pytestmark = pytest.mark.database

PREVIOUS_REVISION = "0010_web_authentication"
CURRENT_REVISION = "0011_drop_legacy_polling"


@pytest.fixture
def database_engine(database_url: str) -> Iterator[Engine]:
    """Yield an isolated engine and release its pool before teardown."""
    engine = create_database_engine(database_url, connect_timeout_seconds=5)
    try:
        yield engine
    finally:
        engine.dispose()


def test_fresh_database_contains_no_legacy_polling_artifact(
    database_engine: Engine,
) -> None:
    upgrade_to_head(database_engine)

    inspector = inspect(database_engine)
    job_columns = {
        column["name"] for column in inspector.get_columns("ticket_creation_jobs")
    }
    assert "transcription_provider_job_id" not in job_columns
    assert _current_revision(database_engine) == CURRENT_REVISION
    assert _head_revision() == CURRENT_REVISION

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
    with pytest.raises(IntegrityError):
        with database_engine.begin() as connection:
            connection.execute(
                TicketCreationJobEvent.__table__.insert(),
                {
                    "job_uuid": job_uuid,
                    "status": "pending",
                    "stage": None,
                    "event_type": "transcription_submitted",
                    "actor": "worker_automation",
                },
            )


def test_upgrade_drops_provider_ids_and_only_obsolete_events(
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
        connection.execute(
            text(
                "UPDATE ticket_creation_jobs "
                "SET transcription_provider_job_id = :provider_job_id "
                "WHERE uuid = :job_uuid"
            ),
            {"provider_job_id": "synthetic-provider-job", "job_uuid": job_uuid},
        )
        connection.execute(
            TicketCreationJobEvent.__table__.insert(),
            [
                {
                    "job_uuid": job_uuid,
                    "status": "pending",
                    "stage": None,
                    "event_type": "job_created",
                    "actor": "worker_publisher",
                },
                {
                    "job_uuid": job_uuid,
                    "status": "processing",
                    "stage": "transcription",
                    "event_type": "transcription_submitted",
                    "actor": "worker_automation",
                },
            ],
        )

    upgrade_to_head(database_engine)

    job_columns = {
        column["name"]
        for column in inspect(database_engine).get_columns("ticket_creation_jobs")
    }
    assert "transcription_provider_job_id" not in job_columns
    with database_engine.connect() as connection:
        assert (
            connection.scalar(
                select(func.count())
                .select_from(TicketCreationJob)
                .where(TicketCreationJob.uuid == job_uuid)
            )
            == 1
        )
        assert connection.scalars(
            select(TicketCreationJobEvent.event_type)
            .where(TicketCreationJobEvent.job_uuid == job_uuid)
            .order_by(TicketCreationJobEvent.event_id)
        ).all() == ["job_created"]
    assert _current_revision(database_engine) == CURRENT_REVISION


def test_downgrade_restores_legacy_schema_without_fabricating_events(
    database_engine: Engine,
) -> None:
    upgrade_to_head(database_engine)
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

    _migrate(database_engine, PREVIOUS_REVISION, downgrade=True)

    job_columns = {
        column["name"]
        for column in inspect(database_engine).get_columns("ticket_creation_jobs")
    }
    assert "transcription_provider_job_id" in job_columns
    with database_engine.begin() as connection:
        assert (
            connection.scalar(
                text(
                    "SELECT transcription_provider_job_id "
                    "FROM ticket_creation_jobs WHERE uuid = :job_uuid"
                ),
                {"job_uuid": job_uuid},
            )
            is None
        )
        connection.execute(
            TicketCreationJobEvent.__table__.insert(),
            {
                "job_uuid": job_uuid,
                "status": "processing",
                "stage": "transcription",
                "event_type": "transcription_submitted",
                "actor": "worker_automation",
            },
        )
    assert _current_revision(database_engine) == PREVIOUS_REVISION


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
