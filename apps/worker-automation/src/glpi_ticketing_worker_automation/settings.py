"""Typed configuration for the automation worker."""

from pathlib import Path
from typing import Self

from pydantic import Field, HttpUrl, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class WorkerSettings(BaseSettings):
    """Configuration owned by the automation worker process."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="",
        case_sensitive=False,
        env_ignore_empty=True,
        extra="ignore",
        frozen=True,
    )

    public_base_url: HttpUrl

    audio_pending_dir: Path = Path("/data/audio-pending")
    audio_processing_dir: Path = Path("/data/audio-processing")
    audio_completed_dir: Path = Path("/data/audio-completed")
    audio_failed_dir: Path = Path("/data/audio-failed")

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

    glpi_password: SecretStr | None = Field(default=None, repr=False)
    glpi_client_secret: SecretStr | None = Field(default=None, repr=False)

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

    @model_validator(mode="after")
    def validate_audio_directories_are_distinct(self) -> Self:
        """Reject configurations that merge two audio lifecycle directories."""
        directories = (
            self.audio_pending_dir,
            self.audio_processing_dir,
            self.audio_completed_dir,
            self.audio_failed_dir,
        )
        normalized_directories = {
            directory.expanduser().resolve(strict=False) for directory in directories
        }

        if len(normalized_directories) != len(directories):
            msg = "Audio directories must be distinct"
            raise ValueError(msg)

        return self
