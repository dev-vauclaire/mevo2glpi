"""Typed configuration owned by the web application."""

from ipaddress import ip_address
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
    web_host: str = Field(default="127.0.0.1", min_length=1)
    web_port: int = Field(default=8000, ge=1, le=65535)
    web_trust_proxy_headers: bool = False
    max_upload_bytes: int = Field(default=DEFAULT_MAX_UPLOAD_BYTES, gt=0)

    @field_validator("web_port", "max_upload_bytes", mode="before")
    @classmethod
    def reject_boolean_integers(cls, value: object) -> object:
        """Reject booleans instead of silently treating them as integers."""
        if isinstance(value, bool):
            raise ValueError("Integer settings cannot be booleans")
        return value

    @field_validator("web_host")
    @classmethod
    def validate_web_host(cls, web_host: str) -> str:
        """Require an explicit IPv4 or IPv6 bind address."""
        try:
            return str(ip_address(web_host))
        except ValueError:
            raise ValueError("WEB_HOST must be an IPv4 or IPv6 address") from None

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
