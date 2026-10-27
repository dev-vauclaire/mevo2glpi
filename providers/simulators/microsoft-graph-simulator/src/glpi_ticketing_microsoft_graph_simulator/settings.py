"""Typed, secret-safe configuration for the Microsoft Graph simulator."""

from ipaddress import IPv4Address, IPv6Address
from uuid import UUID

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class MicrosoftGraphSimulatorSettings(BaseSettings):
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

    microsoft_graph_simulator_host: IPv4Address | IPv6Address = IPv4Address("0.0.0.0")
    microsoft_graph_simulator_port: int = Field(default=8080, ge=1, le=65_535)
    microsoft_graph_simulator_mailbox_id: UUID
    microsoft_graph_simulator_admin_token: SecretStr = Field(
        repr=False,
        min_length=16,
    )

    @field_validator("microsoft_graph_simulator_port", mode="before")
    @classmethod
    def reject_boolean_port(cls, value: object) -> object:
        """Do not accept booleans through Pydantic's integer coercion."""
        if isinstance(value, bool):
            raise ValueError("MICROSOFT_GRAPH_SIMULATOR_PORT cannot be a boolean")
        return value

    @field_validator("microsoft_graph_simulator_admin_token", mode="before")
    @classmethod
    def reject_documentation_placeholder(cls, value: object) -> object:
        """Fail closed when the documented placeholder was not replaced."""
        raw_value = value.get_secret_value() if isinstance(value, SecretStr) else value
        if isinstance(raw_value, str) and "CHANGE_ME" in raw_value.upper():
            raise ValueError("MICROSOFT_GRAPH_SIMULATOR_ADMIN_TOKEN must be replaced")
        return value


__all__ = ["MicrosoftGraphSimulatorSettings"]
