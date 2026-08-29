"""Tests for the persistence-independent worker health policy."""

from __future__ import annotations

import errno
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path

import pytest

import glpi_ticketing_common.runtime_healthcheck as healthcheck_module
from glpi_ticketing_common.runtime_healthcheck import (
    AudioAccessMode,
    HealthcheckCode,
    HealthcheckConfigurationError,
    HealthcheckFailure,
    HealthcheckSettings,
    RuntimeHeartbeat,
    probe_audio_root,
    probe_worker_health,
    run_healthcheck,
)

NOW = datetime(2026, 8, 29, 12, tzinfo=UTC)
DATABASE_URL = "postgresql+psycopg://worker:secret@database/jobs"


def _settings(tmp_path: Path, *, stale_after: float = 45.0) -> HealthcheckSettings:
    return HealthcheckSettings(
        database_url=DATABASE_URL,
        audio_root=tmp_path,
        heartbeat_interval_seconds=10.0,
        stale_after_seconds=stale_after,
    )


def _heartbeat(
    heartbeat_at: datetime,
    *,
    state: str = "running",
) -> RuntimeHeartbeat:
    return RuntimeHeartbeat(state=state, last_heartbeat_at=heartbeat_at)


def test_settings_load_defaults_without_exposing_database_secret(
    tmp_path: Path,
) -> None:
    settings = HealthcheckSettings.from_environment(
        {"DATABASE_URL": DATABASE_URL, "AUDIO_ROOT": str(tmp_path)}
    )

    assert settings.database_connect_timeout_seconds == 5
    assert settings.heartbeat_interval_seconds == 10.0
    assert settings.stale_after_seconds == 45.0
    assert "secret" not in repr(settings)


@pytest.mark.parametrize("stale_after", [10, 9, 0, float("inf"), float("nan")])
def test_stale_threshold_must_be_finite_and_exceed_heartbeat(
    tmp_path: Path,
    stale_after: float,
) -> None:
    with pytest.raises(HealthcheckConfigurationError):
        _settings(tmp_path, stale_after=stale_after)


@pytest.mark.parametrize(
    ("environment", "expected_fragment"),
    [
        ({"DATABASE_URL": ""}, "database URL"),
        (
            {
                "DATABASE_URL": DATABASE_URL,
                "WORKER_HEARTBEAT_INTERVAL_SECONDS": "inf",
            },
            "heartbeat interval",
        ),
        (
            {
                "DATABASE_URL": DATABASE_URL,
                "WORKER_HEARTBEAT_INTERVAL_SECONDS": "45",
                "WORKER_HEALTHCHECK_STALE_AFTER_SECONDS": "45",
            },
            "must exceed",
        ),
    ],
)
def test_environment_validation_is_strict_and_sanitized(
    environment: dict[str, str],
    expected_fragment: str,
) -> None:
    with pytest.raises(
        HealthcheckConfigurationError,
        match=expected_fragment,
    ) as captured_error:
        HealthcheckSettings.from_environment(environment)

    assert DATABASE_URL not in str(captured_error.value)


