from __future__ import annotations

import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest
from pydantic import SecretStr

from mevo2glpi_database import (
    MailIngestionDisposition,
    MailIngestionStatus,
    WorkerServiceName,
)
from mevo2glpi_worker_publisher.replay.application import (
    ReplayAlreadyRunningError,
    ReplayExitCode,
    parse_replay_request,
    run_cli,
    run_replay_application,
)
from mevo2glpi_worker_publisher.replay.models import (
    ReplayMode,
    ReplayReport,
    ReplayRequest,
)
from mevo2glpi_worker_publisher.replay.settings import ReplaySettings

INGESTION_UUID = UUID("00000000-0000-0000-0000-000000000203")
SECRET = "never-log-replay-secret"


def _arguments(mode: str = "--dry-run") -> list[str]:
    return [
        "--status",
        "failed",
        "--disposition",
        "rejected",
        "--rejection-category",
        "invalid_attachment",
        "--rejection-code",
        "wav_attachment_missing",
        "--job-uuid-is-null",
        "--attachment-id-is-not-null",
        "--received-after",
        "2026-09-01T00:00:00Z",
        "--received-before",
        "2026-10-27T00:00:00+00:00",
        mode,
    ]


def _settings() -> ReplaySettings:
    return ReplaySettings(
        database_url=SecretStr("postgresql+psycopg://user:password@db/database")
    )


def test_parser_builds_typed_database_filters() -> None:
    request = parse_replay_request(_arguments())

    assert request.mode is ReplayMode.DRY_RUN
    assert request.replay_filter.status is MailIngestionStatus.FAILED
    assert request.replay_filter.disposition is MailIngestionDisposition.REJECTED
    assert request.replay_filter.rejection_category == "invalid_attachment"
    assert request.replay_filter.rejection_code == "wav_attachment_missing"
    assert request.replay_filter.job_uuid_is_null is True
    assert request.replay_filter.attachment_id_is_not_null is True
    assert request.replay_filter.received_after == datetime(2026, 9, 1, tzinfo=UTC)
    assert request.replay_filter.received_before == datetime(2026, 10, 27, tzinfo=UTC)
    assert request.replay_filter.limit == 1_000


@pytest.mark.parametrize(
    "arguments",
    [
        _arguments() + ["--apply"],
        [item for item in _arguments() if item != "--job-uuid-is-null"],
        [
            *(_arguments()[:-2]),
            "2026-10-27T00:00:00+02:00",
            "--dry-run",
        ],
    ],
)
def test_parser_rejects_ambiguous_or_unsafe_requests(arguments: list[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        parse_replay_request(arguments)

    assert raised.value.code == 2


def test_run_cli_logs_only_application_uuids_and_counts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def runner(_settings: ReplaySettings, request: ReplayRequest) -> ReplayReport:
        assert request.mode is ReplayMode.APPLY
        return ReplayReport(
            mode=request.mode,
            selected_ingestion_uuids=(INGESTION_UUID,),
            rearmed_ingestion_uuids=(INGESTION_UUID,),
        )

    with caplog.at_level(logging.INFO):
        exit_code = run_cli(
            _arguments("--apply"),
            settings_factory=_settings,
            application_runner=runner,
        )

    assert exit_code is ReplayExitCode.SUCCESS
    assert str(INGESTION_UUID) in caplog.text
    assert "selected_count=1 rearmed_count=1" in caplog.text
    assert "wav_attachment_missing" not in caplog.text
    assert SECRET not in caplog.text


def test_run_cli_warns_when_another_bounded_batch_may_exist(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def runner(_settings: ReplaySettings, request: ReplayRequest) -> ReplayReport:
        return ReplayReport(
            mode=request.mode,
            selected_ingestion_uuids=(INGESTION_UUID,),
        )

    arguments = _arguments()
    arguments[-1:-1] = ["--limit", "1"]
    with caplog.at_level(logging.INFO):
        exit_code = run_cli(
            arguments,
            settings_factory=_settings,
            application_runner=runner,
        )

    assert exit_code is ReplayExitCode.SUCCESS
    assert "selection reached limit limit=1 repeat_required=true" in caplog.text


def test_run_cli_hides_runtime_error_messages(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def fail(_settings: ReplaySettings, _request: ReplayRequest) -> ReplayReport:
        raise RuntimeError(SECRET)

    with caplog.at_level(logging.INFO):
        exit_code = run_cli(
            _arguments(),
            settings_factory=_settings,
            application_runner=fail,
        )

    assert exit_code is ReplayExitCode.UNEXPECTED_ERROR
    assert "error_type=RuntimeError" in caplog.text
    assert SECRET not in caplog.text


def test_run_cli_reports_the_shared_singleton_conflict() -> None:
    def fail(_settings: ReplaySettings, _request: ReplayRequest) -> ReplayReport:
        raise ReplayAlreadyRunningError("not logged")

    assert (
        run_cli(
            _arguments(),
            settings_factory=_settings,
            application_runner=fail,
        )
        is ReplayExitCode.SINGLETON_CONFLICT
    )


def test_runtime_uses_publisher_lock_and_releases_every_resource() -> None:
    request = parse_replay_request(_arguments())
    engine = SimpleNamespace(dispose_calls=0)
    engine.dispose = lambda: setattr(engine, "dispose_calls", engine.dispose_calls + 1)

    class Lock:
        is_released = False
        release_calls = 0

        def open_session(self) -> object:
            raise AssertionError("fake service does not open sessions")

        def ensure_held(self) -> None:
            return None

        def release(self) -> None:
            self.release_calls += 1
            self.is_released = True

    lock = Lock()
    observed: dict[str, object] = {}

    def engine_factory(url: str, *, connect_timeout_seconds: int) -> object:
        assert "password" in url
        assert connect_timeout_seconds == 5
        return engine

    def lock_factory(candidate_engine: object, service: WorkerServiceName) -> Lock:
        observed["engine"] = candidate_engine
        observed["service"] = service
        return lock

    class Service:
        def execute(self, selected_request: ReplayRequest) -> ReplayReport:
            assert selected_request is request
            return ReplayReport(
                mode=request.mode,
                selected_ingestion_uuids=(),
            )

    report = run_replay_application(
        _settings(),
        request,
        engine_factory=engine_factory,  # type: ignore[arg-type]
        lock_factory=lock_factory,  # type: ignore[arg-type]
        service_factory=lambda **_kwargs: Service(),  # type: ignore[arg-type]
    )

    assert report.selected_count == 0
    assert observed == {
        "engine": engine,
        "service": WorkerServiceName.PUBLISHER,
    }
    assert lock.release_calls == 1
    assert engine.dispose_calls == 1


def test_runtime_disposes_engine_when_publisher_lock_is_unavailable() -> None:
    request = parse_replay_request(_arguments())
    engine = SimpleNamespace(dispose_calls=0)
    engine.dispose = lambda: setattr(engine, "dispose_calls", engine.dispose_calls + 1)

    with pytest.raises(ReplayAlreadyRunningError):
        run_replay_application(
            _settings(),
            request,
            engine_factory=lambda *_args, **_kwargs: engine,  # type: ignore[arg-type]
            lock_factory=lambda *_args: None,
        )

    assert engine.dispose_calls == 1
