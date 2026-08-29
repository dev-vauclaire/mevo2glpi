"""Dependency-free health policy shared by singleton worker containers."""

from __future__ import annotations

import errno
import math
import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from secrets import token_hex
from typing import Protocol, TextIO

DEFAULT_AUDIO_ROOT = Path("/data/audios")
DEFAULT_CONNECT_TIMEOUT_SECONDS = 5
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 10.0
DEFAULT_STALE_AFTER_SECONDS = 45.0


class AudioAccessMode(StrEnum):
    """Filesystem permissions expected by one worker role."""

    READ_ONLY = "read_only"
    READ_WRITE = "read_write"


class HealthcheckCode(StrEnum):
    """Stable, sanitized diagnostics emitted to the container runtime."""

    INVALID_CONFIGURATION = "invalid_configuration"
    DATABASE_UNAVAILABLE = "database_unavailable"
    HEARTBEAT_MISSING = "heartbeat_missing"
    HEARTBEAT_STOPPED = "heartbeat_stopped"
    HEARTBEAT_INVALID = "heartbeat_invalid"
    HEARTBEAT_STALE = "heartbeat_stale"
    AUDIO_UNAVAILABLE = "audio_unavailable"
    AUDIO_PERMISSIONS_INVALID = "audio_permissions_invalid"
    UNEXPECTED_ERROR = "unexpected_error"


class HealthcheckConfigurationError(ValueError):
    """Reject unusable probe settings without echoing their values."""


