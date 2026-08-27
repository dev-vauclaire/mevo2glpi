from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from pydantic import ValidationError
from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError

from glpi_ticketing_database.engine import DatabaseConfigurationError
from glpi_ticketing_database.models import Base
from glpi_ticketing_database_migrations.application import (
    ALEMBIC_INI_PATH,
    DatabaseUnavailableError,
    MigrationExitCode,
    build_alembic_config,
    run_application,
    run_cli,
    wait_for_database,
)
from glpi_ticketing_database_migrations.settings import MigrationSettings

DATABASE_URL = "postgresql+psycopg://migration:secret@database/mvp"


def _settings(**overrides: object) -> MigrationSettings:
    values: dict[str, object] = {
        "database_url": DATABASE_URL,
        "database_ready_timeout_seconds": 5.0,
        "database_ready_interval_seconds": 1.0,
    }
    values.update(overrides)
    return MigrationSettings(**values, _env_file=None)


def _operational_error() -> OperationalError:
    return OperationalError("SELECT 1", {}, ConnectionError("not ready"))


def _monotonic(values: list[float]) -> Iterator[float]:
    yield from values


def test_wait_for_database_returns_after_successful_probe() -> None:
    engine = MagicMock(spec=Engine)
    connection = engine.connect.return_value.__enter__.return_value

    wait_for_database(
        engine,
        timeout_seconds=5.0,
        interval_seconds=1.0,
        sleep=lambda _seconds: pytest.fail("sleep must not be called"),
        monotonic=lambda: 0.0,
    )

    connection.execute.assert_called_once()


def test_wait_for_database_retries_with_injected_sleep() -> None:
    engine = MagicMock(spec=Engine)
    successful_context = MagicMock()
    engine.connect.side_effect = [_operational_error(), successful_context]
    monotonic_values = _monotonic([0.0, 0.25])
    sleeps: list[float] = []

    wait_for_database(
        engine,
        timeout_seconds=5.0,
        interval_seconds=1.0,
        sleep=sleeps.append,
        monotonic=lambda: next(monotonic_values),
    )

    assert sleeps == [1.0]
    successful_context.__enter__.return_value.execute.assert_called_once()


def test_wait_for_database_caps_sleep_at_remaining_deadline() -> None:
    engine = MagicMock(spec=Engine)
    engine.connect.side_effect = [_operational_error(), _operational_error()]
    monotonic_values = _monotonic([10.0, 10.75, 11.0])
    sleeps: list[float] = []

    with pytest.raises(DatabaseUnavailableError):
        wait_for_database(
            engine,
            timeout_seconds=1.0,
            interval_seconds=4.0,
            sleep=sleeps.append,
            monotonic=lambda: next(monotonic_values),
        )

    assert sleeps == [0.25]


def test_run_application_waits_then_upgrades_and_disposes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = MagicMock(spec=Engine)
    events: list[str] = []

    def engine_factory(
        database_url: str,
        *,
        echo: bool,
        connect_timeout_seconds: int | None,
    ) -> Engine:
        assert database_url == DATABASE_URL
        assert echo is False
        assert connect_timeout_seconds == 5
        events.append("engine")
        return engine

    def wait_stub(*_args: object, **_kwargs: object) -> None:
        events.append("ready")

    def upgrade_runner(received_engine: Engine) -> None:
        assert received_engine is engine
        events.append("upgrade")

    monkeypatch.setattr(
        "glpi_ticketing_database_migrations.application.wait_for_database",
        wait_stub,
    )
    engine.dispose.side_effect = lambda: events.append("dispose")

    run_application(
        _settings(),
        engine_factory=engine_factory,
        upgrade_runner=upgrade_runner,
    )

    assert events == ["engine", "ready", "upgrade", "dispose"]


def test_run_application_disposes_engine_after_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = MagicMock(spec=Engine)

    def fail_wait(*_args: object, **_kwargs: object) -> None:
        raise DatabaseUnavailableError

    monkeypatch.setattr(
        "glpi_ticketing_database_migrations.application.wait_for_database",
        fail_wait,
    )

    with pytest.raises(DatabaseUnavailableError):
        run_application(
            _settings(),
            engine_factory=lambda _url, **_kwargs: engine,
        )

    engine.dispose.assert_called_once_with()


