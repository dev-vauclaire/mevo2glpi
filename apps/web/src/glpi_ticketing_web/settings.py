"""Typed configuration owned by the web application."""

from ipaddress import ip_address
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

DEFAULT_MAX_UPLOAD_BYTES = 25 * 1024 * 1024


class WebSettings(BaseSettings):
    """Configuration required by the persistent web application."""

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

    audio_root: Path = Path("/data/audios")
    database_url: SecretStr
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

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, database_url: SecretStr) -> SecretStr:
        """Require the synchronous PostgreSQL driver without exposing the URL."""
        raw_url = database_url.get_secret_value()
        try:
            parsed_url = make_url(raw_url)
        except ArgumentError:
            raise ValueError("DATABASE_URL must be a valid SQLAlchemy URL") from None
        if parsed_url.drivername != "postgresql+psycopg":
            raise ValueError("DATABASE_URL must use the postgresql+psycopg driver")
        return database_url