class HealthcheckFailure(RuntimeError):
    """Report one expected probe failure through a non-sensitive code."""

    def __init__(self, code: HealthcheckCode) -> None:
        if not isinstance(code, HealthcheckCode):
            raise TypeError("code must be a HealthcheckCode")
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True, slots=True, kw_only=True)
class HealthcheckSettings:
    """Minimal environment required by a standalone worker health process."""

    database_url: str = field(repr=False)
    audio_root: Path = DEFAULT_AUDIO_ROOT
    database_connect_timeout_seconds: int = DEFAULT_CONNECT_TIMEOUT_SECONDS
    heartbeat_interval_seconds: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS
    stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS

    def __post_init__(self) -> None:
        if not isinstance(self.database_url, str) or not self.database_url:
            raise HealthcheckConfigurationError("database URL is required")
        if not isinstance(self.audio_root, Path):
            raise HealthcheckConfigurationError("audio root must be a path")
        if (
            isinstance(self.database_connect_timeout_seconds, bool)
            or not isinstance(self.database_connect_timeout_seconds, int)
            or self.database_connect_timeout_seconds <= 0
        ):
            raise HealthcheckConfigurationError("connect timeout must be positive")
        _require_positive_finite(
            self.heartbeat_interval_seconds,
            "heartbeat interval",
        )
        _require_positive_finite(self.stale_after_seconds, "stale threshold")
        if self.stale_after_seconds <= self.heartbeat_interval_seconds:
            raise HealthcheckConfigurationError(
                "stale threshold must exceed heartbeat interval"
            )

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> HealthcheckSettings:
        """Load probe-only settings without reading remote-service secrets."""
        values = os.environ if environ is None else environ
        database_url = values.get("DATABASE_URL", "")
        audio_root = values.get("AUDIO_ROOT", str(DEFAULT_AUDIO_ROOT))
        if not audio_root:
            raise HealthcheckConfigurationError("audio root is required")
        return cls(
            database_url=database_url,
            audio_root=Path(audio_root),
            database_connect_timeout_seconds=_parse_positive_integer(
                values.get(
                    "DATABASE_CONNECT_TIMEOUT_SECONDS",
                    str(DEFAULT_CONNECT_TIMEOUT_SECONDS),
                ),
                "database connect timeout",
            ),
            heartbeat_interval_seconds=_parse_positive_float(
                values.get(
                    "WORKER_HEARTBEAT_INTERVAL_SECONDS",
                    str(DEFAULT_HEARTBEAT_INTERVAL_SECONDS),
                ),
                "heartbeat interval",
            ),
            stale_after_seconds=_parse_positive_float(
                values.get(
                    "WORKER_HEALTHCHECK_STALE_AFTER_SECONDS",
                    str(DEFAULT_STALE_AFTER_SECONDS),
                ),
                "stale threshold",
            ),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class RuntimeHeartbeat:
    """Persistence-independent heartbeat projection used by the policy."""

    state: str
    last_heartbeat_at: datetime


class HeartbeatReader(Protocol):
    """Read the latest heartbeat through an application-owned adapter."""

    def __call__(
        self,
        settings: HealthcheckSettings,
    ) -> RuntimeHeartbeat | None: ...


def probe_worker_health(
    settings: HealthcheckSettings,
    *,
    heartbeat_reader: HeartbeatReader,
    audio_access_mode: AudioAccessMode,
    now: datetime | None = None,
) -> None:
    """Require a fresh live heartbeat and the role's exact storage access."""
    if not isinstance(settings, HealthcheckSettings):
        raise TypeError("settings must be HealthcheckSettings")
    if not callable(heartbeat_reader):
        raise TypeError("heartbeat_reader must be callable")
    if not isinstance(audio_access_mode, AudioAccessMode):
        raise TypeError("audio_access_mode must be an AudioAccessMode")

    snapshot = heartbeat_reader(settings)
    _require_fresh_heartbeat(snapshot, settings, now or datetime.now(UTC))
    probe_audio_root(settings.audio_root, audio_access_mode)


def _require_fresh_heartbeat(
    snapshot: RuntimeHeartbeat | None,
    settings: HealthcheckSettings,
    now: datetime,
) -> None:
    if snapshot is None:
        raise HealthcheckFailure(HealthcheckCode.HEARTBEAT_MISSING)
    if snapshot.state == "stopped":
        raise HealthcheckFailure(HealthcheckCode.HEARTBEAT_STOPPED)
    if snapshot.state not in {"starting", "running", "degraded", "blocked"}:
        raise HealthcheckFailure(HealthcheckCode.HEARTBEAT_INVALID)
    if now.tzinfo is None or now.utcoffset() is None:
        raise TypeError("now must be timezone-aware")
    heartbeat_at = snapshot.last_heartbeat_at
    if heartbeat_at.tzinfo is None or heartbeat_at.utcoffset() is None:
        raise HealthcheckFailure(HealthcheckCode.HEARTBEAT_INVALID)
    age_seconds = (now - heartbeat_at).total_seconds()
    if age_seconds < 0:
        raise HealthcheckFailure(HealthcheckCode.HEARTBEAT_INVALID)
    if age_seconds > settings.stale_after_seconds:
        raise HealthcheckFailure(HealthcheckCode.HEARTBEAT_STALE)


def probe_audio_root(audio_root: Path, access_mode: AudioAccessMode) -> None:
    """Prove readability and then the precise write capability of the mount."""
    if not isinstance(audio_root, Path):
        raise TypeError("audio_root must be a Path")
    if not isinstance(access_mode, AudioAccessMode):
        raise TypeError("access_mode must be an AudioAccessMode")

    try:
        if audio_root.is_symlink():
            raise HealthcheckFailure(HealthcheckCode.AUDIO_UNAVAILABLE)
        resolved = audio_root.resolve(strict=True)
        if not resolved.is_dir():
            raise HealthcheckFailure(HealthcheckCode.AUDIO_UNAVAILABLE)
        with os.scandir(resolved) as entries:
            next(entries, None)
    except HealthcheckFailure:
        raise
    except OSError:
        raise HealthcheckFailure(HealthcheckCode.AUDIO_UNAVAILABLE) from None

    probe_path = resolved / f".gta-health-{os.getpid()}-{token_hex(4)}"
    descriptor: int | None = None
    try:
        descriptor = os.open(
            probe_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except OSError as error:
        if access_mode is AudioAccessMode.READ_ONLY and error.errno in {
            errno.EACCES,
            errno.EPERM,
            errno.EROFS,
        }:
            return
        raise HealthcheckFailure(HealthcheckCode.AUDIO_UNAVAILABLE) from None

    try:
        os.close(descriptor)
        descriptor = None
        probe_path.unlink()
    except OSError:
        if descriptor is not None:
            os.close(descriptor)
        try:
            probe_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise HealthcheckFailure(HealthcheckCode.AUDIO_UNAVAILABLE) from None

    if access_mode is AudioAccessMode.READ_ONLY:
        raise HealthcheckFailure(HealthcheckCode.AUDIO_PERMISSIONS_INVALID)


def run_healthcheck(
    *,
    service_label: str,
    health_probe: Callable[[HealthcheckSettings], None],
    settings_factory: Callable[[], HealthcheckSettings] = (
        HealthcheckSettings.from_environment
    ),
    stderr: TextIO = sys.stderr,
) -> int:
    """Run one adapter with a bounded, secret-free process contract."""
    if not isinstance(service_label, str) or not service_label:
        raise TypeError("service_label must be a non-empty string")
    if not callable(health_probe):
        raise TypeError("health_probe must be callable")
    try:
        settings = settings_factory()
    except HealthcheckConfigurationError:
        return _report_failure(
            service_label,
            HealthcheckCode.INVALID_CONFIGURATION,
            stderr,
        )
    except Exception:
        return _report_failure(
            service_label,
            HealthcheckCode.UNEXPECTED_ERROR,
            stderr,
        )

    try:
        health_probe(settings)
    except HealthcheckFailure as error:
        return _report_failure(service_label, error.code, stderr)
    except Exception:
        return _report_failure(
            service_label,
            HealthcheckCode.UNEXPECTED_ERROR,
            stderr,
        )
    return 0


def _report_failure(
    service_label: str,
    code: HealthcheckCode,
    stderr: TextIO,
) -> int:
    print(f"{service_label} healthcheck failed code={code.value}", file=stderr)
    return 1


def _parse_positive_integer(value: str, label: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise HealthcheckConfigurationError(f"{label} must be an integer") from None
    if parsed <= 0:
        raise HealthcheckConfigurationError(f"{label} must be positive")
    return parsed


def _parse_positive_float(value: str, label: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise HealthcheckConfigurationError(f"{label} must be numeric") from None
    _require_positive_finite(parsed, label)
    return parsed


def _require_positive_finite(value: float, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HealthcheckConfigurationError(f"{label} must be numeric")
    if value <= 0 or not math.isfinite(value):
        raise HealthcheckConfigurationError(f"{label} must be positive and finite")


__all__ = [
    "AudioAccessMode",
    "HealthcheckCode",
    "HealthcheckConfigurationError",
    "HealthcheckFailure",
    "HealthcheckSettings",
    "HeartbeatReader",
    "RuntimeHeartbeat",
    "probe_audio_root",
    "probe_worker_health",
    "run_healthcheck",
]
