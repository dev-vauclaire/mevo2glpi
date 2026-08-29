"""Unit tests for retention-worker runtime resource ownership."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, cast

import pytest

import glpi_ticketing_worker_retention.runtime.context as context_module
from glpi_ticketing_database import (
    AdvisoryLockConnectionLostError,
    WorkerRuntimeState,
    WorkerServiceName,
)
from glpi_ticketing_worker_retention.retention import IdleOutcome
from glpi_ticketing_worker_retention.runtime.context import (
    RetentionWorkerAlreadyRunningError,
    retention_worker_runtime,
)
from glpi_ticketing_worker_retention.settings import RetentionWorkerSettings

SYNTHETIC_SECRET = "never-log-runtime-secret"


class _FakeEngine:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.disposed = False

    def dispose(self) -> None:
        self.events.append("engine.dispose")
        self.disposed = True


class _FakeHttpClient:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.closed = False

    def close(self) -> None:
        self.events.append("http.close")
        self.closed = True


class _FakeLock:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.is_released = False
        self.ensure_calls = 0
        self.fail_ensure_call: int | None = None

    def ensure_held(self) -> None:
        self.ensure_calls += 1
        self.events.append("lock.ensure_held")
        if self.ensure_calls == self.fail_ensure_call:
            self.is_released = True
            raise AdvisoryLockConnectionLostError("synthetic lock loss")

    def open_session(self) -> object:
        self.events.append("lock.open_session")
        self.ensure_held()
        return object()

    def release(self) -> None:
        self.events.append("lock.release")
        self.is_released = True


class _FakeWorker:
    def __init__(self, events: list[str], outcome: IdleOutcome) -> None:
        self.events = events
        self.outcome = outcome
        self.calls = 0

    def run_once(self) -> IdleOutcome:
        self.events.append("worker.run_once")
        self.calls += 1
        return self.outcome


class _FakeHeartbeat:
    def __init__(self, lock: _FakeLock) -> None:
        self.lock = lock
        self.states: list[WorkerRuntimeState] = []
        self.outcomes: list[IdleOutcome] = []
        self.stop_error: Exception | None = None
        lock.events.append("heartbeat.create")

    def record_state(self, state: WorkerRuntimeState) -> None:
        self.lock.events.append(f"heartbeat.{state.value}")
        if state is WorkerRuntimeState.STOPPED and self.stop_error is not None:
            raise self.stop_error
        self.states.append(state)

    def report_outcome(self, outcome: IdleOutcome) -> None:
        self.lock.events.append(f"heartbeat.outcome.{outcome.status.value}")
        self.outcomes.append(outcome)


class _RuntimeHarness:
    def __init__(self, *, lock_available: bool = True) -> None:
        self.events: list[str] = []
        self.engine = _FakeEngine(self.events)
        self.http_client = _FakeHttpClient(self.events)
        self.lock = _FakeLock(self.events)
        self.worker = _FakeWorker(self.events, IdleOutcome(wait_seconds=60))
        self.lock_available = lock_available
        self.engine_arguments: tuple[str, int] | None = None
        self.build_arguments: dict[str, object] | None = None
        self.heartbeat: _FakeHeartbeat | None = None
        self.build_error: BaseException | None = None

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        harness = self

        def create_engine(
            database_url: str,
            *,
            connect_timeout_seconds: int,
        ) -> _FakeEngine:
            harness.events.append("engine.create")
            harness.engine_arguments = (database_url, connect_timeout_seconds)
            return harness.engine

        def acquire_lock(engine: object, service_name: object) -> _FakeLock | None:
            assert engine is harness.engine
            assert service_name is WorkerServiceName.RETENTION
            harness.events.append("lock.acquire")
            return harness.lock if harness.lock_available else None

        def build_worker(
            _settings: RetentionWorkerSettings,
            **kwargs: object,
        ) -> _FakeWorker:
            harness.events.append("worker.build")
            harness.build_arguments = kwargs
            if harness.build_error is not None:
                raise harness.build_error
            return harness.worker

        def build_heartbeat(lock: _FakeLock) -> _FakeHeartbeat:
            heartbeat = _FakeHeartbeat(lock)
            harness.heartbeat = heartbeat
            return heartbeat

        monkeypatch.setattr(context_module, "create_database_engine", create_engine)
        monkeypatch.setattr(
            context_module,
            "try_acquire_advisory_lock",
            acquire_lock,
        )
        monkeypatch.setattr(context_module, "build_retention_worker", build_worker)
        monkeypatch.setattr(
            context_module,
            "RetentionRuntimeHeartbeatReporter",
            build_heartbeat,
        )

    def create_http_client(self) -> _FakeHttpClient:
        self.events.append("http.create")
        return self.http_client


def test_runtime_owns_resources_uses_retention_lock_and_fences_each_iteration(
    retention_settings_factory: Callable[..., RetentionWorkerSettings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _RuntimeHarness()
    harness.install(monkeypatch)
    settings = retention_settings_factory()

    with retention_worker_runtime(
        settings,
        http_client_factory=cast(Any, harness.create_http_client),
    ) as runtime_worker:
        assert runtime_worker.run_once() is harness.worker.outcome

    assert harness.engine_arguments == (
        settings.database_url.get_secret_value(),
        7,
    )
    assert harness.build_arguments is not None
    assert harness.build_arguments["http_client"] is harness.http_client
    assert callable(harness.build_arguments["session_factory"])
    assert callable(harness.build_arguments["ownership_guard"])
    assert harness.heartbeat is not None
    assert harness.heartbeat.states == [
        WorkerRuntimeState.STARTING,
        WorkerRuntimeState.RUNNING,
        WorkerRuntimeState.STOPPED,
    ]
    assert harness.heartbeat.outcomes == [harness.worker.outcome]
    first_guard = harness.events.index("lock.ensure_held")
    worker_call = harness.events.index("worker.run_once")
    second_guard = harness.events.index("lock.ensure_held", first_guard + 1)
    heartbeat_call = harness.events.index("heartbeat.outcome.idle")
    assert first_guard < worker_call < second_guard < heartbeat_call
    assert (
        harness.events.index("heartbeat.stopped")
        < harness.events.index("http.close")
        < harness.events.index("lock.release")
        < harness.events.index("engine.dispose")
    )


def test_runtime_never_calls_worker_when_lock_is_lost_before_iteration(
    retention_settings_factory: Callable[..., RetentionWorkerSettings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _RuntimeHarness()
    harness.install(monkeypatch)
    harness.lock.fail_ensure_call = 1

    with retention_worker_runtime(
        retention_settings_factory(),
        http_client_factory=cast(Any, harness.create_http_client),
    ) as runtime_worker:
        with pytest.raises(AdvisoryLockConnectionLostError):
            runtime_worker.run_once()

    assert harness.worker.calls == 0
    assert harness.heartbeat is not None
    assert harness.heartbeat.outcomes == []
    assert WorkerRuntimeState.STOPPED not in harness.heartbeat.states
    assert "lock.release" not in harness.events
    assert harness.http_client.closed
    assert harness.engine.disposed


def test_runtime_never_publishes_outcome_after_lock_is_lost_during_iteration(
    retention_settings_factory: Callable[..., RetentionWorkerSettings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _RuntimeHarness()
    harness.install(monkeypatch)
    harness.lock.fail_ensure_call = 2

    with retention_worker_runtime(
        retention_settings_factory(),
        http_client_factory=cast(Any, harness.create_http_client),
    ) as runtime_worker:
        with pytest.raises(AdvisoryLockConnectionLostError):
            runtime_worker.run_once()

    assert harness.worker.calls == 1
    assert harness.heartbeat is not None
    assert harness.heartbeat.outcomes == []
    assert WorkerRuntimeState.STOPPED not in harness.heartbeat.states
    assert "lock.release" not in harness.events


def test_runtime_refuses_second_singleton_before_http_start(
    retention_settings_factory: Callable[..., RetentionWorkerSettings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _RuntimeHarness(lock_available=False)
    harness.install(monkeypatch)

    with pytest.raises(RetentionWorkerAlreadyRunningError):
        with retention_worker_runtime(
            retention_settings_factory(),
            http_client_factory=cast(Any, harness.create_http_client),
        ):
            pytest.fail("a second singleton must never enter its runtime")

    assert "http.create" not in harness.events
    assert "worker.build" not in harness.events
    assert harness.heartbeat is None
    assert harness.engine.disposed
    assert not harness.http_client.closed
    assert "lock.release" not in harness.events


def test_runtime_cleans_up_after_composition_failure(
    retention_settings_factory: Callable[..., RetentionWorkerSettings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _RuntimeHarness()
    harness.build_error = RuntimeError(SYNTHETIC_SECRET)
    harness.install(monkeypatch)

    with pytest.raises(RuntimeError, match=SYNTHETIC_SECRET):
        with retention_worker_runtime(
            retention_settings_factory(),
            http_client_factory=cast(Any, harness.create_http_client),
        ):
            pytest.fail("composition failure must prevent runtime entry")

    assert harness.http_client.closed
    assert harness.lock.is_released
    assert harness.engine.disposed
    assert harness.heartbeat is not None
    assert harness.heartbeat.states == [
        WorkerRuntimeState.STARTING,
        WorkerRuntimeState.STOPPED,
    ]


def test_cleanup_sanitizes_heartbeat_error_and_still_releases_resources(
    retention_settings_factory: Callable[..., RetentionWorkerSettings],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    harness = _RuntimeHarness()
    harness.install(monkeypatch)

    with caplog.at_level(logging.WARNING):
        with retention_worker_runtime(
            retention_settings_factory(),
            http_client_factory=cast(Any, harness.create_http_client),
        ):
            assert harness.heartbeat is not None
            harness.heartbeat.stop_error = RuntimeError(SYNTHETIC_SECRET)

    assert "error_type=RuntimeError" in caplog.text
    assert SYNTHETIC_SECRET not in caplog.text
    assert harness.http_client.closed
    assert harness.lock.is_released
    assert harness.engine.disposed
