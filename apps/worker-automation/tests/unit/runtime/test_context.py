"""Unit tests for automation worker runtime resource ownership."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import pytest

import glpi_ticketing_worker_automation.runtime.context as context_module
from glpi_ticketing_database import (
    AdvisoryLockConnectionLostError,
    WorkerRuntimeState,
    WorkerServiceName,
)
from glpi_ticketing_worker_automation.runtime.context import (
    WorkerAlreadyRunningError,
    worker_runtime,
)
from glpi_ticketing_worker_automation.settings import WorkerSettings
from glpi_ticketing_worker_automation.worker import IdleOutcome, RunOutcome


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
        self.ensure_error: BaseException | None = None
        self.is_released = False

    def ensure_held(self) -> None:
        self.events.append("lock.ensure_held")
        if self.ensure_error is not None:
            self.is_released = True
            raise self.ensure_error

    def open_session(self) -> object:
        self.events.append("lock.open_session")
        self.ensure_held()
        return object()

    def release(self) -> None:
        self.events.append("lock.release")
        self.is_released = True


class _FakeWorker:
    def __init__(self, events: list[str], outcome: RunOutcome) -> None:
        self.events = events
        self.outcome = outcome
        self.calls = 0

    def run_once(self) -> RunOutcome:
        self.events.append("worker.run_once")
        self.calls += 1
        return self.outcome


class _FakeHeartbeat:
    def __init__(self, lock: _FakeLock) -> None:
        self.lock = lock
        self.states: list[WorkerRuntimeState] = []
        self.outcomes: list[RunOutcome] = []
        lock.events.append("heartbeat.create")

    def record_state(self, state: WorkerRuntimeState) -> None:
        self.lock.events.append(f"heartbeat.{state.value}")
        self.states.append(state)

    def report_outcome(self, outcome: RunOutcome) -> None:
        self.lock.events.append(f"heartbeat.outcome.{outcome.status.value}")
        self.outcomes.append(outcome)


class _RuntimeHarness:
    def __init__(self, outcome: RunOutcome, *, lock_available: bool = True) -> None:
        self.events: list[str] = []
        self.engine = _FakeEngine(self.events)
        self.http_client = _FakeHttpClient(self.events)
        self.lock = _FakeLock(self.events)
        self.worker = _FakeWorker(self.events, outcome)
        self.lock_available = lock_available
        self.engine_arguments: tuple[str, int] | None = None
        self.build_arguments: dict[str, object] | None = None
        self.heartbeat: _FakeHeartbeat | None = None

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
            assert service_name is WorkerServiceName.AUTOMATION
            harness.events.append("lock.acquire")
            return harness.lock if harness.lock_available else None

        def build_worker(
            _settings: WorkerSettings,
            **kwargs: object,
        ) -> _FakeWorker:
            harness.events.append("worker.build")
            harness.build_arguments = kwargs
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
        monkeypatch.setattr(
            context_module,
            "build_automation_worker",
            build_worker,
        )
        monkeypatch.setattr(
            context_module,
            "RuntimeHeartbeatReporter",
            build_heartbeat,
        )

    def create_http_client(self) -> _FakeHttpClient:
        self.events.append("http.create")
        return self.http_client


def test_worker_runtime_owns_resources_and_guards_each_iteration(
    worker_settings_factory: Callable[..., WorkerSettings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome = IdleOutcome(wait_seconds=1)
    harness = _RuntimeHarness(outcome)
    harness.install(monkeypatch)
    settings = worker_settings_factory()
    injected_sleep: list[float] = []

    with worker_runtime(
        settings,
        http_client_factory=cast(Any, harness.create_http_client),
        transcription_sleep=injected_sleep.append,
        transcription_should_stop=lambda: False,
    ) as runtime_worker:
        assert runtime_worker.run_once() is outcome
        assert harness.build_arguments is not None
        guarded_should_stop = cast(
            Callable[[], bool],
            harness.build_arguments["transcription_should_stop"],
        )
        assert guarded_should_stop() is False

    assert harness.engine_arguments == (
        settings.database_url.get_secret_value(),
        7,
    )
    assert harness.build_arguments is not None
    assert harness.build_arguments["http_client"] is harness.http_client
    assert callable(harness.build_arguments["session_factory"])
    assert harness.build_arguments["transcription_sleep"] == injected_sleep.append
    assert callable(harness.build_arguments["ownership_guard"])
    assert harness.heartbeat is not None
    assert harness.heartbeat.states == [
        WorkerRuntimeState.STARTING,
        WorkerRuntimeState.RUNNING,
        WorkerRuntimeState.STOPPED,
    ]
    assert harness.heartbeat.outcomes == [outcome]
    assert (
        harness.events.index("lock.ensure_held")
        < harness.events.index("worker.run_once")
        < harness.events.index("heartbeat.outcome.idle")
    )
    assert (
        harness.events.index("heartbeat.stopped")
        < harness.events.index("http.close")
        < harness.events.index("lock.release")
        < harness.events.index("engine.dispose")
    )
    assert harness.http_client.closed
    assert harness.lock.is_released
    assert harness.engine.disposed


def test_runtime_guard_never_calls_worker_after_lock_loss(
    worker_settings_factory: Callable[..., WorkerSettings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _RuntimeHarness(IdleOutcome(wait_seconds=1))
    harness.install(monkeypatch)

    with worker_runtime(
        worker_settings_factory(),
        http_client_factory=cast(Any, harness.create_http_client),
    ) as runtime_worker:
        harness.lock.ensure_error = AdvisoryLockConnectionLostError(
            "synthetic lock loss"
        )
        with pytest.raises(
            AdvisoryLockConnectionLostError,
            match="synthetic lock loss",
        ):
            runtime_worker.run_once()

    assert harness.worker.calls == 0
    assert "worker.run_once" not in harness.events
    assert harness.heartbeat is not None
    assert WorkerRuntimeState.STOPPED not in harness.heartbeat.states
    assert harness.http_client.closed
    assert harness.engine.disposed
    assert "lock.release" not in harness.events


def test_worker_runtime_refuses_second_singleton_before_http_start(
    worker_settings_factory: Callable[..., WorkerSettings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _RuntimeHarness(
        IdleOutcome(wait_seconds=1),
        lock_available=False,
    )
    harness.install(monkeypatch)

    with pytest.raises(WorkerAlreadyRunningError):
        with worker_runtime(
            worker_settings_factory(),
            http_client_factory=cast(Any, harness.create_http_client),
        ):
            pytest.fail("a second singleton must never enter its runtime")

    assert "http.create" not in harness.events
    assert "worker.build" not in harness.events
    assert harness.heartbeat is None
    assert harness.engine.disposed
    assert not harness.http_client.closed
    assert not harness.lock.is_released
