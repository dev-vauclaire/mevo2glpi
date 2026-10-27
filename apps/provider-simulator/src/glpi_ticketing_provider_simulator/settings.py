"""Typed, secret-safe configuration for the staging provider simulator."""

from ipaddress import IPv4Address, IPv6Address
from uuid import UUID

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ProviderSimulatorSettings(BaseSettings):
    """Configuration required by the staging-only HTTP process."""

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

    provider_simulator_host: IPv4Address | IPv6Address = IPv4Address("0.0.0.0")
    provider_simulator_port: int = Field(default=8080, ge=1, le=65_535)
    provider_simulator_mailbox_id: UUID
    provider_simulator_admin_token: SecretStr = Field(repr=False, min_length=16)
    provider_simulator_transcription_api_key: SecretStr = Field(
        repr=False,
        min_length=16,
    )

    @field_validator("provider_simulator_port", mode="before")
    @classmethod
    def reject_boolean_port(cls, value: object) -> object:
        """Do not accept booleans through Pydantic's integer coercion."""
        if isinstance(value, bool):
            raise ValueError("PROVIDER_SIMULATOR_PORT cannot be a boolean")
        return value


__all__ = ["ProviderSimulatorSettings"]
