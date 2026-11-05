"""Typed configuration for Microsoft 365 mail ingestion."""

import math
from enum import StrEnum
from pathlib import Path
from typing import Self
from uuid import UUID

from pydantic import Field, HttpUrl, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from glpi_ticketing_common import DeploymentEnvironment, ReleaseIdentity

MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
OFFICIAL_GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"


class PublisherGraphAuthMode(StrEnum):
    """Explicit Graph authentication backends supported by the publisher."""

    ENTRA_CERTIFICATE = "entra_certificate"
    SYNTHETIC = "synthetic"


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

    app_environment: DeploymentEnvironment = DeploymentEnvironment.DEVELOPMENT
    app_release: str = "development"
    app_commit_sha: str = "unknown"
    publisher_graph_auth_mode: PublisherGraphAuthMode = (
        PublisherGraphAuthMode.ENTRA_CERTIFICATE
    )
    publisher_graph_base_url: HttpUrl = HttpUrl(OFFICIAL_GRAPH_BASE_URL)
    publisher_entra_tenant_id: UUID | None = None
    publisher_entra_client_id: UUID | None = None
    publisher_entra_private_key_pem_path: Path | None = None
    publisher_entra_certificate_pem_path: Path | None = None
    publisher_entra_private_key_passphrase: SecretStr | None = Field(
        default=None,
        repr=False,
    )
    publisher_mailbox_id: UUID
    database_url: SecretStr = Field(repr=False)
    audio_root: Path = Path("/data/audios")
    originating_phone_number_key_path: Path
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

    @field_validator("originating_phone_number_key_path")
    @classmethod
    def validate_originating_phone_number_key_path(cls, path: Path) -> Path:
        """Require a readable secret file without inspecting or logging its value."""
        return _validate_originating_phone_number_key_file(path)

    @field_validator("publisher_graph_auth_mode", "publisher_log_level", mode="before")
    @classmethod
    def normalize_enum_setting(cls, value: object) -> object:
        """Accept conventional case-insensitive enum values."""
        if isinstance(value, str):
            normalized = value.strip()
            if normalized.lower() in {mode.value for mode in PublisherGraphAuthMode}:
                return normalized.lower()
            return normalized.upper()
        return value

    @field_validator("publisher_graph_base_url")
    @classmethod
    def validate_graph_base_url(cls, url: HttpUrl) -> HttpUrl:
        """Require the pinned Graph v1.0 root without embedded request data."""
        if url.username or url.password or url.query or url.fragment:
            raise ValueError(
                "PUBLISHER_GRAPH_BASE_URL must not contain credentials, "
                "a query, or a fragment"
            )
        if url.path.rstrip("/") != "/v1.0":
            raise ValueError("PUBLISHER_GRAPH_BASE_URL must target Graph v1.0")
        return url

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
        """Keep retry and authentication settings internally coherent."""
        if self.publisher_retry_initial_seconds > self.publisher_retry_max_seconds:
            raise ValueError(
                "PUBLISHER_RETRY_INITIAL_SECONDS must not exceed "
                "PUBLISHER_RETRY_MAX_SECONDS"
            )

        entra_values = (
            self.publisher_entra_tenant_id,
            self.publisher_entra_client_id,
            self.publisher_entra_private_key_pem_path,
            self.publisher_entra_certificate_pem_path,
            self.publisher_entra_private_key_passphrase,
        )
        if self.publisher_graph_auth_mode is PublisherGraphAuthMode.SYNTHETIC:
            if _is_official_graph_url(self.publisher_graph_base_url):
                raise ValueError(
                    "Synthetic Graph authentication requires a non-Microsoft URL"
                )
            if any(value is not None for value in entra_values):
                raise ValueError(
                    "Synthetic Graph authentication must not receive Entra credentials"
                )
            return self

        if not _is_official_graph_url(self.publisher_graph_base_url):
            raise ValueError(
                "Certificate authentication requires the official Microsoft Graph URL"
            )
        if any(value is None for value in entra_values[:4]):
            raise ValueError(
                "Certificate authentication requires tenant, client and PEM files"
            )
        assert self.publisher_entra_private_key_pem_path is not None
        assert self.publisher_entra_certificate_pem_path is not None
        _validate_readable_file(
            self.publisher_entra_private_key_pem_path,
            variable_name="PUBLISHER_ENTRA_PRIVATE_KEY_PEM_PATH",
        )
        _validate_readable_file(
            self.publisher_entra_certificate_pem_path,
            variable_name="PUBLISHER_ENTRA_CERTIFICATE_PEM_PATH",
        )
        if (
            self.publisher_entra_private_key_pem_path.resolve()
            == self.publisher_entra_certificate_pem_path.resolve()
        ):
            raise ValueError("The private key and certificate must use distinct files")
        return self

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


def _validate_readable_file(path: Path, *, variable_name: str) -> Path:
    if not path.is_file():
        raise ValueError(f"{variable_name} must be a file")
    try:
        with path.open("rb"):
            pass
    except OSError:
        raise ValueError(f"{variable_name} must be readable") from None
    return path


def _validate_originating_phone_number_key_file(path: Path) -> Path:
    return _validate_readable_file(
        path,
        variable_name="ORIGINATING_PHONE_NUMBER_KEY_PATH",
    )


def _is_official_graph_url(url: HttpUrl) -> bool:
    return (
        url.scheme == "https"
        and url.host == "graph.microsoft.com"
        and url.path.rstrip("/") == "/v1.0"
        and not url.username
        and not url.password
        and not url.query
        and not url.fragment
    )


__all__ = [
    "MAX_ATTACHMENT_BYTES",
    "OFFICIAL_GRAPH_BASE_URL",
    "PublisherGraphAuthMode",
    "PublisherLogLevel",
    "PublisherSettings",
]
