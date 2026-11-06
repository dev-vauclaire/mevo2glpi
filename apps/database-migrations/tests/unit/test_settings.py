from __future__ import annotations

import pytest
from pydantic import ValidationError

from mevo2glpi_common import DeploymentEnvironment
from mevo2glpi_database_migrations.settings import MigrationSettings

DATABASE_URL = "postgresql+psycopg://migration:secret@database/mvp"
COMMIT_SHA = "bd88d901f4569ea8ee455c53b20920b0e1140ee2"


def test_database_url_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("database_url", raising=False)

    with pytest.raises(ValidationError):
        MigrationSettings(_env_file=None)


@pytest.mark.parametrize(
    "invalid_url",
    [
        "not-a-url",
        "sqlite+pysqlite:///local.db",
        "postgresql://migration:secret@database/mvp",
        "postgresql+psycopg2://migration:secret@database/mvp",
        "postgresql+asyncpg://migration:secret@database/mvp",
        "postgresql+psycopg://migration:secret@database",
    ],
)
def test_database_url_requires_sync_psycopg(invalid_url: str) -> None:
    with pytest.raises(ValidationError):
        MigrationSettings(database_url=invalid_url, _env_file=None)


def test_settings_read_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENVIRONMENT", "production")
    monkeypatch.setenv("APP_RELEASE", "v0.2.0")
    monkeypatch.setenv("APP_COMMIT_SHA", COMMIT_SHA)
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("DATABASE_READY_TIMEOUT_SECONDS", "12.5")
    monkeypatch.setenv("DATABASE_READY_INTERVAL_SECONDS", "0.25")
    monkeypatch.setenv("DATABASE_CONNECT_TIMEOUT_SECONDS", "3")

    settings = MigrationSettings(_env_file=None)

    assert settings.release_identity.environment is DeploymentEnvironment.PRODUCTION
    assert settings.release_identity.release == "v0.2.0"
    assert settings.release_identity.commit_sha == COMMIT_SHA
    assert settings.database_url.get_secret_value() == DATABASE_URL
    assert settings.database_ready_timeout_seconds == 12.5
    assert settings.database_ready_interval_seconds == 0.25
    assert settings.database_connect_timeout_seconds == 3


def test_database_url_password_is_hidden_from_repr() -> None:
    secret = "never-render-this-password"
    settings = MigrationSettings(
        database_url=f"postgresql+psycopg://migration:{secret}@database/mvp",
        _env_file=None,
    )

    assert secret not in repr(settings)
    assert DATABASE_URL.split("://", maxsplit=1)[0] not in repr(settings.database_url)


@pytest.mark.parametrize("field_value", [0, -1, True, float("inf"), float("nan")])
@pytest.mark.parametrize(
    "field_name",
    [
        "database_ready_timeout_seconds",
        "database_ready_interval_seconds",
        "database_connect_timeout_seconds",
    ],
)
def test_readiness_numbers_must_be_positive_finite_non_booleans(
    field_name: str,
    field_value: float | bool,
) -> None:
    with pytest.raises(ValidationError):
        MigrationSettings(
            database_url=DATABASE_URL,
            **{field_name: field_value},
            _env_file=None,
        )


def test_connection_timeout_is_capped_at_sixty_seconds() -> None:
    with pytest.raises(ValidationError):
        MigrationSettings(
            database_url=DATABASE_URL,
            database_connect_timeout_seconds=61,
            _env_file=None,
        )
