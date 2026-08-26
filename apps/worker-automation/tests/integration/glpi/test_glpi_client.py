"""GLPI client contract against the disposable GLPI integration stack."""

from __future__ import annotations

from collections.abc import Mapping
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from glpi_ticketing_worker_automation.glpi import (
    GLPI_API_VERSION,
    GlpiClient,
    GlpiTicketDefaults,
    GlpiTicketInput,
    OAuthToken,
    TicketLevel,
    TicketType,
)

pytestmark = pytest.mark.glpi

GLPI_TICKET_PATH = f"/api.php/v{GLPI_API_VERSION}/Assistance/Ticket"
GLPI_HTTP_TIMEOUT_SECONDS = 10.0
TEST_TICKET_TITLE = "Demande issue d'un audio de test"
TEST_TICKET_DEFAULTS = GlpiTicketDefaults(
    title=TEST_TICKET_TITLE,
    ticket_type=TicketType.REQUEST,
    urgency=TicketLevel.LOW,
    impact=TicketLevel.MEDIUM,
)


def _new_glpi_client(glpi_environment, http_client: httpx.Client) -> GlpiClient:
    credentials = glpi_environment.credentials
    return GlpiClient(
        base_url=glpi_environment.base_url,
        username=credentials.username,
        password=SecretStr(credentials.password),
        client_id=credentials.client_id,
        client_secret=SecretStr(credentials.client_secret),
        ticket_defaults=TEST_TICKET_DEFAULTS,
        http_client=http_client,
        timeout=GLPI_HTTP_TIMEOUT_SECONDS,
    )


def test_password_grant_returns_access_token(glpi_environment) -> None:
    with httpx.Client() as http_client:
        client = _new_glpi_client(glpi_environment, http_client)
        token = client.authenticate()

    assert isinstance(token, OAuthToken)
    assert token.token_type == "Bearer"
    assert token.expires_in_seconds > 0
    assert len(token.access_token.get_secret_value()) > 0
    assert "access_token" not in repr(token)


def test_glpi_client_creates_one_ticket_per_audio_uuid(glpi_environment) -> None:
    audio_uuid = uuid4()
    transcription = "Transcription synthetique reservee au test GLPI."
    audio_url = f"https://audio.example.test/recordings/{audio_uuid}.wav"
    ticket_input = GlpiTicketInput(
        audio_uuid=audio_uuid,
        transcription=transcription,
        audio_url=audio_url,
    )

    with httpx.Client() as http_client:
        client = _new_glpi_client(glpi_environment, http_client)
        token = client.authenticate()
        first_ticket_id = client.ensure_ticket(ticket_input)
        second_ticket_id = client.ensure_ticket(ticket_input)

        response = http_client.get(
            f"{glpi_environment.base_url}{GLPI_TICKET_PATH}",
            headers={
                "Accept": "application/json",
                "Authorization": (
                    f"{token.token_type} {token.access_token.get_secret_value()}"
                ),
            },
            params={
                "filter": f"external_id=={audio_uuid}",
                "limit": "2",
            },
            timeout=GLPI_HTTP_TIMEOUT_SECONDS,
            follow_redirects=False,
        )

    if response.status_code not in {httpx.codes.OK, httpx.codes.PARTIAL_CONTENT}:
        pytest.fail(
            f"The exact external_id lookup failed with HTTP {response.status_code}"
        )
    try:
        tickets = response.json()
    except ValueError:
        pytest.fail("The exact external_id lookup returned invalid JSON")

    if not isinstance(tickets, list):
        pytest.fail("The exact external_id lookup returned an invalid collection")
    if len(tickets) != 1:
        pytest.fail(
            "The exact external_id lookup did not return exactly one ticket "
            f"(count: {len(tickets)})"
        )

    ticket = tickets[0]
    if not isinstance(ticket, Mapping):
        pytest.fail("The exact external_id lookup returned an invalid ticket")
    content = ticket.get("content")
    if not isinstance(content, str):
        pytest.fail("The created GLPI ticket has invalid content")
    if transcription not in content:
        pytest.fail("The created GLPI ticket does not contain the transcription")
    if audio_url not in content:
        pytest.fail("The created GLPI ticket does not contain the audio URL")

    assert first_ticket_id == second_ticket_id
    assert int(first_ticket_id) > 0
    assert ticket.get("id") == int(first_ticket_id)
    assert ticket.get("external_id") == str(audio_uuid)
    assert ticket.get("name") == TEST_TICKET_TITLE
    assert str(audio_uuid) not in str(ticket.get("name"))
    assert ticket.get("type") == int(TEST_TICKET_DEFAULTS.ticket_type)
    assert ticket.get("urgency") == int(TEST_TICKET_DEFAULTS.urgency)
    assert ticket.get("impact") == int(TEST_TICKET_DEFAULTS.impact)
    assert ticket.get("priority") in {level.value for level in TicketLevel}
