"""Complete persistent flow against disposable Transcribe AI and GLPI stacks."""

from __future__ import annotations

import html
import re
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select

from glpi_ticketing_common.audio_files import publish_audio
from glpi_ticketing_database import (
    AudioExtension,
    AudioRetention,
    JobEventActor,
    JobEventType,
    JobStage,
    JobStatus,
    PendingJob,
    RetentionStatus,
    TicketCreationJob,
    TicketCreationJobEvent,
    TicketCreationJobEventRepository,
    TicketCreationJobRepository,
    create_database_engine,
    create_session_factory,
)
from glpi_ticketing_glpi import (
    GLPI_API_VERSION,
    GlpiClient,
    TicketLevel,
    TicketType,
)
from glpi_ticketing_worker_automation.runtime import worker_runtime
from glpi_ticketing_worker_automation.settings import WorkerSettings
from glpi_ticketing_worker_automation.worker import (
    CompletedOutcome,
    IdleOutcome,
    RetryOutcome,
    SystemBlockedOutcome,
)

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(1200)]

PIPELINE_TIMEOUT_SECONDS = 15 * 60.0
GLPI_HTTP_TIMEOUT_SECONDS = 30.0
PUBLIC_BASE_URL = "https://audio.e2e.test/poc"
TICKET_TITLE = "Demande issue du pipeline E2E"
FIRST_PARAGRAPH_PATTERN = re.compile(r"\A\s*<p>(.*?)</p>", flags=re.DOTALL)


def test_worker_transcribes_audio_creates_ticket_and_completes_job(
    glpi_environment,
    transcribe_ai_environment,
    migrated_database_url: str,
    tmp_path: Path,
) -> None:
    """Exercise the composed worker through persistence and both real adapters."""

    audio_root = tmp_path / "audios"
    audio_root.mkdir()
    audio_uuid = uuid4()
    storage_key = f"{audio_uuid}.wav"
    with transcribe_ai_environment.audio_path.open("rb") as source:
        published = publish_audio(audio_root, storage_key, source)
    expected_audio = published.path.read_bytes()

    engine = create_database_engine(
        migrated_database_url,
        connect_timeout_seconds=5,
    )
    session_factory = create_session_factory(engine)
    try:
        with session_factory.begin() as session:
            snapshot = TicketCreationJobRepository(session).add_pending(
                PendingJob(
                    uuid=audio_uuid,
                    storage_key=storage_key,
                    audio_extension=AudioExtension.WAV,
                    audio_size_bytes=published.size_bytes,
                )
            )
            TicketCreationJobEventRepository(session).append(
                snapshot.uuid,
                occurred_at=snapshot.created_at,
                status=snapshot.status,
                stage=snapshot.stage,
                event_type=JobEventType.JOB_CREATED,
                actor=JobEventActor.WEB,
            )

        settings = _build_settings(
            database_url=migrated_database_url,
            audio_root=audio_root,
            glpi_environment=glpi_environment,
            transcribe_ai_environment=transcribe_ai_environment,
        )

        with worker_runtime(settings) as worker:
            outcome = _run_until_completed(worker)

        with session_factory() as session:
            job = session.get(TicketCreationJob, audio_uuid)
            retention = session.get(AudioRetention, audio_uuid)
            event_types = session.scalars(
                select(TicketCreationJobEvent.event_type)
                .where(TicketCreationJobEvent.job_uuid == audio_uuid)
                .order_by(TicketCreationJobEvent.event_id)
            ).all()
    finally:
        engine.dispose()

    if outcome.audio_uuid != audio_uuid:
        pytest.fail("The worker returned an unexpected audio UUID")
    if published.path.read_bytes() != expected_audio:
        pytest.fail("The canonical audio changed during processing")
    if {path.name for path in audio_root.iterdir()} != {storage_key}:
        pytest.fail("The worker created an unexpected filesystem lifecycle artifact")
    if job is None or job.status != JobStatus.COMPLETED.value:
        pytest.fail("The persistent job was not completed")
    if job.stage != JobStage.FINALIZATION.value:
        pytest.fail("The persistent job did not reach finalization")
    if job.glpi_ticket_id != int(outcome.ticket_id):
        pytest.fail("The persistent GLPI identifier differs from the outcome")
    if retention is None or retention.status != RetentionStatus.SCHEDULED.value:
        pytest.fail("The completed audio retention was not scheduled")
    expected_event_subsequence = [
        JobEventType.JOB_CREATED.value,
        JobEventType.PROCESSING_STARTED.value,
        JobEventType.TRANSCRIPTION_SUBMITTED.value,
        JobEventType.TRANSCRIPTION_SUCCEEDED.value,
        JobEventType.TICKET_CONFIRMED.value,
        JobEventType.JOB_COMPLETED.value,
    ]
    if not _contains_ordered_subsequence(event_types, expected_event_subsequence):
        pytest.fail("The persistent job event history is incomplete or unordered")
    forbidden_events = {
        JobEventType.JOB_FAILED.value,
        JobEventType.JOB_BLOCKED.value,
    }
    if forbidden_events.intersection(event_types):
        pytest.fail("The completed E2E job contains a terminal failure event")

    ticket = _find_ticket(
        glpi_environment=glpi_environment,
        audio_uuid=audio_uuid,
    )
    if ticket.get("id") != int(outcome.ticket_id):
        pytest.fail("The GLPI ticket does not match the worker outcome")
    if ticket.get("external_id") != str(audio_uuid):
        pytest.fail("The GLPI ticket external_id does not match the audio UUID")

    content = ticket.get("content")
    if not isinstance(content, str):
        pytest.fail("The GLPI ticket content is invalid")
    transcription_match = FIRST_PARAGRAPH_PATTERN.match(content)
    if transcription_match is None:
        pytest.fail("The GLPI ticket does not contain a transcription paragraph")
    transcription = html.unescape(transcription_match.group(1)).strip()
    if not transcription:
        pytest.fail("The GLPI ticket contains an empty transcription")

    expected_audio_url = f"{PUBLIC_BASE_URL}/audios/{audio_uuid}"
    if expected_audio_url not in content:
        pytest.fail("The GLPI ticket does not contain the expected audio URL")


