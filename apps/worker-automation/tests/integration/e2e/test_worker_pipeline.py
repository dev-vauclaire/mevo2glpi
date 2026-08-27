"""Complete worker flow against disposable Transcribe AI and GLPI stacks."""

from __future__ import annotations

import html
import re
import time
from collections.abc import Mapping
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import SecretStr

from glpi_ticketing_common import audio_files
from glpi_ticketing_worker_automation.application import worker_runtime
from glpi_ticketing_worker_automation.glpi import (
    GLPI_API_VERSION,
    GlpiClient,
    GlpiTicketDefaults,
    TicketLevel,
    TicketType,
)
from glpi_ticketing_worker_automation.settings import WorkerSettings
from glpi_ticketing_worker_automation.worker import (
    AudioProcessor,
    CompletedOutcome,
    RetryOutcome,
)

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(1200)]

PIPELINE_TIMEOUT_SECONDS = 15 * 60.0
GLPI_HTTP_TIMEOUT_SECONDS = 30.0
PUBLIC_BASE_URL = "https://audio.e2e.test/poc"
TICKET_TITLE = "Demande issue du pipeline E2E"
FIRST_PARAGRAPH_PATTERN = re.compile(r"\A\s*<p>(.*?)</p>", flags=re.DOTALL)


def test_worker_transcribes_audio_creates_ticket_and_completes_file(
    glpi_environment,
    transcribe_ai_environment,
    tmp_path: Path,
) -> None:
    """Exercise the composed worker through both real service adapters."""
    directories = _create_audio_directories(tmp_path)
    audio_uuid = uuid4()
    pending_path = directories["pending"] / f"{audio_uuid}.wav"
    with transcribe_ai_environment.audio_path.open("rb") as source:
        audio_files.atomic_write(pending_path, source)
    expected_audio = pending_path.read_bytes()

    settings = _build_settings(
        directories=directories,
        glpi_environment=glpi_environment,
        transcribe_ai_environment=transcribe_ai_environment,
    )

    with worker_runtime(settings) as processor:
        outcome = _run_until_completed(processor)

    expected_completed_path = directories["completed"] / pending_path.name
    if outcome.completed_path != expected_completed_path:
        pytest.fail("The worker returned an unexpected completed audio path")
    if outcome.audio_uuid != audio_uuid:
        pytest.fail("The worker returned an unexpected audio UUID")
    if expected_completed_path.read_bytes() != expected_audio:
        pytest.fail("The completed audio differs from the submitted fixture")
    if any(directories["pending"].iterdir()) or any(
        directories["processing"].iterdir()
    ):
        pytest.fail("The completed audio still exists in an active directory")
    if any(directories["failed"].iterdir()):
        pytest.fail("The successful pipeline unexpectedly published a failed artifact")

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


def _create_audio_directories(tmp_path: Path) -> dict[str, Path]:
    directories = {
        name: tmp_path / name
        for name in ("pending", "processing", "completed", "failed")
    }
    for directory in directories.values():
        directory.mkdir()
    return directories


def _build_settings(
    *,
    directories: Mapping[str, Path],
    glpi_environment,
    transcribe_ai_environment,
) -> WorkerSettings:
    credentials = glpi_environment.credentials
    return WorkerSettings(
        public_base_url=PUBLIC_BASE_URL,
        audio_pending_dir=directories["pending"],
        audio_processing_dir=directories["processing"],
        audio_completed_dir=directories["completed"],
        audio_failed_dir=directories["failed"],
        worker_scan_interval_seconds=1.0,
        legacy_transcription_base_url=transcribe_ai_environment.base_url,
        legacy_transcription_api_key=transcribe_ai_environment.api_key,
        transcription_http_timeout_seconds=30.0,
        transcription_poll_interval_seconds=2.0,
        transcription_job_timeout_seconds=PIPELINE_TIMEOUT_SECONDS,
        glpi_base_url=glpi_environment.base_url,
        glpi_username=credentials.username,
        glpi_password=SecretStr(credentials.password),
        glpi_client_id=credentials.client_id,
        glpi_client_secret=SecretStr(credentials.client_secret),
        glpi_http_timeout_seconds=GLPI_HTTP_TIMEOUT_SECONDS,
        glpi_retry_interval_seconds=1.0,
        glpi_ticket_title=TICKET_TITLE,
        glpi_ticket_type=TicketType.REQUEST,
        glpi_ticket_urgency=TicketLevel.LOW,
        glpi_ticket_impact=TicketLevel.MEDIUM,
        _env_file=None,
    )


def _run_until_completed(processor: AudioProcessor) -> CompletedOutcome:
    deadline = time.monotonic() + PIPELINE_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        outcome = processor.run_once()
        if isinstance(outcome, CompletedOutcome):
            return outcome
        if isinstance(outcome, RetryOutcome):
            time.sleep(outcome.retry_after_seconds)
            continue
        pytest.fail(f"The worker stopped before completion (status: {outcome.status})")
    pytest.fail("The worker pipeline exceeded its deadline")


def _find_ticket(*, glpi_environment, audio_uuid: UUID) -> Mapping[str, object]:
    credentials = glpi_environment.credentials
    defaults = GlpiTicketDefaults(
        title=TICKET_TITLE,
        ticket_type=TicketType.REQUEST,
        urgency=TicketLevel.LOW,
        impact=TicketLevel.MEDIUM,
    )
    with httpx.Client() as http_client:
        client = GlpiClient(
            base_url=glpi_environment.base_url,
            username=credentials.username,
            password=SecretStr(credentials.password),
            client_id=credentials.client_id,
            client_secret=SecretStr(credentials.client_secret),
            ticket_defaults=defaults,
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
