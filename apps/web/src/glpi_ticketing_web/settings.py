"""Typed configuration owned by the web application."""

import math
from ipaddress import ip_address
from pathlib import Path

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


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
    worker_heartbeat_interval_seconds: float = Field(default=10.0, gt=0)
    dashboard_worker_stale_after_seconds: float = Field(default=45.0, gt=0)
    dashboard_pending_alert_after_seconds: float = Field(default=900.0, gt=0)
    dashboard_retry_overdue_grace_seconds: float = Field(default=60.0, gt=0)
    dashboard_retention_deadline_grace_seconds: float = Field(default=120.0, gt=0)

    @field_validator("web_port", mode="before")
    @classmethod
    def reject_boolean_integers(cls, value: object) -> object:
        """Reject booleans instead of silently treating them as integers."""
        if isinstance(value, bool):
            raise ValueError("Integer settings cannot be booleans")
        return value

    @field_validator(
        "worker_heartbeat_interval_seconds",
        "dashboard_worker_stale_after_seconds",
        "dashboard_pending_alert_after_seconds",
        "dashboard_retry_overdue_grace_seconds",
        "dashboard_retention_deadline_grace_seconds",
        mode="before",
    )
    @classmethod
    def validate_duration_seconds(cls, value: object) -> object:
        """Require finite positive durations and reject boolean coercion."""
        if isinstance(value, bool):
            raise ValueError("Duration settings cannot be booleans")
        try:
            duration = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return value
        if not math.isfinite(duration):
            raise ValueError("Duration settings must be finite")
        return value

    @model_validator(mode="after")
    def validate_observability_thresholds(self) -> "WebSettings":
        """Keep stale detection above the expected heartbeat cadence."""
        if (
            self.dashboard_worker_stale_after_seconds
            <= self.worker_heartbeat_interval_seconds
        ):
            raise ValueError(
                "DASHBOARD_WORKER_STALE_AFTER_SECONDS must exceed "
                "WORKER_HEARTBEAT_INTERVAL_SECONDS"
            )
        return self

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
