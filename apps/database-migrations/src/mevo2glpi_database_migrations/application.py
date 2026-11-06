"""Composition root and bounded lifecycle of the migration application."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from enum import IntEnum
from pathlib import Path
from typing import Protocol

from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy import Engine, text
from sqlalchemy.exc import OperationalError

from mevo2glpi_database.engine import (
    DatabaseConfigurationError,
    create_database_engine,
)

from .settings import MigrationSettings

LOGGER = logging.getLogger(__name__)
ALEMBIC_INI_PATH = Path(__file__).resolve().parent / "alembic.ini"


class MigrationExitCode(IntEnum):
    """Stable process exit codes exposed to the container runtime."""

    SUCCESS = 0
    UNEXPECTED_ERROR = 1
    CONFIGURATION_ERROR = 2
    DATABASE_UNAVAILABLE = 3
    MIGRATION_ERROR = 4


class DatabaseUnavailableError(RuntimeError):
    """Report that PostgreSQL stayed unreachable until the configured deadline."""


class _EngineFactory(Protocol):
    def __call__(
        self,
        database_url: str,
        *,
        echo: bool = False,
        connect_timeout_seconds: int | None = None,
    ) -> Engine:
        """Return the synchronous SQLAlchemy engine owned by this process."""
        ...


class _UpgradeRunner(Protocol):
    def __call__(self, engine: Engine) -> None:
        """Upgrade the database represented by ``engine`` to Alembic head."""
        ...


def build_alembic_config() -> Config:
    """Build an Alembic configuration from package-relative resources."""
    return Config(str(ALEMBIC_INI_PATH))


def wait_for_database(
    engine: Engine,
    *,
    timeout_seconds: float,
    interval_seconds: float,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    """Poll PostgreSQL until it answers or the bounded deadline expires."""
    if not isinstance(engine, Engine):
        raise TypeError("engine must be a SQLAlchemy Engine")
    if not callable(sleep):
        raise TypeError("sleep must be callable")
    if not callable(monotonic):
        raise TypeError("monotonic must be callable")

    deadline = monotonic() + timeout_seconds
    while True:
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            return
        except OperationalError:
            remaining_seconds = deadline - monotonic()
            if remaining_seconds <= 0:
                raise DatabaseUnavailableError(
                    "database did not become ready before the deadline"
                ) from None
            sleep(min(interval_seconds, remaining_seconds))


def upgrade_to_head(engine: Engine) -> None:
    """Run Alembic against an existing engine without embedding its URL."""
    if not isinstance(engine, Engine):
        raise TypeError("engine must be a SQLAlchemy Engine")

    config = build_alembic_config()
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")


def run_application(
    settings: MigrationSettings,
    *,
    engine_factory: _EngineFactory = create_database_engine,
    upgrade_runner: _UpgradeRunner = upgrade_to_head,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    """Wait for PostgreSQL, migrate it exactly once, and release the engine."""
    if not isinstance(settings, MigrationSettings):
        raise TypeError("settings must be MigrationSettings")

    engine = engine_factory(
        settings.database_url.get_secret_value(),
        echo=False,
        connect_timeout_seconds=settings.database_connect_timeout_seconds,
    )
    try:
        wait_for_database(
            engine,
            timeout_seconds=settings.database_ready_timeout_seconds,
            interval_seconds=settings.database_ready_interval_seconds,
            sleep=sleep,
            monotonic=monotonic,
        )
        upgrade_runner(engine)
    finally:
        engine.dispose()


def run_cli(
    *,
    settings_factory: Callable[[], MigrationSettings] = MigrationSettings,
    application_runner: Callable[[MigrationSettings], None] = run_application,
) -> MigrationExitCode:
    """Load configuration and migrate with diagnostics safe for production logs."""
    try:
        settings = settings_factory()
    except ValidationError:
        LOGGER.error("database migration configuration is invalid")
        return MigrationExitCode.CONFIGURATION_ERROR
    except Exception as error:  # pragma: no cover - defensive startup boundary
        LOGGER.error(
            "database migration configuration loading failed error_type=%s",
            type(error).__name__,
        )
        return MigrationExitCode.UNEXPECTED_ERROR

    release_identity = settings.release_identity
    LOGGER.info(
        "database migration starting app_environment=%s app_release=%s "
        "app_commit_sha=%s",
        release_identity.environment.value,
        release_identity.release,
        release_identity.commit_sha,
    )
    try:
        application_runner(settings)
    except DatabaseUnavailableError:
        LOGGER.error("database did not become ready before the configured deadline")
        return MigrationExitCode.DATABASE_UNAVAILABLE
    except DatabaseConfigurationError:
        LOGGER.error("database migration runtime configuration is invalid")
        return MigrationExitCode.CONFIGURATION_ERROR
    except Exception as error:
        LOGGER.error(
            "database migration failed error_type=%s",
            type(error).__name__,
        )
        return MigrationExitCode.MIGRATION_ERROR

    LOGGER.info("database migration completed")
    return MigrationExitCode.SUCCESS


def main() -> int:
    """Console-script entry point."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    return int(run_cli())


__all__ = [
    "ALEMBIC_INI_PATH",
    "DatabaseUnavailableError",
    "MigrationExitCode",
    "build_alembic_config",
    "main",
    "run_application",
    "run_cli",
    "upgrade_to_head",
    "wait_for_database",
]
