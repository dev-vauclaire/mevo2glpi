"""Integration contracts for the Alembic migration lifecycle."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import CheckConstraint, Engine, MetaData, Table, inspect, select, text

from mevo2glpi_database import (
    AudioRetention,
    Base,
    TicketCreationJob,
    TicketCreationJobEvent,
    WorkerRuntimeStatus,
    create_database_engine,
)
from mevo2glpi_database_migrations.application import (
    MigrationExitCode,
    build_alembic_config,
    run_cli,
    upgrade_to_head,
)
from mevo2glpi_database_migrations.settings import MigrationSettings

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


def _downgrade_to_revision(engine: Engine, revision: str) -> None:
    config = build_alembic_config()
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, revision)


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


def test_upgrade_from_0002_preserves_data_and_enables_retention_events(
    database_engine: Engine,
) -> None:
    _upgrade_to_revision(database_engine, "0002_add_operator_retry_event")
    job_uuid = uuid4()
    now = datetime(2026, 8, 28, 12, tzinfo=UTC)
    with database_engine.begin() as connection:
        legacy_retentions = Table(
            "audio_retentions",
            MetaData(),
            autoload_with=connection,
        )
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
            legacy_retentions.insert(),
            {
                "job_uuid": job_uuid,
                "policy": "ticket_or_deadline",
                "status": "scheduled",
                "hard_delete_at": now + timedelta(days=90),
                "next_check_at": now + timedelta(hours=6),
                "last_glpi_status": "5",
            },
        )
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

    upgrade_to_head(database_engine)

    retention_event_types = (
        "retention_retry_scheduled",
        "retention_blocked",
        "audio_deletion_started",
        "audio_deleted",
    )
    with database_engine.begin() as connection:
        persisted_retention = connection.execute(
            select(
                AudioRetention.job_uuid,
                AudioRetention.last_glpi_status_id,
            ).where(AudioRetention.job_uuid == job_uuid)
        ).one()
        connection.execute(
            TicketCreationJobEvent.__table__.insert(),
            [
                {
                    "job_uuid": job_uuid,
                    "status": "pending",
                    "stage": None,
                    "event_type": event_type,
                    "actor": "worker_retention",
                }
                for event_type in retention_event_types
            ],
        )

    assert persisted_retention == (job_uuid, 5)
    assert _current_revision(database_engine) == _single_head()
    with database_engine.connect() as connection:
        event_types = connection.scalars(
            select(TicketCreationJobEvent.event_type)
            .where(TicketCreationJobEvent.job_uuid == job_uuid)
            .order_by(TicketCreationJobEvent.event_id)
        ).all()
    assert event_types == ["operator_retry_requested", *retention_event_types]


def test_upgrade_from_0003_preserves_runtime_state_and_adds_observability_index(
    database_engine: Engine,
) -> None:
    _upgrade_to_revision(database_engine, "0003_add_retention_lifecycle")
    instance_id = uuid4()
    with database_engine.begin() as connection:
        connection.execute(
            WorkerRuntimeStatus.__table__.insert(),
            {
                "service_name": "worker_automation",
                "instance_id": instance_id,
                "state": "degraded",
                "last_error_category": "database",
                "last_error_code": "connection_lost",
            },
        )

    upgrade_to_head(database_engine)

    with database_engine.connect() as connection:
        persisted = connection.execute(
            select(
                WorkerRuntimeStatus.instance_id,
                WorkerRuntimeStatus.state,
                WorkerRuntimeStatus.last_error_category,
            )
        ).one()
    indexes = {
        index["name"]: tuple(index["column_names"])
        for index in inspect(database_engine).get_indexes("audio_retentions")
    }
    assert persisted == (instance_id, "degraded", "database")
    assert indexes["ix_audio_retentions_status_dashboard"] == (
        "status",
        "updated_at",
        "job_uuid",
    )
    assert _current_revision(database_engine) == _single_head()


def test_observability_migration_has_a_non_destructive_downgrade(
    database_engine: Engine,
) -> None:
    upgrade_to_head(database_engine)
    instance_id = uuid4()
    with database_engine.begin() as connection:
        connection.execute(
            WorkerRuntimeStatus.__table__.insert(),
            {
                "service_name": "worker_retention",
                "instance_id": instance_id,
                "state": "running",
            },
        )

    _downgrade_to_revision(database_engine, "0003_add_retention_lifecycle")

    inspector = inspect(database_engine)
    index_names = {
        index["name"] for index in inspector.get_indexes(AudioRetention.__tablename__)
    }
    runtime_check_names = {
        constraint["name"]
        for constraint in inspector.get_check_constraints(
            WorkerRuntimeStatus.__tablename__
        )
    }
    with database_engine.connect() as connection:
        persisted = connection.execute(
            select(
                WorkerRuntimeStatus.instance_id,
                WorkerRuntimeStatus.state,
            )
        ).one()

    assert "ix_audio_retentions_status_dashboard" not in index_names
    assert "ck_worker_runtime_status_error_for_state" not in runtime_check_names
    assert persisted == (instance_id, "running")
    assert _current_revision(database_engine) == "0003_add_retention_lifecycle"

    upgrade_to_head(database_engine)

    with database_engine.connect() as connection:
        assert connection.scalar(select(WorkerRuntimeStatus.instance_id)) == instance_id
    assert _current_revision(database_engine) == _single_head()


def test_retention_status_id_migration_has_a_symmetric_downgrade(
    database_engine: Engine,
) -> None:
    upgrade_to_head(database_engine)
    job_uuid = uuid4()
    now = datetime(2026, 8, 28, 12, tzinfo=UTC)
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
            AudioRetention.__table__.insert(),
            {
                "job_uuid": job_uuid,
                "policy": "ticket_or_deadline",
                "status": "scheduled",
                "hard_delete_at": now + timedelta(days=90),
                "next_check_at": now + timedelta(hours=6),
                "last_glpi_status_id": 6,
            },
        )

    _downgrade_to_revision(database_engine, "0002_add_operator_retry_event")

    with database_engine.connect() as connection:
        legacy_retentions = Table(
            "audio_retentions",
            MetaData(),
            autoload_with=connection,
        )
        assert "last_glpi_status" in legacy_retentions.c
        assert "last_glpi_status_id" not in legacy_retentions.c
        assert (
            connection.scalar(
                select(legacy_retentions.c.last_glpi_status).where(
                    legacy_retentions.c.job_uuid == job_uuid
                )
            )
            == "6"
        )

    upgrade_to_head(database_engine)

    with database_engine.connect() as connection:
        assert (
            connection.scalar(
                select(AudioRetention.last_glpi_status_id).where(
                    AudioRetention.job_uuid == job_uuid
                )
            )
            == 6
        )


def test_upgrade_from_0006_preserves_events_and_allows_transcription_failure(
    database_engine: Engine,
) -> None:
    _upgrade_to_revision(database_engine, "0006_add_ogg_replay_support")
    job_uuid = uuid4()
    with database_engine.begin() as connection:
        connection.execute(
            TicketCreationJob.__table__.insert(),
            {
                "uuid": job_uuid,
                "storage_key": f"{job_uuid}.ogg",
                "audio_extension": "ogg",
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
                "actor": "worker_publisher",
            },
        )

    upgrade_to_head(database_engine)

    with database_engine.begin() as connection:
        connection.execute(
            TicketCreationJobEvent.__table__.insert(),
            {
                "job_uuid": job_uuid,
                "status": "processing",
                "stage": "glpi_creation",
                "event_type": "transcription_failed",
                "error_scope": "job",
                "error_category": "terminal",
                "technical_code": "invalid_transcription",
                "actor": "worker_automation",
            },
        )
        event_types = connection.scalars(
            select(TicketCreationJobEvent.event_type)
            .where(TicketCreationJobEvent.job_uuid == job_uuid)
            .order_by(TicketCreationJobEvent.event_id)
        ).all()

    assert event_types == ["job_created", "transcription_failed"]
    assert _current_revision(database_engine) == _single_head()


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
