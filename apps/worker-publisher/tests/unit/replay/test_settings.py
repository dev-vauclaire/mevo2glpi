from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from glpi_ticketing_worker_publisher.replay.settings import ReplaySettings


def test_replay_settings_require_only_database_configuration() -> None:
    settings = ReplaySettings(
        database_url=SecretStr("postgresql+psycopg://user:password@db/database")
    )

    assert settings.database_connect_timeout_seconds == 5


@pytest.mark.parametrize(
    "database_url",
    ["invalid", "postgresql://user:password@db/database"],
)
def test_replay_settings_reject_invalid_database_urls(database_url: str) -> None:
    with pytest.raises(ValidationError):
        ReplaySettings(database_url=SecretStr(database_url))


def test_replay_settings_reject_boolean_timeout() -> None:
    with pytest.raises(ValidationError):
        ReplaySettings(
            database_url=SecretStr("postgresql+psycopg://user:password@db/database"),
            database_connect_timeout_seconds=True,
        )
