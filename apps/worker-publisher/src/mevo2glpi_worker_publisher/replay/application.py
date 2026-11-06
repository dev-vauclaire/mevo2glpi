"""CLI and runtime ownership for database-only publisher replay."""

from __future__ import annotations

import argparse
import logging
import re
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from enum import IntEnum
from typing import Protocol

from pydantic import ValidationError
from sqlalchemy.engine import Engine

from mevo2glpi_database import (
    AdvisoryLock,
    MailIngestionDisposition,
    MailIngestionReplayFilter,
    MailIngestionStatus,
    WorkerServiceName,
    create_database_engine,
    try_acquire_advisory_lock,
)

from .models import ReplayMode, ReplayReport, ReplayRequest
from .service import PublisherReplayService
from .settings import ReplaySettings

LOGGER = logging.getLogger(__name__)
_MAX_REPLAY_LIMIT = 1_000
_SAFE_IDENTIFIER = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


class ReplayExitCode(IntEnum):
    """Stable command exit codes."""

    SUCCESS = 0
    UNEXPECTED_ERROR = 1
    CONFIGURATION_ERROR = 2
    SINGLETON_CONFLICT = 3


class ReplayAlreadyRunningError(RuntimeError):
    """Report that the live publisher owns the shared singleton lock."""


class ReplayRunnerProtocol(Protocol):
    def __call__(
        self,
        settings: ReplaySettings,
        request: ReplayRequest,
    ) -> ReplayReport: ...


def parse_replay_request(arguments: Sequence[str] | None = None) -> ReplayRequest:
    """Translate bounded CLI arguments into one typed replay request."""
    parser = _build_argument_parser()
    namespace = parser.parse_args(arguments)
    if namespace.received_before <= namespace.received_after:
        parser.error("received-before must be later than received-after")
    try:
        replay_filter = MailIngestionReplayFilter(
            status=namespace.status,
            disposition=namespace.disposition,
            rejection_category=namespace.rejection_category,
            rejection_code=namespace.rejection_code,
            job_uuid_is_null=namespace.job_uuid_is_null,
            attachment_id_is_not_null=namespace.attachment_id_is_not_null,
            received_after=namespace.received_after,
            received_before=namespace.received_before,
            limit=namespace.limit,
        )
        mode = ReplayMode.APPLY if namespace.apply else ReplayMode.DRY_RUN
        return ReplayRequest(replay_filter=replay_filter, mode=mode)
    except (TypeError, ValueError):
        parser.error("replay filters are invalid")


def run_replay_application(
    settings: ReplaySettings,
    request: ReplayRequest,
    *,
    engine_factory: Callable[..., Engine] = create_database_engine,
    lock_factory: Callable[
        [Engine, WorkerServiceName], AdvisoryLock | None
    ] = try_acquire_advisory_lock,
    service_factory: Callable[..., PublisherReplayService] = PublisherReplayService,
) -> ReplayReport:
    """Own the database engine and publisher singleton lock for one replay."""
    if not isinstance(settings, ReplaySettings):
        raise TypeError("settings must be ReplaySettings")
    if not isinstance(request, ReplayRequest):
        raise TypeError("request must be ReplayRequest")
    if not callable(engine_factory):
        raise TypeError("engine_factory must be callable")
    if not callable(lock_factory):
        raise TypeError("lock_factory must be callable")
    if not callable(service_factory):
        raise TypeError("service_factory must be callable")

    engine = engine_factory(
        settings.database_url.get_secret_value(),
        connect_timeout_seconds=settings.database_connect_timeout_seconds,
    )
    advisory_lock = None
    try:
        advisory_lock = lock_factory(
            engine,
            WorkerServiceName.PUBLISHER,
        )
        if advisory_lock is None:
            raise ReplayAlreadyRunningError("publisher singleton lock is already held")
        service = service_factory(
            session_factory=advisory_lock.open_session,
            ownership_guard=advisory_lock.ensure_held,
        )
        return service.execute(request)
    finally:
        try:
            if advisory_lock is not None and not advisory_lock.is_released:
                advisory_lock.release()
        finally:
            engine.dispose()


