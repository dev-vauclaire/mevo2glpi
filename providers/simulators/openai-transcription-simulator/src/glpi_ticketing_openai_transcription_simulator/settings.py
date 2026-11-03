"""Typed, secret-safe configuration for the OpenAI-compatible simulator."""

from ipaddress import IPv4Address, IPv6Address

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class OpenAiTranscriptionSimulatorSettings(BaseSettings):
    """Configuration required by the isolated simulator process."""

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

    openai_transcription_simulator_host: IPv4Address | IPv6Address = IPv4Address(
        "0.0.0.0"
    )
    openai_transcription_simulator_port: int = Field(
        default=8080,
        ge=1,
        le=65_535,
    )
    openai_transcription_simulator_api_key: SecretStr = Field(
        repr=False,
        min_length=16,
    )
    openai_transcription_simulator_max_upload_bytes: int = Field(
        default=25 * 1024 * 1024,
        ge=1,
        le=25 * 1024 * 1024,
    )

    @field_validator(
        "openai_transcription_simulator_port",
        "openai_transcription_simulator_max_upload_bytes",
        mode="before",
    )
    @classmethod
    def reject_boolean_integers(cls, value: object) -> object:
        """Do not accept booleans through Pydantic's integer coercion."""
        if isinstance(value, bool):
            raise ValueError("integer configuration values cannot be booleans")
        return value

    @field_validator("openai_transcription_simulator_api_key", mode="before")
    @classmethod
    def reject_documentation_placeholder(cls, value: object) -> object:
        """Fail closed when the documented placeholder was not replaced."""
        raw_value = value.get_secret_value() if isinstance(value, SecretStr) else value
        if isinstance(raw_value, str) and "CHANGE_ME" in raw_value.upper():
            raise ValueError("OPENAI_TRANSCRIPTION_SIMULATOR_API_KEY must be replaced")
        return value


__all__ = ["OpenAiTranscriptionSimulatorSettings"]
