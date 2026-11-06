from __future__ import annotations

from typing import Any, cast

import pytest
from pydantic import SecretStr

import mevo2glpi_worker_publisher.runtime.context as context_module
from mevo2glpi_database import WorkerRuntimeState, WorkerServiceName
from mevo2glpi_worker_publisher.publisher import IdleOutcome
from mevo2glpi_worker_publisher.runtime import (
    PublisherAlreadyRunningError,
    publisher_runtime,
)
from mevo2glpi_worker_publisher.settings import PublisherSettings


class _Engine:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def dispose(self) -> None:
        self.events.append("engine.dispose")


class _HttpClient:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def close(self) -> None:
        self.events.append("http.close")


class _Lock:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.is_released = False

    def ensure_held(self) -> None:
        self.events.append("lock.ensure")

    def open_session(self) -> object:
        self.events.append("lock.session")
        return object()

    def release(self) -> None:
        self.events.append("lock.release")
        self.is_released = True


class _Heartbeat:
    def __init__(self, lock: _Lock, *, heartbeat_interval_seconds: float) -> None:
        self.lock = lock
        self.interval = heartbeat_interval_seconds
        self.states: list[WorkerRuntimeState] = []
        self.outcomes: list[object] = []

    def record_state(self, state: WorkerRuntimeState) -> None:
        self.lock.events.append(f"heartbeat.{state.value}")
        self.states.append(state)

    def pulse_if_due(self) -> bool:
        self.lock.events.append("heartbeat.pulse")
        return True

    def start_ingestion(self, _ingestion_uuid: object) -> None:
        return None

    def report_outcome(self, outcome: object) -> None:
        self.lock.events.append("heartbeat.outcome")
        self.outcomes.append(outcome)


class _Worker:
    outcome = IdleOutcome(wait_seconds=30)

    def __init__(self, events: list[str]) -> None:
        self.events = events

    def run_once(self) -> IdleOutcome:
        self.events.append("worker.run")
        return self.outcome


def _settings() -> PublisherSettings:
    return PublisherSettings.model_construct(
        database_url=SecretStr("postgresql+psycopg://publisher:secret@database/jobs"),
        database_connect_timeout_seconds=7,
        publisher_heartbeat_interval_seconds=10.0,
    )


def test_runtime_uses_publisher_lock_fences_iteration_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    engine = _Engine(events)
    lock = _Lock(events)
    http_client = _HttpClient(events)
    worker = _Worker(events)
    heartbeat: _Heartbeat | None = None

    monkeypatch.setattr(
        context_module,
        "create_database_engine",
        lambda *_args, **_kwargs: engine,
    )

    def acquire(_engine: object, service: WorkerServiceName) -> _Lock:
        assert service is WorkerServiceName.PUBLISHER
        events.append("lock.acquire")
        return lock

    monkeypatch.setattr(context_module, "try_acquire_advisory_lock", acquire)

    def make_heartbeat(
        received_lock: _Lock,
        *,
        heartbeat_interval_seconds: float,
    ) -> _Heartbeat:
        nonlocal heartbeat
        heartbeat = _Heartbeat(
            received_lock,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
        )
        return heartbeat

    monkeypatch.setattr(
        context_module,
        "PublisherRuntimeHeartbeatReporter",
        make_heartbeat,
    )
    monkeypatch.setattr(
        context_module,
        "build_publisher_worker",
        lambda *_args, **_kwargs: worker,
    )

    with publisher_runtime(
        _settings(),
        http_client_factory=cast(Any, lambda: http_client),
    ) as runtime_worker:
        assert runtime_worker.run_once() is worker.outcome

    assert heartbeat is not None
    assert heartbeat.states == [
        WorkerRuntimeState.STARTING,
        WorkerRuntimeState.RUNNING,
        WorkerRuntimeState.STOPPED,
    ]
    assert events.index("lock.ensure") < events.index("worker.run")
    assert events.index("worker.run") < events.index("heartbeat.outcome")
    assert events[-3:] == ["http.close", "lock.release", "engine.dispose"]


def test_runtime_rejects_a_second_publisher_and_releases_started_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    engine = _Engine(events)
    monkeypatch.setattr(
        context_module,
        "create_database_engine",
        lambda *_args, **_kwargs: engine,
    )
    monkeypatch.setattr(
        context_module,
        "try_acquire_advisory_lock",
        lambda *_args, **_kwargs: None,
    )

    with pytest.raises(PublisherAlreadyRunningError):
        with publisher_runtime(_settings()):
            pass

    assert events == ["engine.dispose"]