def test_alembic_resources_are_independent_from_current_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)

    config = build_alembic_config()
    scripts = ScriptDirectory.from_config(config)

    assert ALEMBIC_INI_PATH.is_absolute()
    assert Path(scripts.dir).is_absolute()
    assert Path(scripts.dir).parent == ALEMBIC_INI_PATH.parent
    assert len(scripts.get_heads()) == 1


def test_offline_migration_preserves_metadata_object_names() -> None:
    config = build_alembic_config()
    config.set_main_option(
        "sqlalchemy.url",
        "postgresql+psycopg://migration:synthetic@localhost/mvp",
    )
    output = StringIO()
    config.output_buffer = output

    command.upgrade(config, "head", sql=True)

    expected_names = {
        database_object.name
        for table in Base.metadata.tables.values()
        for database_object in (*table.constraints, *table.indexes)
        if database_object.name is not None
    }
    rendered_names = set(
        re.findall(r"(?:CONSTRAINT|INDEX) ([A-Za-z0-9_]+)", output.getvalue())
    )
    assert expected_names <= rendered_names


def test_run_cli_returns_success() -> None:
    settings = _settings()
    observed: list[MigrationSettings] = []

    exit_code = run_cli(
        settings_factory=lambda: settings,
        application_runner=observed.append,
    )

    assert exit_code is MigrationExitCode.SUCCESS
    assert observed == [settings]


def test_run_cli_classifies_expected_failures() -> None:
    settings = _settings()

    def unavailable(_settings: MigrationSettings) -> None:
        raise DatabaseUnavailableError

    def configuration_error(_settings: MigrationSettings) -> None:
        raise DatabaseConfigurationError("private configuration")

    def migration_error(_settings: MigrationSettings) -> None:
        raise RuntimeError("private migration detail")

    assert (
        run_cli(settings_factory=lambda: settings, application_runner=unavailable)
        is MigrationExitCode.DATABASE_UNAVAILABLE
    )
    assert (
        run_cli(
            settings_factory=lambda: settings,
            application_runner=configuration_error,
        )
        is MigrationExitCode.CONFIGURATION_ERROR
    )
    assert (
        run_cli(settings_factory=lambda: settings, application_runner=migration_error)
        is MigrationExitCode.MIGRATION_ERROR
    )


def test_run_cli_does_not_log_database_secret_or_exception_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "never-log-this-database-password"
    settings = _settings(
        database_url=f"postgresql+psycopg://migration:{secret}@database/mvp"
    )
    exception_detail = "never-log-this-migration-error"

    def fail(_settings: MigrationSettings) -> None:
        raise RuntimeError(exception_detail)

    with caplog.at_level(logging.INFO):
        exit_code = run_cli(
            settings_factory=lambda: settings,
            application_runner=fail,
        )

    assert exit_code is MigrationExitCode.MIGRATION_ERROR
    assert secret not in caplog.text
    assert exception_detail not in caplog.text
    assert "error_type=RuntimeError" in caplog.text


def test_run_cli_does_not_log_invalid_database_url(
    caplog: pytest.LogCaptureFixture,
) -> None:
    invalid_url = "postgresql+psycopg://private:secret@database"

    def invalid_settings() -> MigrationSettings:
        return MigrationSettings(database_url=invalid_url, _env_file=None)

    with caplog.at_level(logging.INFO):
        exit_code = run_cli(settings_factory=invalid_settings)

    assert exit_code is MigrationExitCode.CONFIGURATION_ERROR
    assert invalid_url not in caplog.text


def test_run_cli_classifies_settings_factory_validation_error() -> None:
    def invalid_settings() -> MigrationSettings:
        raise ValidationError.from_exception_data("MigrationSettings", [])

    assert (
        run_cli(settings_factory=invalid_settings)
        is MigrationExitCode.CONFIGURATION_ERROR
    )
