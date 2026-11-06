"""Synchronous SQLAlchemy engine and session construction."""

from __future__ import annotations

from sqlalchemy import URL, create_engine
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.orm import Session, sessionmaker

REQUIRED_DATABASE_DRIVER = "postgresql+psycopg"


class DatabaseConfigurationError(ValueError):
    """Report invalid database configuration without exposing its value."""


def create_database_engine(
    database_url: str,
    *,
    echo: bool = False,
    connect_timeout_seconds: int | None = None,
) -> Engine:
    """Build a lazy synchronous PostgreSQL engine using Psycopg 3.

    The function validates the driver before SQLAlchemy creates the engine. It
    never interpolates the supplied URL into validation errors, because that URL
    may contain a database password.
    """

    url = _parse_database_url(database_url)
    connect_args: dict[str, int] = {}
    if connect_timeout_seconds is not None:
        if (
            isinstance(connect_timeout_seconds, bool)
            or not isinstance(connect_timeout_seconds, int)
            or connect_timeout_seconds <= 0
        ):
            raise ValueError("connect_timeout_seconds must be a positive integer")
        connect_args["connect_timeout"] = connect_timeout_seconds
    return create_engine(
        url,
        echo=echo,
        pool_pre_ping=True,
        connect_args=connect_args,
    )


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Create the shared synchronous session factory.

    Applications define transaction boundaries with ``Session.begin()``; no
    additional Unit of Work abstraction is introduced by this package.
    """

    if not isinstance(engine, Engine):
        raise TypeError("engine must be a SQLAlchemy Engine")
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _parse_database_url(database_url: str) -> URL:
    if not isinstance(database_url, str):
        raise TypeError("database_url must be a string")
    if not database_url:
        raise DatabaseConfigurationError("database_url must not be empty")

    try:
        url = make_url(database_url)
    except ArgumentError:
        raise DatabaseConfigurationError(
            "database_url must be a valid SQLAlchemy URL"
        ) from None

    if url.drivername != REQUIRED_DATABASE_DRIVER:
        raise DatabaseConfigurationError(
            f"database_url must use the {REQUIRED_DATABASE_DRIVER} driver"
        )
    return url
