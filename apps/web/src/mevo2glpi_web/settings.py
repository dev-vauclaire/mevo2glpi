"""Typed configuration owned by the web application."""

import math
import re
from enum import StrEnum
from ipaddress import ip_address
from pathlib import Path
from typing import Self

from pydantic import Field, HttpUrl, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from mevo2glpi_common.release_identity import (
    DeploymentEnvironment,
    ReleaseIdentity,
)

_SAFE_OIDC_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")


class WebAuthMode(StrEnum):
    """Explicit authentication modes supported by the web application."""

    LOCAL = "local"
    OIDC = "oidc"


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
    web_auth_mode: WebAuthMode
    web_auth_session_secret: SecretStr
    web_auth_session_max_age_seconds: int = Field(default=1800, ge=300, le=86400)
    web_auth_cookie_secure: bool = True
    web_auth_required_role: str = "mevo2glpi-access"
    web_auth_roles_claim: str = "roles"
    web_auth_issuer_url: HttpUrl | None = None
    web_auth_client_id: str | None = None
    web_auth_client_secret: SecretStr | None = None
    web_auth_redirect_uri: HttpUrl | None = None
    worker_heartbeat_interval_seconds: float = Field(default=10.0, gt=0)
    dashboard_worker_stale_after_seconds: float = Field(default=45.0, gt=0)
    dashboard_pending_alert_after_seconds: float = Field(default=900.0, gt=0)
    dashboard_retry_overdue_grace_seconds: float = Field(default=60.0, gt=0)
    dashboard_retention_deadline_grace_seconds: float = Field(default=120.0, gt=0)
    app_environment: DeploymentEnvironment = DeploymentEnvironment.DEVELOPMENT
    app_release: str = "development"
    app_commit_sha: str = "unknown"

    @field_validator(
        "web_port",
        "web_auth_session_max_age_seconds",
        mode="before",
    )
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

    @field_validator("web_auth_session_secret")
    @classmethod
    def validate_session_secret(cls, secret: SecretStr) -> SecretStr:
        """Require enough entropy without ever echoing the configured value."""
        if len(secret.get_secret_value()) < 32:
            raise ValueError(
                "WEB_AUTH_SESSION_SECRET must contain at least 32 characters"
            )
        return secret

    @field_validator("web_auth_required_role", "web_auth_roles_claim")
    @classmethod
    def validate_oidc_name(cls, value: str) -> str:
        """Keep role and flat claim names bounded and unambiguous."""
        if not _SAFE_OIDC_NAME.fullmatch(value):
            raise ValueError("OIDC role and claim names must be safe identifiers")
        return value

    @field_validator("web_auth_client_id")
    @classmethod
    def validate_optional_client_id(cls, value: str | None) -> str | None:
        """Reject empty or control-character-bearing client identifiers."""
        if value is None:
            return None
        normalized = value.strip()
        if (
            not normalized
            or len(normalized) > 255
            or any(
                ord(character) < 32 or ord(character) == 127 for character in normalized
            )
        ):
            raise ValueError("WEB_AUTH_CLIENT_ID is invalid")
        return normalized

    @model_validator(mode="after")
    def validate_authentication_configuration(self) -> Self:
        """Fail closed when the selected authentication mode is incomplete."""
        if (
            self.app_environment is not DeploymentEnvironment.DEVELOPMENT
            and not self.web_auth_cookie_secure
        ):
            raise ValueError(
                "WEB_AUTH_COOKIE_SECURE must be enabled outside development"
            )

        oidc_values = {
            "WEB_AUTH_ISSUER_URL": self.web_auth_issuer_url,
            "WEB_AUTH_CLIENT_ID": self.web_auth_client_id,
            "WEB_AUTH_CLIENT_SECRET": self.web_auth_client_secret,
            "WEB_AUTH_REDIRECT_URI": self.web_auth_redirect_uri,
        }
        if self.web_auth_mode is WebAuthMode.OIDC:
            missing = tuple(
                name for name, value in oidc_values.items() if value is None
            )
            if missing:
                raise ValueError("OIDC authentication configuration is incomplete")
            if self.app_environment is not DeploymentEnvironment.DEVELOPMENT and (
                self.web_auth_issuer_url is None
                or self.web_auth_issuer_url.scheme != "https"
                or self.web_auth_redirect_uri is None
                or self.web_auth_redirect_uri.scheme != "https"
            ):
                raise ValueError("OIDC URLs must use HTTPS outside development")
        elif any(value is not None for value in oidc_values.values()):
            raise ValueError("OIDC settings must be unset when WEB_AUTH_MODE is local")
        return self

    @model_validator(mode="after")
    def validate_release_identity(self) -> "WebSettings":
        """Reject inconsistent deployment metadata at the configuration edge."""
        _ = self.release_identity
        return self

    @property
    def release_identity(self) -> ReleaseIdentity:
        """Return the validated non-sensitive deployment identity."""
        return ReleaseIdentity(
            environment=self.app_environment,
            release=self.app_release,
            commit_sha=self.app_commit_sha,
        )

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