def run_cli(
    arguments: Sequence[str] | None = None,
    *,
    settings_factory: Callable[[], ReplaySettings] = ReplaySettings,
    application_runner: ReplayRunnerProtocol = run_replay_application,
) -> ReplayExitCode:
    """Validate input, execute replay and emit content-free diagnostics."""
    request = parse_replay_request(arguments)
    try:
        settings = settings_factory()
    except ValidationError:
        LOGGER.error("publisher replay configuration is invalid")
        return ReplayExitCode.CONFIGURATION_ERROR
    except Exception as error:  # pragma: no cover - defensive startup boundary
        LOGGER.error(
            "publisher replay configuration loading failed error_type=%s",
            type(error).__name__,
        )
        return ReplayExitCode.UNEXPECTED_ERROR

    LOGGER.info("publisher replay starting mode=%s", request.mode.value)
    try:
        report = application_runner(settings, request)
    except ReplayAlreadyRunningError:
        LOGGER.error("publisher singleton lock is already held")
        return ReplayExitCode.SINGLETON_CONFLICT
    except Exception as error:  # pragma: no cover - defensive runtime boundary
        LOGGER.error(
            "publisher replay failed error_type=%s",
            type(error).__name__,
        )
        return ReplayExitCode.UNEXPECTED_ERROR

    for ingestion_uuid in report.selected_ingestion_uuids:
        LOGGER.info(
            "publisher replay candidate ingestion_uuid=%s mode=%s",
            ingestion_uuid,
            report.mode.value,
        )
    LOGGER.info(
        "publisher replay completed mode=%s selected_count=%s rearmed_count=%s",
        report.mode.value,
        report.selected_count,
        report.rearmed_count,
    )
    if report.selected_count == request.replay_filter.limit:
        LOGGER.warning(
            "publisher replay selection reached limit limit=%s repeat_required=true",
            request.replay_filter.limit,
        )
    return ReplayExitCode.SUCCESS


def _build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mevo2glpi-worker-publisher-replay",
        description="Safely rearm filtered terminal mail ingestions.",
    )
    parser.add_argument(
        "--status",
        required=True,
        type=_replayable_status,
        metavar="failed",
    )
    parser.add_argument(
        "--disposition",
        required=True,
        type=_disposition,
        metavar="rejected",
    )
    parser.add_argument("--rejection-category", required=True, type=_safe_identifier)
    parser.add_argument("--rejection-code", required=True, type=_safe_identifier)
    parser.add_argument(
        "--job-uuid-is-null",
        required=True,
        action="store_true",
        help="Mandatory safety guard: select only ingestions without a job.",
    )
    parser.add_argument(
        "--attachment-id-is-not-null",
        action="store_true",
        help="Select only ingestions that retained attachment metadata.",
    )
    parser.add_argument("--received-after", required=True, type=_utc_datetime)
    parser.add_argument("--received-before", required=True, type=_utc_datetime)
    parser.add_argument("--limit", type=_positive_limit, default=1_000)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    return parser


def _replayable_status(value: str) -> MailIngestionStatus:
    try:
        status = MailIngestionStatus(value)
    except ValueError:
        raise argparse.ArgumentTypeError("status must be failed") from None
    if status is not MailIngestionStatus.FAILED:
        raise argparse.ArgumentTypeError("status must be failed")
    return status


def _disposition(value: str) -> MailIngestionDisposition:
    try:
        disposition = MailIngestionDisposition(value)
    except ValueError:
        raise argparse.ArgumentTypeError("disposition must be rejected") from None
    if disposition is not MailIngestionDisposition.REJECTED:
        raise argparse.ArgumentTypeError("disposition must be rejected")
    return disposition


def _utc_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise argparse.ArgumentTypeError("timestamp must be ISO 8601 UTC") from None
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise argparse.ArgumentTypeError("timestamp must use UTC")
    return parsed.astimezone(UTC)


def _positive_limit(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("limit must be an integer") from None
    if parsed <= 0 or parsed > _MAX_REPLAY_LIMIT:
        raise argparse.ArgumentTypeError(
            f"limit must be between 1 and {_MAX_REPLAY_LIMIT}"
        )
    return parsed


def _safe_identifier(value: str) -> str:
    if _SAFE_IDENTIFIER.fullmatch(value) is None:
        raise argparse.ArgumentTypeError("value must be a safe identifier")
    return value


def main() -> int:
    """Console-script entry point."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    return int(run_cli())


__all__ = [
    "ReplayExitCode",
    "ReplayAlreadyRunningError",
    "main",
    "parse_replay_request",
    "run_cli",
    "run_replay_application",
]