def _build_settings(
    *,
    database_url: str,
    audio_root: Path,
    glpi_environment,
    transcribe_ai_environment,
) -> WorkerSettings:
    credentials = glpi_environment.credentials
    return WorkerSettings(
        public_base_url=PUBLIC_BASE_URL,
        database_url=database_url,
        audio_root=audio_root,
        database_connect_timeout_seconds=5,
        worker_idle_interval_seconds=1,
        worker_retry_initial_seconds=1,
        worker_retry_max_seconds=30,
        worker_system_probe_interval_seconds=2,
        completed_retention_days=90,
        failed_retention_days=30,
        blocked_retention_days=90,
        retention_check_interval_hours=6,
        legacy_transcription_base_url=transcribe_ai_environment.base_url,
        legacy_transcription_api_key=transcribe_ai_environment.api_key,
        transcription_http_timeout_seconds=30,
        transcription_poll_interval_seconds=2,
        transcription_job_timeout_seconds=PIPELINE_TIMEOUT_SECONDS,
        glpi_base_url=glpi_environment.base_url,
        glpi_username=credentials.username,
        glpi_password=SecretStr(credentials.password),
        glpi_client_id=credentials.client_id,
        glpi_client_secret=SecretStr(credentials.client_secret),
        glpi_http_timeout_seconds=GLPI_HTTP_TIMEOUT_SECONDS,
        glpi_ticket_title=TICKET_TITLE,
        glpi_ticket_type=TicketType.REQUEST,
        glpi_ticket_request_source_id=glpi_environment.request_source.id,
        glpi_ticket_urgency=TicketLevel.LOW,
        glpi_ticket_impact=TicketLevel.MEDIUM,
        _env_file=None,
    )


def _run_until_completed(worker) -> CompletedOutcome:
    deadline = time.monotonic() + PIPELINE_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        outcome = worker.run_once()
        if isinstance(outcome, CompletedOutcome):
            return outcome
        if isinstance(outcome, RetryOutcome):
            remaining = (outcome.next_attempt_at - datetime.now(UTC)).total_seconds()
            time.sleep(max(0.0, remaining))
            continue
        if isinstance(outcome, IdleOutcome):
            time.sleep(outcome.wait_seconds)
            continue
        if isinstance(outcome, SystemBlockedOutcome):
            if not outcome.automatic_probe:
                pytest.fail(
                    "The worker reached a latched system block during the E2E flow"
                )
            time.sleep(outcome.probe_after_seconds)
            continue
        pytest.fail(f"The worker stopped before completion (status: {outcome.status})")
    pytest.fail("The worker pipeline exceeded its deadline")


def _contains_ordered_subsequence(
    values: list[str],
    expected: list[str],
) -> bool:
    expected_iterator = iter(expected)
    next_expected = next(expected_iterator, None)
    for value in values:
        if value != next_expected:
            continue
        next_expected = next(expected_iterator, None)
        if next_expected is None:
            return True
    return next_expected is None


def _find_ticket(*, glpi_environment, audio_uuid: UUID) -> Mapping[str, object]:
    credentials = glpi_environment.credentials
    with httpx.Client() as http_client:
        client = GlpiClient(
            base_url=glpi_environment.base_url,
            username=credentials.username,
            password=SecretStr(credentials.password),
            client_id=credentials.client_id,
            client_secret=SecretStr(credentials.client_secret),
            http_client=http_client,
            timeout=GLPI_HTTP_TIMEOUT_SECONDS,
        )
        token = client.authenticate()
        response = http_client.get(
            (
                f"{glpi_environment.base_url}/api.php/v{GLPI_API_VERSION}"
                "/Assistance/Ticket"
            ),
            headers={
                "Accept": "application/json",
                "Authorization": (
                    f"{token.token_type} {token.access_token.get_secret_value()}"
                ),
            },
            params={"filter": f"external_id=={audio_uuid}", "limit": "2"},
            timeout=GLPI_HTTP_TIMEOUT_SECONDS,
            follow_redirects=False,
        )

    if response.status_code not in {httpx.codes.OK, httpx.codes.PARTIAL_CONTENT}:
        pytest.fail(
            f"The exact GLPI external_id lookup failed with HTTP {response.status_code}"
        )
    try:
        tickets = response.json()
    except ValueError:
        pytest.fail("The exact GLPI external_id lookup returned invalid JSON")
    if not isinstance(tickets, list) or len(tickets) != 1:
        count = len(tickets) if isinstance(tickets, list) else "invalid"
        pytest.fail(
            f"The exact GLPI external_id lookup returned an unexpected count ({count})"
        )
    ticket = tickets[0]
    if not isinstance(ticket, Mapping):
        pytest.fail("The exact GLPI external_id lookup returned an invalid ticket")
    return ticket
