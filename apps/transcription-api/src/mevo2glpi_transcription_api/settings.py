"""Validated, secret-safe runtime configuration for the transcription API."""

from __future__ import annotations

import math
from ipaddress import ip_address
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class TranscriptionSettings(BaseSettings):
    """CPU-only model, HTTP and input limits required by the service."""

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

    transcription_api_key: SecretStr = Field(min_length=16, max_length=4096)
    transcription_model_path: Path = Path("/models/parakeet-ultra-Q8_0.gguf")
    transcription_model_name: str = Field(
        default="parakeet-ultra",
        min_length=1,
        max_length=255,
    )
    transcription_cpu_threads: int = Field(default=2, ge=1, le=256)
    transcription_host: str = "127.0.0.1"
    transcription_port: int = Field(default=5092, ge=1, le=65535)
    transcription_log_level: Literal["critical", "error", "warning", "info"] = "info"
    transcription_max_upload_bytes: int = Field(
        default=25 * 1024 * 1024,
        ge=1,
        le=100 * 1024 * 1024,
    )
    transcription_max_audio_seconds: float = Field(default=600.0, gt=0, le=3600)
    transcription_ffmpeg_path: Path = Path("/usr/bin/ffmpeg")
    transcription_ffmpeg_timeout_seconds: float = Field(
        default=60.0,
        gt=0,
        le=600,
    )

    @field_validator(
        "transcription_cpu_threads",
        "transcription_port",
        "transcription_max_upload_bytes",
        mode="before",
    )
    @classmethod
    def reject_boolean_integers(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("integer settings cannot be booleans")
        return value

    @field_validator(
        "transcription_max_audio_seconds",
        "transcription_ffmpeg_timeout_seconds",
        mode="before",
    )
    @classmethod
    def require_finite_durations(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("duration settings cannot be booleans")
        try:
            numeric = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return value
        if not math.isfinite(numeric):
            raise ValueError("duration settings must be finite")
        return value

    @field_validator("transcription_host")
    @classmethod
    def require_ip_bind_address(cls, value: str) -> str:
        try:
            return str(ip_address(value))
        except ValueError:
            raise ValueError(
                "TRANSCRIPTION_HOST must be an IPv4 or IPv6 address"
            ) from None

    @field_validator(
        "transcription_model_path",
        "transcription_ffmpeg_path",
    )
    @classmethod
    def require_absolute_paths(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("runtime paths must be absolute")
        return value

    @field_validator("transcription_model_name")
    @classmethod
    def require_safe_model_name(cls, value: str) -> str:
        if value != value.strip() or any(not char.isprintable() for char in value):
            raise ValueError("model name must be trimmed and printable")
        return value


__all__ = ["TranscriptionSettings"]
