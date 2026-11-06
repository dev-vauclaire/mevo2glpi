import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from mevo2glpi_database import engine as engine_module
from mevo2glpi_database.engine import (
    DatabaseConfigurationError,
    create_database_engine,
    create_session_factory,
)

DATABASE_URL = (
    "postgresql+psycopg://mvp_user:temporary-database-secret@localhost:5432/mvp"
)


@pytest.mark.parametrize(
    "database_url",
    [
        "sqlite+pysqlite:///:memory:",
        "postgresql://user:password@localhost/database",
        "postgresql+psycopg2://user:password@localhost/database",
    ],
)
def test_engine_factory_rejects_non_postgresql_psycopg_url(
    database_url: str,
) -> None:
    with pytest.raises(
        DatabaseConfigurationError,
        match="must use the postgresql\\+psycopg driver",
    ):
        create_database_engine(database_url)


@pytest.mark.parametrize("database_url", ["", "not a database URL"])
def test_engine_factory_rejects_invalid_url_without_echoing_it(
    database_url: str,
) -> None:
    with pytest.raises(DatabaseConfigurationError) as caught:
        create_database_engine(database_url)

    assert database_url not in str(caught.value) or database_url == ""


def test_engine_factory_requires_a_string() -> None:
    with pytest.raises(TypeError, match="database_url must be a string"):
        create_database_engine(None)  # type: ignore[arg-type]


def test_engine_configuration_hides_database_password() -> None:
    engine = create_database_engine(DATABASE_URL, connect_timeout_seconds=7)

    try:
        assert isinstance(engine, Engine)
        assert engine.dialect.name == "postgresql"
        assert engine.dialect.driver == "psycopg"
        assert engine.url.query.get("connect_timeout") is None
        assert "temporary-database-secret" not in str(engine.url)
        assert "temporary-database-secret" not in repr(engine.url)
        assert "temporary-database-secret" not in repr(engine)
    finally:
        engine.dispose()


@pytest.mark.parametrize("timeout", [True, 0, -1, 1.5])
def test_engine_factory_rejects_invalid_connect_timeout(timeout: object) -> None:
    with pytest.raises(
        ValueError,
        match="connect_timeout_seconds must be a positive integer",
    ):
        create_database_engine(
            DATABASE_URL,
            connect_timeout_seconds=timeout,  # type: ignore[arg-type]
        )


def test_engine_factory_passes_connect_timeout_outside_the_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected_engine = object()
    captured_options: dict[str, object] = {}

    def fake_create_engine(_url: object, **options: object) -> object:
        captured_options.update(options)
        return expected_engine

    monkeypatch.setattr(engine_module, "create_engine", fake_create_engine)

    result = create_database_engine(DATABASE_URL, connect_timeout_seconds=7)

    assert result is expected_engine
    assert captured_options["pool_pre_ping"] is True
    assert captured_options["connect_args"] == {"connect_timeout": 7}


def test_session_factory_uses_explicit_short_transaction_defaults() -> None:
    engine = create_database_engine(DATABASE_URL)

    try:
        session_factory = create_session_factory(engine)

        assert session_factory.kw["bind"] is engine
        assert session_factory.kw["autoflush"] is False
        assert session_factory.kw["expire_on_commit"] is False
        assert issubclass(session_factory.class_, Session)
    finally:
        engine.dispose()


def test_session_factory_rejects_non_engine() -> None:
    with pytest.raises(TypeError, match="engine must be a SQLAlchemy Engine"):
        create_session_factory(object())  # type: ignore[arg-type]
