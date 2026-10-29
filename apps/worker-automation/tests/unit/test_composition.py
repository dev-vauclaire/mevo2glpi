"""Unit tests for worker dependency composition."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import httpx
import pytest

import glpi_ticketing_worker_automation.composition as composition_module
from glpi_ticketing_worker_automation.composition import (
    WorkerConfigurationError,
    build_automation_worker,
    build_public_audio_url,
)
from glpi_ticketing_worker_automation.settings import WorkerSettings
from glpi_ticketing_worker_automation.transcription import JobUuid

AUDIO_UUID = UUID("123e4567-e89b-42d3-a456-426614174000")
PROVIDER_JOB_ID = JobUuid("provider-job-42")


def test_public_audio_url_preserves_base_path_without_double_slash(
    worker_settings_factory: Callable[..., WorkerSettings],
) -> None:
    settings = worker_settings_factory()

    assert build_public_audio_url(settings.public_base_url, AUDIO_UUID) == (
        f"https://web.example.test/base/audios/{AUDIO_UUID}"
    )

    with pytest.raises(TypeError, match="UUID"):
        build_public_audio_url(
            settings.public_base_url,
            str(AUDIO_UUID),  # type: ignore[arg-type]
        )


def test_build_automation_worker_wires_stages_and_submission_callback(
    worker_settings_factory: Callable[..., WorkerSettings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = worker_settings_factory()
    http_client = httpx.Client()
    captured: dict[str, Any] = {}
    recorded_submissions: list[tuple[UUID, str, datetime]] = []
    transcription_client = object()
    transcriber = object()
    glpi_client = object()
    audio_inspector = object()
    transitions = object()
    transcription_stage = object()
    ticket_creation_stage = object()
    worker = object()

    class RecordingLifecycle:
        def __init__(self, **kwargs: object) -> None:
            captured["lifecycle"] = kwargs

        def record_transcription_submitted(
            self,
            audio_uuid: UUID,
            provider_job_id: str,
            now: datetime,
        ) -> None:
            recorded_submissions.append((audio_uuid, provider_job_id, now))

    def capture(name: str, result: object) -> Callable[..., object]:
        def factory(*args: object, **kwargs: object) -> object:
            captured[name] = {"args": args, "kwargs": kwargs}
            return result

        return factory

    def capture_lifecycle(**kwargs: object) -> RecordingLifecycle:
        recorded_lifecycle = RecordingLifecycle(**kwargs)
        captured["lifecycle_instance"] = recorded_lifecycle
        return recorded_lifecycle

    monkeypatch.setattr(
        composition_module,
        "AutomationJobLifecycleService",
        capture_lifecycle,
    )
    monkeypatch.setattr(
        composition_module,
        "LegacyTranscriptionClient",
        capture("transcription_client", transcription_client),
    )
    monkeypatch.setattr(
        composition_module,
        "LegacyPollingTranscriber",
        capture("poller", transcriber),
    )
    monkeypatch.setattr(
        composition_module,
        "GlpiClient",
        capture("glpi_client", glpi_client),
    )
    monkeypatch.setattr(
        composition_module,
        "AudioInspector",
        capture("audio_inspector", audio_inspector),
    )
    monkeypatch.setattr(
        composition_module,
        "JobTransitionService",
        capture("transitions", transitions),
    )
    monkeypatch.setattr(
        composition_module,
        "TranscriptionStage",
        capture("transcription_stage", transcription_stage),
    )
    monkeypatch.setattr(
        composition_module,
        "TicketCreationStage",
        capture("ticket_creation_stage", ticket_creation_stage),
    )
    monkeypatch.setattr(
        composition_module,
        "AutomationWorker",
        capture("worker", worker),
    )

    def session_factory() -> object:
        return object()

    ownership_checks: list[str] = []
    started_jobs: list[UUID] = []
    transcription_sleeps: list[float] = []

    try:
        result = build_automation_worker(
            settings,
            http_client=http_client,
            session_factory=cast(Any, session_factory),
            transcription_sleep=transcription_sleeps.append,
            transcription_should_stop=lambda: False,
            ownership_guard=lambda: ownership_checks.append("checked"),
            on_job_started=started_jobs.append,
        )
    finally:
        http_client.close()

    assert result is worker
    lifecycle_arguments = captured["lifecycle"]
    assert lifecycle_arguments["session_factory"] is session_factory
    assert callable(lifecycle_arguments["ownership_guard"])
    transcription_arguments = captured["transcription_client"]["kwargs"]
    assert transcription_arguments == {
        "base_url": "https://transcribe.example.test/prefix",
        "api_key": settings.legacy_transcription_api_key,
        "http_client": http_client,
        "timeout": 4.5,
    }
    poller_arguments = captured["poller"]["kwargs"]
    assert poller_arguments["client"] is transcription_client
    assert poller_arguments["poll_interval_seconds"] == 2.5
    assert poller_arguments["job_timeout_seconds"] == 120.0
    assert poller_arguments["sleep"] == transcription_sleeps.append
    assert callable(poller_arguments["should_stop"])

    callback = poller_arguments["on_job_submitted"]
    assert callable(callback)
    callback(AUDIO_UUID, PROVIDER_JOB_ID)

    assert len(recorded_submissions) == 1
    recorded_uuid, recorded_provider_id, recorded_at = recorded_submissions[0]
    assert recorded_uuid == AUDIO_UUID
    assert recorded_provider_id == PROVIDER_JOB_ID
    assert recorded_at.tzinfo is UTC

    transitions_arguments = captured["transitions"]["kwargs"]
    assert transitions_arguments["lifecycle"] is captured["lifecycle_instance"]
    assert transitions_arguments["retry_policy"].initial_seconds == 3.5
    assert transitions_arguments["retry_policy"].maximum_seconds == 30.0
    assert transitions_arguments["maximum_consecutive_retries"] == 4
    retention_policy = transitions_arguments["retention_policy"]
    assert retention_policy.completed_ttl.days == 91
    assert retention_policy.failed_ttl.days == 31
    assert retention_policy.blocked_ttl.days == 92
    assert retention_policy.first_completed_check_after.total_seconds() == 7 * 3600
    assert transitions_arguments["system_probe_interval_seconds"] == 45.0

    inspector_arguments = captured["audio_inspector"]
    assert inspector_arguments["args"] == (settings.audio_root.resolve(),)
    transcription_stage_arguments = captured["transcription_stage"]["kwargs"]
    assert transcription_stage_arguments["transcriber"] is transcriber
    assert transcription_stage_arguments["transitions"] is transitions
    ticket_stage_arguments = captured["ticket_creation_stage"]["kwargs"]
    assert ticket_stage_arguments["glpi_client"] is glpi_client
    assert ticket_stage_arguments["transitions"] is transitions
    assert ticket_stage_arguments["audio_url_for"](AUDIO_UUID) == (
        f"https://web.example.test/base/audios/{AUDIO_UUID}"
    )
    ticket_defaults = ticket_stage_arguments["ticket_defaults"]
    assert ticket_defaults.title == settings.glpi_ticket_title
    assert ticket_defaults.ticket_type is settings.glpi_ticket_type
    assert ticket_defaults.request_source_id == settings.glpi_ticket_request_source_id
    assert ticket_defaults.urgency is settings.glpi_ticket_urgency
    assert ticket_defaults.impact is settings.glpi_ticket_impact
    assert (
        ticket_stage_arguments["title_generator"].generate("not retained")
        == settings.glpi_ticket_title
    )

    worker_arguments = captured["worker"]["kwargs"]
    assert worker_arguments["lifecycle"] is captured["lifecycle_instance"]
    assert worker_arguments["audio_inspector"] is audio_inspector
    assert worker_arguments["transcription_stage"] is transcription_stage
    assert worker_arguments["ticket_creation_stage"] is ticket_creation_stage
    assert worker_arguments["transitions"] is transitions
    assert worker_arguments["idle_wait_seconds"] == 1.25
    assert worker_arguments["on_job_started"] == started_jobs.append


def test_openai_compatible_provider_is_composed_without_polling(
    worker_settings_factory: Callable[..., WorkerSettings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = worker_settings_factory(
        transcription_provider="openai_compatible",
        legacy_transcription_base_url=None,
        legacy_transcription_api_key=None,
        openai_compatible_transcription_base_url="http://parakeet:5092/v1",
        openai_compatible_transcription_api_key="synthetic-provider-key",
        openai_compatible_transcription_model="parakeet-tdt-0.6b",
        openai_compatible_transcription_language="fr",
    )
    captured: dict[str, object] = {}
    transcriber = object()

    def capture_openai_transcriber(**kwargs: object) -> object:
        captured.update(kwargs)
        return transcriber

    monkeypatch.setattr(
        composition_module,
        "OpenAICompatibleTranscriber",
        capture_openai_transcriber,
    )
    with httpx.Client() as http_client:
        result = composition_module._build_transcriber(
            settings,
            http_client=http_client,
            transcription_sleep=lambda _seconds: None,
            transcription_should_stop=lambda: False,
            record_transcription_submission=lambda _uuid, _provider_id: None,
        )

    assert result is transcriber
    assert captured.pop("http_client") is http_client
    assert captured == {
        "base_url": "http://parakeet:5092/v1",
        "api_key": settings.openai_compatible_transcription_api_key,
        "model": "parakeet-tdt-0.6b",
        "language": "fr",
        "timeout": settings.transcription_http_timeout_seconds,
    }


def test_build_automation_worker_rejects_missing_audio_root(
    tmp_path: Path,
    worker_settings_factory: Callable[..., WorkerSettings],
) -> None:
    missing_root = tmp_path / "missing"
    settings = worker_settings_factory(audio_root=missing_root)

    with httpx.Client() as http_client:
        with pytest.raises(WorkerConfigurationError, match="audio root"):
            build_automation_worker(
                settings,
                http_client=http_client,
                session_factory=cast(Any, lambda: object()),
            )
