"""Typed runtime settings for the one-shot migration application."""

from __future__ import annotations

import math
from typing import Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from mevo2glpi_common import DeploymentEnvironment, ReleaseIdentity


class MigrationSettings(BaseSettings):
    """Load the PostgreSQL target without exposing its credentials."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="",
        case_sensitive=False,
        env_ignore_empty=True,
        extra="ignore",
        frozen=True,
    )

    app_environment: DeploymentEnvironment = DeploymentEnvironment.DEVELOPMENT
    app_release: str = "development"
    app_commit_sha: str = "unknown"
    database_url: SecretStr
    database_ready_timeout_seconds: float = Field(default=60.0, gt=0)
    database_ready_interval_seconds: float = Field(default=1.0, gt=0)
    database_connect_timeout_seconds: int = Field(default=5, gt=0, le=60)

    @field_validator(
        "database_ready_timeout_seconds",
        "database_ready_interval_seconds",
        "database_connect_timeout_seconds",
        mode="before",
    )
    @classmethod
    def reject_boolean_numbers(cls, value: object) -> object:
        """Reject booleans instead of accepting their integer representation."""
        if isinstance(value, bool):
            raise ValueError("numeric settings cannot be booleans")
        return value

    @field_validator(
        "database_ready_timeout_seconds",
        "database_ready_interval_seconds",
    )
    @classmethod
    def require_finite_numbers(cls, value: float) -> float:
        """Keep readiness deadlines bounded and deterministic."""
        if not math.isfinite(value):
            raise ValueError("numeric settings must be finite")
        return value

    @field_validator("database_url")
    @classmethod
    def require_sync_psycopg_url(cls, value: SecretStr) -> SecretStr:
        """Accept only the PostgreSQL driver used by the shared package."""
        raw_url = value.get_secret_value()
        try:
            parsed_url = make_url(raw_url)
        except ArgumentError:
            raise ValueError("DATABASE_URL must be a valid SQLAlchemy URL") from None

        if parsed_url.drivername != "postgresql+psycopg":
            raise ValueError("DATABASE_URL must use postgresql+psycopg")
        if not parsed_url.database:
            raise ValueError("DATABASE_URL must select a database")
        return value

    @model_validator(mode="after")
    def validate_release_identity(self) -> Self:
        """Reject an incoherent environment, release and Git revision."""
        _ = self.release_identity
        return self

    @property
    def release_identity(self) -> ReleaseIdentity:
        """Return the validated non-sensitive identity of this process."""
        return ReleaseIdentity(
            environment=self.app_environment,
            release=self.app_release,
            commit_sha=self.app_commit_sha,
        )


__all__ = ["MigrationSettings"]
