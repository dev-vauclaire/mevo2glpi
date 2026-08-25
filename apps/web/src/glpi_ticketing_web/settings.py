"""Typed configuration owned by the web application."""

from pathlib import Path
from typing import Self

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_MAX_UPLOAD_BYTES = 25 * 1024 * 1024


class WebSettings(BaseSettings):
    """Configuration limited to the paths and policy required by the web app."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="",
        case_sensitive=False,
        env_ignore_empty=True,
        extra="ignore",
        frozen=True,
    )

    audio_pending_dir: Path = Path("/data/audio-pending")
    audio_completed_dir: Path = Path("/data/audio-completed")
    max_upload_bytes: int = Field(default=DEFAULT_MAX_UPLOAD_BYTES, gt=0)

    @field_validator("max_upload_bytes", mode="before")
    @classmethod
    def reject_boolean_upload_limit(cls, max_upload_bytes: object) -> object:
        """Reject booleans instead of silently treating them as integers."""
        if isinstance(max_upload_bytes, bool):
            raise ValueError("MAX_UPLOAD_BYTES must be a positive integer")
        return max_upload_bytes

    @model_validator(mode="after")
    def validate_audio_directories_are_distinct(self) -> Self:
        """Prevent the upload process from writing into completed storage."""
        pending_directory = self.audio_pending_dir.expanduser().resolve(strict=False)
        completed_directory = self.audio_completed_dir.expanduser().resolve(
            strict=False,
        )
        if pending_directory == completed_directory:
            raise ValueError("Web audio directories must be distinct")
        return self
