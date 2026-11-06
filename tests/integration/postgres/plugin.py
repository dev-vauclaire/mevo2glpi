"""Lazy disposable PostgreSQL fixtures shared across workspace applications."""

from __future__ import annotations

import os
import secrets
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import psycopg
import pytest
from psycopg import sql
from sqlalchemy import URL
from testcontainers.compose import DockerCompose
from testcontainers.core.docker_client import DockerClient

if TYPE_CHECKING:
    from sqlalchemy import Engine

POSTGRES_TEST_DIRECTORY = Path(__file__).resolve().parent
POSTGRES_READY_TIMEOUT_SECONDS = 60.0
POSTGRES_POLL_INTERVAL_SECONDS = 0.25


@dataclass(frozen=True, slots=True)
class PostgresTestEnvironment:
    """Session-scoped connection contract for one disposable PostgreSQL."""

    host: str
    port: int
    username: str
    password: str = field(repr=False)
    admin_database: str
    project_name: str
    _compose: DockerCompose = field(repr=False, compare=False)
    _docker_client: DockerClient = field(repr=False, compare=False)

    def url_for(self, database_name: str) -> str:
        """Build a Psycopg SQLAlchemy URL without logging its credentials."""
        return URL.create(
            drivername="postgresql+psycopg",
            username=self.username,
            password=self.password,
            host=self.host,
            port=self.port,
            database=database_name,
        ).render_as_string(hide_password=False)

    def psycopg_url_for(self, database_name: str) -> str:
        """Build the DB-API URL used only by fixture administration calls."""
        return URL.create(
            drivername="postgresql",
            username=self.username,
            password=self.password,
            host=self.host,
            port=self.port,
            database=database_name,
        ).render_as_string(hide_password=False)

    @property
    def admin_psycopg_url(self) -> str:
        """Return the administration database URL used by the fixture."""
        return self.psycopg_url_for(self.admin_database)


def _new_environment() -> dict[str, str]:
    suffix = secrets.token_hex(6)
    return {
        "COMPOSE_PROJECT_NAME": f"mevo2glpi-db-test-{suffix}",
        "DB_ADMIN_USER": f"mevo2glpi_admin_{suffix}",
        "DB_ADMIN_PASSWORD": secrets.token_urlsafe(32),
        "DB_ADMIN_NAME": "mevo2glpi_admin",
    }


def _get_postgres_container(
    compose: DockerCompose,
    docker_client: DockerClient,
):
    compose_container = compose.get_container(service_name="postgres")
    if compose_container.ID is None:
        raise RuntimeError("The PostgreSQL container has no Docker identifier")
    return docker_client.client.containers.get(compose_container.ID)


def _get_loopback_port(
    compose: DockerCompose,
    docker_client: DockerClient,
) -> int:
    container = _get_postgres_container(compose, docker_client)
    bindings = (
        container.attrs.get("NetworkSettings", {}).get("Ports", {}).get("5432/tcp")
    )
    if not isinstance(bindings, list) or len(bindings) != 1:
        raise RuntimeError("PostgreSQL must expose exactly one test port")
    binding = bindings[0]
    if binding.get("HostIp") != "127.0.0.1":
        raise RuntimeError("PostgreSQL must be exposed only on IPv4 loopback")
    try:
        return int(binding["HostPort"])
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError("PostgreSQL has no usable random port") from error


def _wait_for_postgres(database_url: str) -> None:
    deadline = time.monotonic() + POSTGRES_READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            with psycopg.connect(database_url, connect_timeout=2) as connection:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT 1")
                    if cursor.fetchone() == (1,):
                        return
        except psycopg.OperationalError:
            time.sleep(POSTGRES_POLL_INTERVAL_SECONDS)
    raise RuntimeError("PostgreSQL did not become ready before the test deadline")


def _clean_test_resources(
    compose: DockerCompose,
    docker_client: DockerClient,
    environment: Mapping[str, str],
) -> None:
    cleanup_failed = False
    try:
        compose.stop()
    except Exception:
        cleanup_failed = True

    try:
        if compose.get_containers(include_all=True):
            cleanup_failed = True
        network_name = f"{environment['COMPOSE_PROJECT_NAME']}_database_test"
        if docker_client.client.networks.list(names=[network_name]):
            cleanup_failed = True
    except Exception:
        cleanup_failed = True
    finally:
        docker_client.client.close()

    if cleanup_failed:
        raise RuntimeError("Disposable PostgreSQL resources were not fully removed")


@pytest.fixture(scope="session")
def postgres_environment() -> Iterator[PostgresTestEnvironment]:
    """Start PostgreSQL once, only when a database fixture is requested."""
    if os.environ.get("PYTEST_XDIST_WORKER") is not None:
        pytest.fail("The database integration suite must not use pytest-xdist")

    environment = _new_environment()
    docker_client = DockerClient()
    compose = DockerCompose(
        context=POSTGRES_TEST_DIRECTORY,
        compose_file_name="compose.yml",
        pull=False,
        build=False,
        wait=False,
    )
    primary_error: BaseException | None = None

    try:
        with patch.dict(os.environ, environment, clear=False):
            compose.start()
            port = _get_loopback_port(compose, docker_client)
            test_environment = PostgresTestEnvironment(
                host="127.0.0.1",
                port=port,
                username=environment["DB_ADMIN_USER"],
                password=environment["DB_ADMIN_PASSWORD"],
                admin_database=environment["DB_ADMIN_NAME"],
                project_name=environment["COMPOSE_PROJECT_NAME"],
                _compose=compose,
                _docker_client=docker_client,
            )
            _wait_for_postgres(test_environment.admin_psycopg_url)
        yield test_environment
    except BaseException as error:
        primary_error = error
        raise
    finally:
        try:
            with patch.dict(os.environ, environment, clear=False):
                _clean_test_resources(compose, docker_client, environment)
        except Exception:
            if primary_error is None:
                raise
            primary_error.add_note(
                "PostgreSQL cleanup also failed; the primary error was preserved"
            )


@pytest.fixture
def database_url(postgres_environment: PostgresTestEnvironment) -> Iterator[str]:
    """Create an isolated empty database and drop it after one test."""
    database_name = f"mevo2glpi_test_{secrets.token_hex(8)}"
    with psycopg.connect(
        postgres_environment.admin_psycopg_url,
        autocommit=True,
    ) as connection:
        connection.execute(
            sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name))
        )

    try:
        yield postgres_environment.url_for(database_name)
    finally:
        with psycopg.connect(
            postgres_environment.admin_psycopg_url,
            autocommit=True,
        ) as connection:
            connection.execute(
                "SELECT pg_terminate_backend(pid) "
                "FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (database_name,),
            )
            connection.execute(
                sql.SQL("DROP DATABASE {}").format(sql.Identifier(database_name))
            )


@pytest.fixture
def migrated_database_url(database_url: str) -> str:
    """Apply the workspace Alembic head to an isolated database."""
    from mevo2glpi_database import create_database_engine
    from mevo2glpi_database_migrations.application import upgrade_to_head

    engine = create_database_engine(database_url, connect_timeout_seconds=5)
    try:
        upgrade_to_head(engine)
    finally:
        engine.dispose()
    return database_url


@pytest.fixture
def migrated_engine(migrated_database_url: str) -> Iterator[Engine]:
    """Yield an engine connected to an isolated database at Alembic head."""
    from mevo2glpi_database import create_database_engine

    engine = create_database_engine(
        migrated_database_url,
        connect_timeout_seconds=5,
    )
    try:
        yield engine
    finally:
        engine.dispose()
