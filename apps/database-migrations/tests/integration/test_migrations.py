"""Integration contracts for the Alembic migration lifecycle."""

from __future__ import annotations

from collections.abc import Iterator
from uuid import uuid4

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import CheckConstraint, Engine, inspect, select, text

from glpi_ticketing_database import (
    Base,
    TicketCreationJob,
    TicketCreationJobEvent,
    create_database_engine,
)
from glpi_ticketing_database_migrations.application import (
    MigrationExitCode,
    build_alembic_config,
    run_cli,
    upgrade_to_head,
)
from glpi_ticketing_database_migrations.settings import MigrationSettings

pytestmark = pytest.mark.database


@pytest.fixture
def database_engine(database_url: str) -> Iterator[Engine]:
    """Yield an engine whose pool is always released before database teardown."""

    engine = create_database_engine(database_url, connect_timeout_seconds=5)
    try:
        yield engine
    finally:
        engine.dispose()


def _single_head() -> str:
    heads = ScriptDirectory.from_config(build_alembic_config()).get_heads()
    assert len(heads) == 1
    return heads[0]


def _current_revision(engine: Engine) -> str | None:
    with engine.connect() as connection:
        return connection.scalar(text("SELECT version_num FROM alembic_version"))


def _upgrade_to_revision(engine: Engine, revision: str) -> None:
    config = build_alembic_config()
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, revision)


def test_revision_graph_has_one_head() -> None:
    assert _single_head()


def test_empty_database_upgrades_to_head(database_engine: Engine) -> None:
    assert inspect(database_engine).get_table_names() == []

    upgrade_to_head(database_engine)

    assert set(inspect(database_engine).get_table_names()) == {
        *Base.metadata.tables,
        "alembic_version",
    }
    assert _current_revision(database_engine) == _single_head()


def test_reapplying_head_is_idempotent_and_preserves_data(
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

    upgrade_to_head(database_engine)

    with database_engine.connect() as connection:
        persisted_job = connection.execute(
            select(
                TicketCreationJob.uuid,
                TicketCreationJob.status,
                TicketCreationJob.storage_key,
            ).where(TicketCreationJob.uuid == job_uuid)
        ).one()
    assert persisted_job == (job_uuid, "pending", f"{job_uuid}.wav")
    assert _current_revision(database_engine) == _single_head()


def test_upgrade_from_0001_preserves_events_and_allows_operator_retry(
    database_engine: Engine,
) -> None:
    _upgrade_to_revision(database_engine, "0001_initial_schema")
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
            TicketCreationJobEvent.__table__.insert(),
            {
                "job_uuid": job_uuid,
                "status": "pending",
                "stage": None,
                "event_type": "job_created",
                "actor": "web",
            },
        )

    upgrade_to_head(database_engine)

    with database_engine.begin() as connection:
        historical_event = connection.execute(
            select(
                TicketCreationJobEvent.job_uuid,
                TicketCreationJobEvent.event_type,
            ).where(TicketCreationJobEvent.job_uuid == job_uuid)
        ).one()
        connection.execute(
            TicketCreationJobEvent.__table__.insert(),
            {
                "job_uuid": job_uuid,
                "status": "pending",
                "stage": None,
                "event_type": "operator_retry_requested",
                "actor": "web",
            },
        )

    assert historical_event == (job_uuid, "job_created")
    assert _current_revision(database_engine) == _single_head()
    with database_engine.connect() as connection:
        event_types = connection.scalars(
            select(TicketCreationJobEvent.event_type)
            .where(TicketCreationJobEvent.job_uuid == job_uuid)
            .order_by(TicketCreationJobEvent.event_id)
        ).all()
    assert event_types == ["job_created", "operator_retry_requested"]


def test_migrated_schema_matches_shared_metadata(database_engine: Engine) -> None:
    upgrade_to_head(database_engine)

    with database_engine.connect() as connection:
        migration_context = MigrationContext.configure(
            connection,
            opts={"compare_type": True},
        )
        differences = compare_metadata(migration_context, Base.metadata)

    assert differences == []

    inspector = inspect(database_engine)
    for table_name, table in Base.metadata.tables.items():
        expected_check_names = {
            str(constraint.name)
            for constraint in table.constraints
            if isinstance(constraint, CheckConstraint)
        }
        migrated_check_names = {
            str(constraint["name"])
            for constraint in inspector.get_check_constraints(table_name)
        }
        assert migrated_check_names == expected_check_names


def test_one_shot_application_upgrades_then_returns_success(
    database_url: str,
) -> None:
    settings = MigrationSettings(
        database_url=database_url,
        database_ready_timeout_seconds=5.0,
        database_ready_interval_seconds=0.01,
        database_connect_timeout_seconds=2,
        _env_file=None,
    )

    exit_code = run_cli(settings_factory=lambda: settings)

    assert exit_code is MigrationExitCode.SUCCESS
    engine = create_database_engine(database_url, connect_timeout_seconds=2)
    try:
        assert _current_revision(engine) == _single_head()
    finally:
        engine.dispose()
