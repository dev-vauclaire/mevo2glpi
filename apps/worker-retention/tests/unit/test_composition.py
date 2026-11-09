"""Unit tests for retention-worker dependency composition."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import httpx
import pytest

import mevo2glpi_worker_retention.composition as composition_module
from mevo2glpi_worker_retention.composition import (
    RetentionWorkerConfigurationError,
    build_retention_worker,
)
from mevo2glpi_worker_retention.settings import RetentionWorkerSettings


def test_build_retention_worker_wires_policies_adapters_and_fencing(
    retention_settings_factory: Callable[..., RetentionWorkerSettings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = retention_settings_factory()
    http_client = httpx.Client()
    captured: dict[str, dict[str, object]] = {}
    lifecycle = object()
    rejected_lifecycle = object()
    rejected_retention = object()
    glpi_client = object()
    glpi_stage = object()
    audio_purger = object()
    worker = object()

    def capture(name: str, result: object) -> Callable[..., object]:
        def factory(*args: object, **kwargs: object) -> object:
            captured[name] = {"args": args, "kwargs": kwargs}
            return result

        return factory

    monkeypatch.setattr(
        composition_module,
        "RetentionJobLifecycleService",
        capture("lifecycle", lifecycle),
    )
    monkeypatch.setattr(
        composition_module,
        "RejectedIngestionRetentionLifecycleService",
        capture("rejected_lifecycle", rejected_lifecycle),
    )
    monkeypatch.setattr(
        composition_module,
        "RejectedIngestionRetentionService",
        capture("rejected_retention", rejected_retention),
    )
    monkeypatch.setattr(
        composition_module,
        "GlpiClient",
        capture("glpi_client", glpi_client),
    )
    monkeypatch.setattr(
        composition_module,
        "GlpiCheckStage",
        capture("glpi_stage", glpi_stage),
    )
    monkeypatch.setattr(
        composition_module,
        "CrashSafeAudioPurger",
        capture("audio_purger", audio_purger),
    )
    monkeypatch.setattr(
        composition_module,
        "RetentionWorker",
        capture("worker", worker),
    )

    def session_factory() -> object:
        return object()

    ownership_checks: list[str] = []

    def ownership_guard() -> None:
        ownership_checks.append("checked")

    started_jobs: list[UUID] = []

    try:
        result = build_retention_worker(
            settings,
            http_client=http_client,
            session_factory=cast(Any, session_factory),
            ownership_guard=ownership_guard,
            on_job_started=started_jobs.append,
        )
    finally:
        http_client.close()

    assert result is worker
    lifecycle_arguments = captured["lifecycle"]["kwargs"]
    assert lifecycle_arguments["session_factory"] is session_factory
    assert lifecycle_arguments["ownership_guard"] is ownership_guard
    check_policy = lifecycle_arguments["check_policy"]
    assert check_policy.check_interval.total_seconds() == 8 * 3600

    assert captured["rejected_lifecycle"]["kwargs"] == {
        "session_factory": session_factory,
        "ownership_guard": ownership_guard,
    }
    rejected_arguments = captured["rejected_retention"]["kwargs"]
    assert rejected_arguments["lifecycle"] is rejected_lifecycle
    assert rejected_arguments["mode"] is settings.rejected_ingestion_retention_mode
    assert rejected_arguments["retention_period"].total_seconds() == 90 * 86400
    assert rejected_arguments["scan_interval"].total_seconds() == 6 * 3600
    assert rejected_arguments["batch_size"] == 100

    glpi_arguments = captured["glpi_client"]["kwargs"]
    assert glpi_arguments == {
        "base_url": "https://glpi.example.test/prefix",
        "username": "synthetic-retention-worker",
        "password": settings.glpi_password,
        "client_id": "synthetic-client-id",
        "client_secret": settings.glpi_client_secret,
        "http_client": http_client,
        "timeout": 5.5,
        "ownership_guard": ownership_guard,
    }
    assert captured["glpi_stage"] == {
        "args": (glpi_client,),
        "kwargs": {"ownership_guard": ownership_guard},
    }
    assert captured["audio_purger"] == {
        "args": (settings.audio_root.resolve(),),
        "kwargs": {"ownership_guard": ownership_guard},
    }

    worker_arguments = captured["worker"]["kwargs"]
    assert worker_arguments["lifecycle"] is lifecycle
    assert worker_arguments["glpi_check_stage"] is glpi_stage
    assert worker_arguments["audio_purger"] is audio_purger
    assert worker_arguments["check_policy"] is check_policy
    assert worker_arguments["rejected_ingestion_retention"] is rejected_retention
    assert worker_arguments["poll_interval_seconds"] == 61.0
    assert worker_arguments["system_probe_interval_seconds"] == 47.0
    assert worker_arguments["on_job_started"] == started_jobs.append
    retry_policy = worker_arguments["retry_policy"]
    assert retry_policy.initial_seconds == 3.5
    assert retry_policy.maximum_seconds == 30.0


@pytest.mark.parametrize(
    ("argument_name", "invalid_value", "expected_message"),
    [
        ("settings", object(), "RetentionWorkerSettings"),
        ("http_client", object(), "httpx.Client"),
        ("session_factory", object(), "callable"),
        ("ownership_guard", object(), "callable"),
        ("on_job_started", object(), "callable"),
    ],
)
def test_build_retention_worker_rejects_invalid_composition_inputs(
    retention_settings_factory: Callable[..., RetentionWorkerSettings],
    argument_name: str,
    invalid_value: object,
    expected_message: str,
) -> None:
    settings = retention_settings_factory()
    values: dict[str, object] = {
        "settings": settings,
        "http_client": httpx.Client(),
        "session_factory": lambda: object(),
        "ownership_guard": lambda: None,
        "on_job_started": lambda _job_uuid: None,
    }
    values[argument_name] = invalid_value
    try:
        with pytest.raises(TypeError, match=expected_message):
            build_retention_worker(**cast(Any, values))
    finally:
        http_client = values["http_client"]
        if isinstance(http_client, httpx.Client):
            http_client.close()


def test_build_retention_worker_rejects_missing_audio_root(
    tmp_path: Path,
    retention_settings_factory: Callable[..., RetentionWorkerSettings],
) -> None:
    settings = retention_settings_factory(audio_root=tmp_path / "missing")

    with httpx.Client() as http_client:
        with pytest.raises(RetentionWorkerConfigurationError, match="must exist"):
            build_retention_worker(
                settings,
                http_client=http_client,
                session_factory=cast(Any, lambda: object()),
            )


def test_build_retention_worker_rejects_file_or_symlink_audio_root(
    tmp_path: Path,
    retention_settings_factory: Callable[..., RetentionWorkerSettings],
) -> None:
    audio_file = tmp_path / "audio-file"
    audio_file.write_bytes(b"audio")
    real_root = tmp_path / "real-root"
    real_root.mkdir()
    symlink_root = tmp_path / "linked-root"
    symlink_root.symlink_to(real_root, target_is_directory=True)

    with httpx.Client() as http_client:
        for audio_root, expected_message in (
            (audio_file, "must be a directory"),
            (symlink_root, "symbolic link"),
        ):
            settings = retention_settings_factory(audio_root=audio_root)
            with pytest.raises(
                RetentionWorkerConfigurationError,
                match=expected_message,
            ):
                build_retention_worker(
                    settings,
                    http_client=http_client,
                    session_factory=cast(Any, lambda: object()),
                )