def test_heartbeat_exactly_at_threshold_is_healthy(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    probe_worker_health(
        settings,
        heartbeat_reader=lambda _settings: _heartbeat(
            NOW - timedelta(seconds=settings.stale_after_seconds)
        ),
        audio_access_mode=AudioAccessMode.READ_WRITE,
        now=NOW,
    )

    assert list(tmp_path.iterdir()) == []


def test_heartbeat_over_threshold_is_stale(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    with pytest.raises(HealthcheckFailure) as captured_error:
        probe_worker_health(
            settings,
            heartbeat_reader=lambda _settings: _heartbeat(
                NOW - timedelta(seconds=settings.stale_after_seconds + 0.001)
            ),
            audio_access_mode=AudioAccessMode.READ_WRITE,
            now=NOW,
        )

    assert captured_error.value.code is HealthcheckCode.HEARTBEAT_STALE


@pytest.mark.parametrize(
    ("heartbeat", "expected_code"),
    [
        (None, HealthcheckCode.HEARTBEAT_MISSING),
        (_heartbeat(NOW, state="stopped"), HealthcheckCode.HEARTBEAT_STOPPED),
        (_heartbeat(NOW, state="unknown"), HealthcheckCode.HEARTBEAT_INVALID),
        (
            _heartbeat(NOW + timedelta(microseconds=1)),
            HealthcheckCode.HEARTBEAT_INVALID,
        ),
        (_heartbeat(datetime(2026, 8, 29, 12)), HealthcheckCode.HEARTBEAT_INVALID),
    ],
)
def test_invalid_heartbeat_is_rejected_before_storage_probe(
    tmp_path: Path,
    heartbeat: RuntimeHeartbeat | None,
    expected_code: HealthcheckCode,
) -> None:
    with pytest.raises(HealthcheckFailure) as captured_error:
        probe_worker_health(
            _settings(tmp_path),
            heartbeat_reader=lambda _settings: heartbeat,
            audio_access_mode=AudioAccessMode.READ_WRITE,
            now=NOW,
        )

    assert captured_error.value.code is expected_code
    assert list(tmp_path.iterdir()) == []


def test_read_only_probe_requires_readable_but_non_writable_mount(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_write(*_args: object, **_kwargs: object) -> int:
        raise OSError(errno.EROFS, "synthetic read-only volume")

    monkeypatch.setattr(healthcheck_module.os, "open", reject_write)

    probe_audio_root(tmp_path, AudioAccessMode.READ_ONLY)


def test_read_only_probe_rejects_writable_mount_and_cleans_probe(
    tmp_path: Path,
) -> None:
    with pytest.raises(HealthcheckFailure) as captured_error:
        probe_audio_root(tmp_path, AudioAccessMode.READ_ONLY)

    assert captured_error.value.code is HealthcheckCode.AUDIO_PERMISSIONS_INVALID
    assert list(tmp_path.iterdir()) == []


def test_read_write_probe_creates_and_removes_its_probe(tmp_path: Path) -> None:
    probe_audio_root(tmp_path, AudioAccessMode.READ_WRITE)

    assert list(tmp_path.iterdir()) == []


def test_read_write_probe_rejects_read_only_mount(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_write(*_args: object, **_kwargs: object) -> int:
        raise PermissionError(errno.EACCES, "synthetic read-only volume")

    monkeypatch.setattr(healthcheck_module.os, "open", reject_write)

    with pytest.raises(HealthcheckFailure) as captured_error:
        probe_audio_root(tmp_path, AudioAccessMode.READ_WRITE)

    assert captured_error.value.code is HealthcheckCode.AUDIO_UNAVAILABLE


def test_cli_emits_only_stable_code_when_probe_contains_secret(tmp_path: Path) -> None:
    stderr = StringIO()

    def fail_with_secret(_settings: HealthcheckSettings) -> None:
        raise RuntimeError(DATABASE_URL)

    exit_code = run_healthcheck(
        service_label="synthetic worker",
        settings_factory=lambda: _settings(tmp_path),
        health_probe=fail_with_secret,
        stderr=stderr,
    )

    assert exit_code == 1
    assert stderr.getvalue().strip() == (
        "synthetic worker healthcheck failed code=unexpected_error"
    )
    assert DATABASE_URL not in stderr.getvalue()


def test_cli_reports_expected_failure_code(tmp_path: Path) -> None:
    stderr = StringIO()

    def fail(_settings: HealthcheckSettings) -> None:
        raise HealthcheckFailure(HealthcheckCode.DATABASE_UNAVAILABLE)

    exit_code = run_healthcheck(
        service_label="synthetic worker",
        settings_factory=lambda: _settings(tmp_path),
        health_probe=fail,
        stderr=stderr,
    )

    assert exit_code == 1
    assert "code=database_unavailable" in stderr.getvalue()
