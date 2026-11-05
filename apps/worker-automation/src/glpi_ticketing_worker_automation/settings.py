"""Typed configuration for the automation worker."""

from enum import StrEnum
from pathlib import Path
from typing import Self

from pydantic import Field, HttpUrl, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from glpi_ticketing_common import DeploymentEnvironment, ReleaseIdentity
from glpi_ticketing_glpi import TicketLevel, TicketType

from .transcription import TranscriptionProvider


class WorkerLogLevel(StrEnum):
    """Supported application log levels for the automation worker."""

    DEBUG = "DEBUG"
    INFO = "INFO"


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

    app_environment: DeploymentEnvironment = DeploymentEnvironment.DEVELOPMENT
    app_release: str = "development"
    app_commit_sha: str = "unknown"
    public_base_url: HttpUrl
    database_url: SecretStr = Field(repr=False)
    audio_root: Path = Path("/data/audios")
    originating_phone_number_key_path: Path
    worker_log_level: WorkerLogLevel = WorkerLogLevel.INFO
    database_connect_timeout_seconds: int = Field(default=5, gt=0)
    worker_idle_interval_seconds: float = Field(
        default=1.0,
        gt=0,
        allow_inf_nan=False,
    )
    worker_heartbeat_interval_seconds: float = Field(
        default=10.0,
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
    worker_max_consecutive_retries: int = Field(default=3, gt=0)
    worker_system_probe_interval_seconds: float = Field(
        default=60.0,
        gt=0,
        allow_inf_nan=False,
    )
    completed_retention_days: int = Field(default=90, gt=0)
    failed_retention_days: int = Field(default=30, gt=0)
    blocked_retention_days: int = Field(default=90, gt=0)
    retention_check_interval_hours: int = Field(default=6, gt=0)

    transcription_provider: TranscriptionProvider = TranscriptionProvider.LEGACY_POLLING
    legacy_transcription_base_url: HttpUrl | None = None
    legacy_transcription_api_key: SecretStr | None = Field(default=None, repr=False)
    openai_compatible_transcription_base_url: HttpUrl | None = None
    openai_compatible_transcription_api_key: SecretStr | None = Field(
        default=None,
        repr=False,
    )
    openai_compatible_transcription_model: str | None = None
    openai_compatible_transcription_language: str = "fr"
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
    glpi_ticket_type: TicketType = TicketType.INCIDENT
    glpi_ticket_request_source_id: int = Field(gt=0)
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

    @field_validator("originating_phone_number_key_path")
    @classmethod
    def validate_originating_phone_number_key_path(cls, path: Path) -> Path:
        """Require a readable file without inspecting or exposing its contents."""
        if not path.is_file():
            raise ValueError("ORIGINATING_PHONE_NUMBER_KEY_PATH must be a file")
        try:
            with path.open("rb"):
                pass
        except OSError:
            raise ValueError(
                "ORIGINATING_PHONE_NUMBER_KEY_PATH must be readable"
            ) from None
        return path

    @field_validator("worker_log_level", mode="before")
    @classmethod
    def normalize_worker_log_level(cls, value: object) -> object:
        """Accept conventional case-insensitive logging level names."""
        if isinstance(value, str):
            return value.strip().upper()
        return value

    @field_validator(
        "database_connect_timeout_seconds",
        "worker_max_consecutive_retries",
        "completed_retention_days",
        "failed_retention_days",
        "blocked_retention_days",
        "retention_check_interval_hours",
        "glpi_ticket_request_source_id",
        mode="before",
    )
    @classmethod
    def reject_boolean_integer_settings(cls, value: object) -> object:
        """Reject booleans instead of silently treating them as integers."""
        if isinstance(value, bool):
            raise ValueError("Integer settings cannot be booleans")
        return value

    @field_validator("worker_heartbeat_interval_seconds", mode="before")
    @classmethod
    def reject_boolean_heartbeat_interval(cls, value: object) -> object:
        """Reject booleans instead of coercing them to a heartbeat duration."""
        if isinstance(value, bool):
            raise ValueError("WORKER_HEARTBEAT_INTERVAL_SECONDS cannot be a boolean")
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
        base_url: HttpUrl | None,
    ) -> HttpUrl | None:
        """Reject credentials and suffixes before constructing provider URLs."""
        if base_url is None:
            return None
        has_credentials = bool(base_url.username or base_url.password)
        has_suffix = base_url.query is not None or base_url.fragment is not None
        if has_credentials or has_suffix:
            msg = (
                "LEGACY_TRANSCRIPTION_BASE_URL must not contain credentials, "
                "a query, or a fragment"
            )
            raise ValueError(msg)
        return base_url

    @field_validator("openai_compatible_transcription_base_url")
    @classmethod
    def validate_openai_compatible_transcription_base_url(
        cls,
        base_url: HttpUrl | None,
    ) -> HttpUrl | None:
        """Reject ambiguous URL components on the synchronous provider."""
        if base_url is None:
            return None
        has_credentials = bool(base_url.username or base_url.password)
        has_suffix = base_url.query is not None or base_url.fragment is not None
        if has_credentials or has_suffix:
            msg = (
                "OPENAI_COMPATIBLE_TRANSCRIPTION_BASE_URL must not contain "
                "credentials, a query, or a fragment"
            )
            raise ValueError(msg)
        return base_url

    @field_validator(
        "legacy_transcription_api_key",
        "openai_compatible_transcription_api_key",
    )
    @classmethod
    def validate_transcription_api_key(
        cls,
        api_key: SecretStr | None,
    ) -> SecretStr | None:
        """Reject an empty, padded or control-character-bearing API key."""
        if api_key is None:
            return None
        secret_value = api_key.get_secret_value()
        if (
            not secret_value
            or secret_value != secret_value.strip()
            or any(not character.isprintable() for character in secret_value)
        ):
            msg = (
                "Transcription API keys must be non-empty, trimmed, and contain "
                "no control characters"
            )
            raise ValueError(msg)
        return api_key

    @field_validator(
        "openai_compatible_transcription_model",
        "openai_compatible_transcription_language",
    )
    @classmethod
    def validate_openai_compatible_text(cls, value: str | None) -> str | None:
        """Reject padded or control-character-bearing provider parameters."""
        if value is None:
            return None
        if (
            not value
            or value != value.strip()
            or any(not character.isprintable() for character in value)
        ):
            raise ValueError(
                "OpenAI-compatible transcription values must be non-empty, "
                "trimmed and printable"
            )
        return value

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

    @model_validator(mode="after")
    def validate_transcription_provider_configuration(self) -> Self:
        """Require only the credentials used by the selected provider."""
        if self.transcription_provider is TranscriptionProvider.LEGACY_POLLING:
            if (
                self.legacy_transcription_base_url is None
                or self.legacy_transcription_api_key is None
            ):
                raise ValueError(
                    "The selected legacy polling transcription provider requires "
                    "LEGACY_TRANSCRIPTION_BASE_URL and "
                    "LEGACY_TRANSCRIPTION_API_KEY"
                )
            return self

        if self.transcription_provider is TranscriptionProvider.OPENAI_COMPATIBLE:
            if (
                self.openai_compatible_transcription_base_url is None
                or self.openai_compatible_transcription_api_key is None
                or self.openai_compatible_transcription_model is None
            ):
                raise ValueError(
                    "The selected OpenAI-compatible transcription provider "
                    "requires its base URL, API key and model"
                )
            return self

        raise ValueError("Unsupported transcription provider")

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
