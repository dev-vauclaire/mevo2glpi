"""Minimal configuration for the database-only replay command."""

from __future__ import annotations

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


class ReplaySettings(BaseSettings):
    """Load only the resources required by replay, never Graph credentials."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="",
        case_sensitive=False,
        env_ignore_empty=True,
        hide_input_in_errors=True,
        extra="ignore",
        frozen=True,
    )

    database_url: SecretStr = Field(repr=False)
    database_connect_timeout_seconds: int = Field(default=5, gt=0)

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, database_url: SecretStr) -> SecretStr:
        """Require the same Psycopg 3 URL as every persistent application."""
        try:
            parsed_url = make_url(database_url.get_secret_value())
        except ArgumentError:
            raise ValueError("DATABASE_URL must be a valid SQLAlchemy URL") from None
        if parsed_url.drivername != "postgresql+psycopg":
            raise ValueError("DATABASE_URL must use the postgresql+psycopg driver")
        return database_url

    @field_validator("database_connect_timeout_seconds", mode="before")
    @classmethod
    def reject_boolean_timeout(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("DATABASE_CONNECT_TIMEOUT_SECONDS cannot be a boolean")
        return value


__all__ = ["ReplaySettings"]
