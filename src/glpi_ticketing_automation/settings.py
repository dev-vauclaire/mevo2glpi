"""Typed application configuration loaded from the environment."""

from pathlib import Path
from typing import Self

from pydantic import Field, HttpUrl, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings shared by the future web and worker processes."""

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
