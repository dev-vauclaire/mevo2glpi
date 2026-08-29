"""Typed configuration for the audio-retention worker."""

from pathlib import Path
from typing import Self

from pydantic import Field, HttpUrl, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


class RetentionWorkerSettings(BaseSettings):
    """Configuration owned by the singleton retention process."""

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
    audio_root: Path = Path("/data/audios")
    database_connect_timeout_seconds: int = Field(default=5, gt=0)

    retention_poll_interval_seconds: float = Field(
        default=60.0,
        gt=0,
        allow_inf_nan=False,
    )
    retention_retry_initial_seconds: float = Field(
        default=5.0,
        gt=0,
        allow_inf_nan=False,
    )
    retention_retry_max_seconds: float = Field(
        default=300.0,
        gt=0,
        allow_inf_nan=False,
    )
    retention_system_probe_interval_seconds: float = Field(
        default=60.0,
        gt=0,
        allow_inf_nan=False,
    )
    retention_check_interval_hours: int = Field(default=6, gt=0)

    glpi_base_url: HttpUrl
    glpi_username: str
    glpi_password: SecretStr = Field(repr=False)
    glpi_client_id: str
    glpi_client_secret: SecretStr = Field(repr=False)
    glpi_http_timeout_seconds: float = Field(
        default=10.0,
        gt=0,
        allow_inf_nan=False,
    )

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, database_url: SecretStr) -> SecretStr:
        """Require Psycopg 3 without exposing database credentials."""
        try:
            parsed_url = make_url(database_url.get_secret_value())
        except ArgumentError:
            raise ValueError("DATABASE_URL must be a valid SQLAlchemy URL") from None
        if parsed_url.drivername != "postgresql+psycopg":
            raise ValueError("DATABASE_URL must use the postgresql+psycopg driver")
        return database_url

    @field_validator(
        "database_connect_timeout_seconds",
        "retention_check_interval_hours",
        "retention_poll_interval_seconds",
        "retention_retry_initial_seconds",
        "retention_retry_max_seconds",
        "retention_system_probe_interval_seconds",
        "glpi_http_timeout_seconds",
        mode="before",
    )
    @classmethod
    def reject_boolean_duration_settings(cls, value: object) -> object:
        """Reject booleans instead of silently accepting numeric coercion."""
        if isinstance(value, bool):
            raise ValueError("Duration settings cannot be booleans")
        return value

    @field_validator("glpi_base_url")
    @classmethod
    def validate_glpi_base_url(cls, base_url: HttpUrl) -> HttpUrl:
        """Reject URL components that make GLPI endpoints ambiguous."""
        if (
            base_url.username
            or base_url.password
            or base_url.query is not None
            or base_url.fragment is not None
        ):
            raise ValueError(
                "GLPI_BASE_URL must not contain credentials, a query, or a fragment"
            )
        return base_url

    @field_validator("glpi_username", "glpi_client_id")
    @classmethod
    def validate_glpi_identifier(cls, value: str) -> str:
        """Reject empty, padded or control-character-bearing identifiers."""
        if (
            not value
            or value != value.strip()
            or any(not character.isprintable() for character in value)
        ):
            raise ValueError("GLPI identifiers must be non-empty and printable")
        return value

    @field_validator("glpi_password", "glpi_client_secret")
    @classmethod
    def validate_glpi_secret(cls, secret: SecretStr) -> SecretStr:
        """Reject empty, padded or control-character-bearing secrets."""
        value = secret.get_secret_value()
        if (
            not value
            or value != value.strip()
            or any(not character.isprintable() for character in value)
        ):
            raise ValueError("GLPI secrets must be non-empty and printable")
        return secret

    @model_validator(mode="after")
    def validate_retry_range(self) -> Self:
        """Keep the exponential retry schedule internally consistent."""
        if self.retention_retry_initial_seconds > self.retention_retry_max_seconds:
            raise ValueError(
                "RETENTION_RETRY_INITIAL_SECONDS must not exceed "
                "RETENTION_RETRY_MAX_SECONDS"
            )
        return self


__all__ = ["RetentionWorkerSettings"]
