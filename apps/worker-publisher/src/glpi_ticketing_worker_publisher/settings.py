"""Typed configuration for Microsoft 365 mail ingestion."""

import math
from enum import StrEnum
from pathlib import Path
from typing import Self
from uuid import UUID

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024


class PublisherLogLevel(StrEnum):
    """Supported application log levels for the publisher worker."""

    DEBUG = "DEBUG"
    INFO = "INFO"


class PublisherSettings(BaseSettings):
    """Configuration required by the singleton publisher process."""

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

    publisher_entra_tenant_id: UUID
    publisher_entra_client_id: UUID
    publisher_entra_private_key_pem_path: Path
    publisher_entra_certificate_pem_path: Path
    publisher_entra_private_key_passphrase: SecretStr | None = Field(
        default=None,
        repr=False,
    )
    publisher_mailbox_id: UUID
    database_url: SecretStr = Field(repr=False)
    audio_root: Path = Path("/data/audios")
    publisher_log_level: PublisherLogLevel = PublisherLogLevel.INFO
    database_connect_timeout_seconds: int = Field(default=5, gt=0)
    publisher_graph_page_size: int = Field(default=50, ge=1, le=1000)
    publisher_max_attachment_bytes: int = Field(
        default=MAX_ATTACHMENT_BYTES,
        gt=0,
        le=MAX_ATTACHMENT_BYTES,
    )
    publisher_http_timeout_seconds: float = Field(
        default=10.0,
        gt=0,
        allow_inf_nan=False,
    )
    publisher_poll_interval_seconds: float = Field(
        default=30.0,
        gt=0,
        allow_inf_nan=False,
    )
    publisher_heartbeat_interval_seconds: float = Field(
        default=10.0,
        gt=0,
        allow_inf_nan=False,
    )
    publisher_retry_initial_seconds: float = Field(
        default=5.0,
        gt=0,
        allow_inf_nan=False,
    )
    publisher_retry_max_seconds: float = Field(
        default=300.0,
        gt=0,
        allow_inf_nan=False,
    )
    publisher_system_probe_interval_seconds: float = Field(
        default=60.0,
        gt=0,
        allow_inf_nan=False,
    )

    @field_validator("publisher_entra_private_key_pem_path")
    @classmethod
    def validate_private_key_path(cls, path: Path) -> Path:
        """Require a mounted, readable private-key PEM secret."""
        return _validate_readable_file(
            path,
            variable_name="PUBLISHER_ENTRA_PRIVATE_KEY_PEM_PATH",
        )

    @field_validator("publisher_entra_certificate_pem_path")
    @classmethod
    def validate_certificate_path(cls, path: Path) -> Path:
        """Require a mounted, readable public X.509 certificate PEM."""
        return _validate_readable_file(
            path,
            variable_name="PUBLISHER_ENTRA_CERTIFICATE_PEM_PATH",
        )

    @field_validator("publisher_entra_private_key_passphrase")
    @classmethod
    def validate_private_key_passphrase(
        cls,
        secret: SecretStr | None,
    ) -> SecretStr | None:
        """Allow an unencrypted key and preserve a supplied passphrase verbatim."""
        if secret is None:
            return None
        value = secret.get_secret_value()
        if not value or any(not character.isprintable() for character in value):
            raise ValueError(
                "PUBLISHER_ENTRA_PRIVATE_KEY_PASSPHRASE must be non-empty, "
                "and printable"
            )
        return secret

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, database_url: SecretStr) -> SecretStr:
        """Require the shared synchronous Psycopg 3 database contract."""
        try:
            parsed_url = make_url(database_url.get_secret_value())
        except ArgumentError:
            raise ValueError("DATABASE_URL must be a valid SQLAlchemy URL") from None
        if parsed_url.drivername != "postgresql+psycopg":
            raise ValueError("DATABASE_URL must use the postgresql+psycopg driver")
        return database_url

    @field_validator("publisher_log_level", mode="before")
    @classmethod
    def normalize_log_level(cls, value: object) -> object:
        """Accept conventional case-insensitive publisher log levels."""
        if isinstance(value, str):
            return value.strip().upper()
        return value

    @field_validator(
        "database_connect_timeout_seconds",
        "publisher_graph_page_size",
        "publisher_max_attachment_bytes",
        mode="before",
    )
    @classmethod
    def reject_boolean_integer_settings(cls, value: object) -> object:
        """Reject booleans instead of accepting integer coercion."""
        if isinstance(value, bool):
            raise ValueError("Integer publisher settings cannot be booleans")
        return value

    @field_validator(
        "publisher_http_timeout_seconds",
        "publisher_poll_interval_seconds",
        "publisher_heartbeat_interval_seconds",
        "publisher_retry_initial_seconds",
        "publisher_retry_max_seconds",
        "publisher_system_probe_interval_seconds",
        mode="before",
    )
    @classmethod
    def reject_boolean_duration_settings(cls, value: object) -> object:
        """Reject booleans instead of accepting duration coercion."""
        if isinstance(value, bool):
            raise ValueError("Publisher duration settings cannot be booleans")
        if isinstance(value, int | float) and not math.isfinite(value):
            raise ValueError("Publisher duration settings must be finite")
        return value

    @model_validator(mode="after")
    def validate_runtime_ranges(self) -> Self:
        """Keep the retry schedule and PEM paths internally coherent."""
        if self.publisher_retry_initial_seconds > self.publisher_retry_max_seconds:
            raise ValueError(
                "PUBLISHER_RETRY_INITIAL_SECONDS must not exceed "
                "PUBLISHER_RETRY_MAX_SECONDS"
            )
        if (
            self.publisher_entra_private_key_pem_path.resolve()
            == self.publisher_entra_certificate_pem_path.resolve()
        ):
            raise ValueError("The private key and certificate must use distinct files")
        return self


def _validate_readable_file(path: Path, *, variable_name: str) -> Path:
    if not path.is_file():
        raise ValueError(f"{variable_name} must be a file")
    try:
        with path.open("rb"):
            pass
    except OSError:
        raise ValueError(f"{variable_name} must be readable") from None
    return path


__all__ = ["MAX_ATTACHMENT_BYTES", "PublisherLogLevel", "PublisherSettings"]
