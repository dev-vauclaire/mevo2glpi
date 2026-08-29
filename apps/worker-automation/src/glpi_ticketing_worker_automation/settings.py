"""Typed configuration for the automation worker."""

from pathlib import Path
from typing import Self

from pydantic import Field, HttpUrl, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from glpi_ticketing_glpi import TicketLevel, TicketType


class WorkerSettings(BaseSettings):
    """Configuration owned by the automation worker process."""

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

    public_base_url: HttpUrl
    database_url: SecretStr = Field(repr=False)
    audio_root: Path = Path("/data/audios")
    database_connect_timeout_seconds: int = Field(default=5, gt=0)
    worker_idle_interval_seconds: float = Field(
        default=1.0,
        gt=0,
        allow_inf_nan=False,
    )
    worker_retry_initial_seconds: float = Field(
        default=5.0,
        gt=0,
        allow_inf_nan=False,
    )
    worker_retry_max_seconds: float = Field(
        default=300.0,
        gt=0,
        allow_inf_nan=False,
    )
    worker_system_probe_interval_seconds: float = Field(
        default=60.0,
        gt=0,
        allow_inf_nan=False,
    )
    completed_retention_days: int = Field(default=90, gt=0)
    failed_retention_days: int = Field(default=30, gt=0)
    blocked_retention_days: int = Field(default=90, gt=0)
    retention_check_interval_hours: int = Field(default=6, gt=0)

    legacy_transcription_base_url: HttpUrl
    legacy_transcription_api_key: SecretStr = Field(repr=False)
    transcription_http_timeout_seconds: float = Field(
        default=10.0,
        gt=0,
        allow_inf_nan=False,
    )
    transcription_poll_interval_seconds: float = Field(
        default=2.0,
        gt=0,
        allow_inf_nan=False,
    )
    transcription_job_timeout_seconds: float = Field(
        default=15 * 60.0,
        gt=0,
        allow_inf_nan=False,
    )

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
    glpi_ticket_title: str = Field(min_length=1, max_length=255)
    glpi_ticket_type: TicketType
    glpi_ticket_urgency: TicketLevel
    glpi_ticket_impact: TicketLevel

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, database_url: SecretStr) -> SecretStr:
        """Require Psycopg 3 without exposing database credentials."""
        raw_url = database_url.get_secret_value()
        try:
            parsed_url = make_url(raw_url)
        except ArgumentError:
            raise ValueError("DATABASE_URL must be a valid SQLAlchemy URL") from None
        if parsed_url.drivername != "postgresql+psycopg":
            raise ValueError("DATABASE_URL must use the postgresql+psycopg driver")
        return database_url

    @field_validator(
        "database_connect_timeout_seconds",
        "completed_retention_days",
        "failed_retention_days",
        "blocked_retention_days",
        "retention_check_interval_hours",
        mode="before",
    )
    @classmethod
    def reject_boolean_integer_settings(cls, value: object) -> object:
        """Reject booleans instead of silently treating them as durations."""
        if isinstance(value, bool):
            raise ValueError("Integer settings cannot be booleans")
        return value

    @field_validator("public_base_url")
    @classmethod
    def validate_public_base_url(cls, public_base_url: HttpUrl) -> HttpUrl:
        """Keep the base URL safe and unambiguous for future audio links."""
        has_credentials = bool(public_base_url.username or public_base_url.password)
        has_suffix = (
            public_base_url.query is not None or public_base_url.fragment is not None
        )

        if has_credentials or has_suffix:
            msg = "PUBLIC_BASE_URL must not contain credentials, a query, or a fragment"
            raise ValueError(msg)

        return public_base_url

    @field_validator("legacy_transcription_base_url")
    @classmethod
    def validate_legacy_transcription_base_url(
        cls,
        base_url: HttpUrl,
    ) -> HttpUrl:
        """Reject credentials and suffixes before constructing provider URLs."""
        has_credentials = bool(base_url.username or base_url.password)
        has_suffix = base_url.query is not None or base_url.fragment is not None
        if has_credentials or has_suffix:
            msg = (
                "LEGACY_TRANSCRIPTION_BASE_URL must not contain credentials, "
                "a query, or a fragment"
            )
            raise ValueError(msg)
        return base_url

    @field_validator("legacy_transcription_api_key")
    @classmethod
    def validate_legacy_transcription_api_key(
        cls,
        api_key: SecretStr,
    ) -> SecretStr:
        """Reject an empty, padded or control-character-bearing API key."""
        secret_value = api_key.get_secret_value()
        if (
            not secret_value
            or secret_value != secret_value.strip()
            or any(not character.isprintable() for character in secret_value)
        ):
            msg = (
                "LEGACY_TRANSCRIPTION_API_KEY must be non-empty, trimmed, "
                "and contain no control characters"
            )
            raise ValueError(msg)
        return api_key

    @field_validator("glpi_base_url")
    @classmethod
    def validate_glpi_base_url(cls, base_url: HttpUrl) -> HttpUrl:
        """Reject URL components that would make GLPI endpoints ambiguous."""
        has_credentials = bool(base_url.username or base_url.password)
        has_suffix = base_url.query is not None or base_url.fragment is not None
        if has_credentials or has_suffix:
            msg = "GLPI_BASE_URL must not contain credentials, a query, or a fragment"
            raise ValueError(msg)
        return base_url

    @field_validator("glpi_username", "glpi_client_id", "glpi_ticket_title")
    @classmethod
    def validate_glpi_printable_text(cls, value: str) -> str:
        """Reject empty, padded or control-character-bearing GLPI values."""
        if (
            not value
            or value != value.strip()
            or any(not character.isprintable() for character in value)
        ):
            msg = "GLPI text values must be non-empty, trimmed, and printable"
            raise ValueError(msg)
        return value

    @field_validator("glpi_password", "glpi_client_secret")
    @classmethod
    def validate_glpi_secret(cls, secret: SecretStr) -> SecretStr:
        """Reject empty, padded or control-character-bearing GLPI secrets."""
        secret_value = secret.get_secret_value()
        if (
            not secret_value
            or secret_value != secret_value.strip()
            or any(not character.isprintable() for character in secret_value)
        ):
            msg = "GLPI secrets must be non-empty, trimmed, and printable"
            raise ValueError(msg)
        return secret

    @model_validator(mode="after")
    def validate_schedule_ranges(self) -> Self:
        """Keep retry and retention schedules internally consistent."""
        if self.worker_retry_initial_seconds > self.worker_retry_max_seconds:
            raise ValueError(
                "WORKER_RETRY_INITIAL_SECONDS must not exceed WORKER_RETRY_MAX_SECONDS"
            )
        if self.retention_check_interval_hours >= self.completed_retention_days * 24:
            raise ValueError(
                "RETENTION_CHECK_INTERVAL_HOURS must precede the completed retention "
                "deadline"
            )
        return self
