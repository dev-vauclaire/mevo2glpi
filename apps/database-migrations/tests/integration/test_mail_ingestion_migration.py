"""Upgrade contracts for the publisher mail-ingestion schema."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, MetaData, Table, func, inspect, select, text
from sqlalchemy.exc import IntegrityError

from glpi_ticketing_database import create_database_engine
from glpi_ticketing_database_migrations.application import (
    build_alembic_config,
    upgrade_to_head,
)

pytestmark = pytest.mark.database

PREVIOUS_REVISION = "0004_add_observability_queries"
CURRENT_REVISION = "0005_add_mail_ingestions"


@pytest.fixture
def database_engine(database_url: str) -> Iterator[Engine]:
    """Yield an isolated engine and always release its connection pool."""
    engine = create_database_engine(database_url, connect_timeout_seconds=5)
    try:
        yield engine
    finally:
        engine.dispose()


def _head_revision() -> str:
    heads = ScriptDirectory.from_config(build_alembic_config()).get_heads()
    assert heads == [CURRENT_REVISION]
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


def _reflect_tables(engine: Engine) -> dict[str, Table]:
    metadata = MetaData()
    metadata.reflect(bind=engine)
    return dict(metadata.tables)


def test_fresh_database_upgrades_to_mail_ingestion_head(
    database_engine: Engine,
) -> None:
    assert inspect(database_engine).get_table_names() == []

    upgrade_to_head(database_engine)

    assert {"mail_ingestions", "mail_ingestion_events"} <= set(
        inspect(database_engine).get_table_names()
    )
    assert _current_revision(database_engine) == _head_revision()


def test_existing_0004_database_preserves_data_and_enables_publisher(
    database_engine: Engine,
) -> None:
    _migrate(database_engine, PREVIOUS_REVISION)
    job_uuid = uuid4()
    publisher_ingestion_uuid = uuid4()
    worker_instance_ids = {
        "worker_automation": uuid4(),
        "worker_retention": uuid4(),
    }
    with database_engine.begin() as connection:
        # Deliberately reflect the old schema: current ORM metadata already
        # describes the head and must not be used to seed a 0004 database.
        legacy = MetaData()
        legacy.reflect(bind=connection)
        connection.execute(
            legacy.tables["ticket_creation_jobs"].insert(),
            {
                "uuid": job_uuid,
                "storage_key": f"{job_uuid}.wav",
                "audio_extension": "wav",
                "audio_size_bytes": 128,
            },
        )
        connection.execute(
            legacy.tables["ticket_creation_job_events"].insert(),
            {
                "job_uuid": job_uuid,
                "status": "pending",
                "stage": None,
                "event_type": "job_created",
                "actor": "web",
            },
        )
        connection.execute(
            legacy.tables["audio_retentions"].insert(),
            {
                "job_uuid": job_uuid,
                "policy": "ticket_or_deadline",
            },
        )
        connection.execute(
            legacy.tables["worker_runtime_status"].insert(),
            [
                {
                    "service_name": service_name,
                    "instance_id": instance_id,
                    "state": "running",
                }
                for service_name, instance_id in worker_instance_ids.items()
            ],
        )

    upgrade_to_head(database_engine)

    tables = _reflect_tables(database_engine)
    jobs = tables["ticket_creation_jobs"]
    job_events = tables["ticket_creation_job_events"]
    retentions = tables["audio_retentions"]
    runtimes = tables["worker_runtime_status"]
    ingestions = tables["mail_ingestions"]
    ingestion_events = tables["mail_ingestion_events"]
    with database_engine.begin() as connection:
        assert connection.scalar(select(func.count()).select_from(ingestions)) == 0
        assert (
            connection.scalar(select(func.count()).select_from(ingestion_events)) == 0
        )
        connection.execute(
            ingestions.insert(),
            {
                "uuid": publisher_ingestion_uuid,
                "mailbox_id": uuid4(),
                "message_id": "AAMkSyntheticImmutableId",
                "received_at": datetime(2026, 9, 8, tzinfo=UTC),
            },
        )
        connection.execute(
            job_events.insert(),
            {
                "job_uuid": job_uuid,
                "status": "pending",
                "stage": None,
                "event_type": "job_created",
                "actor": "worker_publisher",
            },
        )
        publisher_instance_id = uuid4()
        connection.execute(
            runtimes.insert(),
            {
                "service_name": "worker_publisher",
                "instance_id": publisher_instance_id,
                "state": "running",
                "current_ingestion_uuid": publisher_ingestion_uuid,
            },
        )

    # Applying head again is a no-op and must preserve old and new rows.
    upgrade_to_head(database_engine)

    with database_engine.connect() as connection:
        persisted_job = connection.execute(
            select(jobs.c.uuid, jobs.c.storage_key).where(jobs.c.uuid == job_uuid)
        ).one()
        persisted_retention = connection.execute(
            select(retentions.c.job_uuid, retentions.c.policy).where(
                retentions.c.job_uuid == job_uuid
            )
        ).one()
        persisted_events = connection.execute(
            select(job_events.c.event_type, job_events.c.actor)
            .where(job_events.c.job_uuid == job_uuid)
            .order_by(job_events.c.event_id)
        ).all()
        persisted_runtimes = dict(
            connection.execute(
                select(runtimes.c.service_name, runtimes.c.instance_id)
            ).all()
        )
        publisher_current_ingestion = connection.scalar(
            select(runtimes.c.current_ingestion_uuid).where(
                runtimes.c.service_name == "worker_publisher"
            )
        )

    assert persisted_job == (job_uuid, f"{job_uuid}.wav")
    assert persisted_retention == (job_uuid, "ticket_or_deadline")
    assert persisted_events == [
        ("job_created", "web"),
        ("job_created", "worker_publisher"),
    ]
    assert persisted_runtimes == {
        **worker_instance_ids,
        "worker_publisher": publisher_instance_id,
    }
    assert publisher_current_ingestion == publisher_ingestion_uuid
    assert _current_revision(database_engine) == _head_revision()


def test_downgrade_refuses_to_discard_publisher_audit_rows(
    database_engine: Engine,
) -> None:
    upgrade_to_head(database_engine)
    job_uuid = uuid4()
    publisher_instance_id = uuid4()
    tables = _reflect_tables(database_engine)
    with database_engine.begin() as connection:
        connection.execute(
            tables["ticket_creation_jobs"].insert(),
            {
                "uuid": job_uuid,
                "storage_key": f"{job_uuid}.wav",
                "audio_extension": "wav",
                "audio_size_bytes": 128,
            },
        )
        connection.execute(
            tables["ticket_creation_job_events"].insert(),
            {
                "job_uuid": job_uuid,
                "status": "pending",
                "stage": None,
                "event_type": "job_created",
                "actor": "worker_publisher",
            },
        )
        connection.execute(
            tables["worker_runtime_status"].insert(),
            {
                "service_name": "worker_publisher",
                "instance_id": publisher_instance_id,
                "state": "running",
            },
        )

    with pytest.raises(IntegrityError):
        _migrate(database_engine, PREVIOUS_REVISION, downgrade=True)

    # PostgreSQL transactional DDL leaves the head and every audit row intact.
    tables = _reflect_tables(database_engine)
    with database_engine.connect() as connection:
        actor = connection.scalar(
            select(tables["ticket_creation_job_events"].c.actor).where(
                tables["ticket_creation_job_events"].c.job_uuid == job_uuid
            )
        )
        instance_id = connection.scalar(
            select(tables["worker_runtime_status"].c.instance_id).where(
                tables["worker_runtime_status"].c.service_name == "worker_publisher"
            )
        )

    assert actor == "worker_publisher"
    assert instance_id == publisher_instance_id
    assert {"mail_ingestions", "mail_ingestion_events"} <= tables.keys()
    assert _current_revision(database_engine) == _head_revision()
