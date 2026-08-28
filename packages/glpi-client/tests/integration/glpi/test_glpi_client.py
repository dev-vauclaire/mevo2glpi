"""GLPI client contract against the disposable GLPI integration stack."""

from __future__ import annotations

from collections.abc import Mapping
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from glpi_ticketing_glpi import (
    GLPI_API_VERSION,
    GlpiClient,
    GlpiErrorCategory,
    GlpiOperation,
    GlpiTicketCreateCommand,
    GlpiTicketDefaults,
    GlpiTicketNotFoundError,
    GlpiTicketSnapshot,
    OAuthToken,
    TicketId,
    TicketLevel,
    TicketStatus,
    TicketType,
)

pytestmark = pytest.mark.glpi

GLPI_TICKET_PATH = f"/api.php/v{GLPI_API_VERSION}/Assistance/Ticket"
GLPI_HTTP_TIMEOUT_SECONDS = 10.0
TEST_TICKET_TITLE = "Demande issue d'un audio de test"
TEST_TICKET_TYPE = TicketType.REQUEST
TEST_TICKET_URGENCY = TicketLevel.LOW
TEST_TICKET_IMPACT = TicketLevel.MEDIUM
TEST_TICKET_DEFAULTS = GlpiTicketDefaults(
    title=TEST_TICKET_TITLE,
    ticket_type=TEST_TICKET_TYPE,
    urgency=TEST_TICKET_URGENCY,
    impact=TEST_TICKET_IMPACT,
)


def _new_glpi_client(glpi_environment, http_client: httpx.Client) -> GlpiClient:
    credentials = glpi_environment.credentials
    return GlpiClient(
        base_url=glpi_environment.base_url,
        username=credentials.username,
        password=SecretStr(credentials.password),
        client_id=credentials.client_id,
        client_secret=SecretStr(credentials.client_secret),
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
    command = GlpiTicketCreateCommand(
        audio_uuid=audio_uuid,
        transcription=transcription,
        audio_url=audio_url,
        defaults=TEST_TICKET_DEFAULTS,
    )

    with httpx.Client() as http_client:
        client = _new_glpi_client(glpi_environment, http_client)
        token = client.authenticate()
        first_ticket_id = client.ensure_ticket(command)
        second_ticket_id = client.ensure_ticket(command)
        snapshot = client.get_ticket(first_ticket_id)

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
    assert isinstance(snapshot, GlpiTicketSnapshot)
    assert snapshot.ticket_id == first_ticket_id
    assert snapshot.external_id == str(audio_uuid)
    assert snapshot.status is TicketStatus.INCOMING
    assert ticket.get("id") == int(first_ticket_id)
    assert ticket.get("external_id") == str(audio_uuid)
    assert ticket.get("name") == TEST_TICKET_TITLE
    assert str(audio_uuid) not in str(ticket.get("name"))
    assert ticket.get("type") == int(TEST_TICKET_TYPE)
    assert ticket.get("urgency") == int(TEST_TICKET_URGENCY)
    assert ticket.get("impact") == int(TEST_TICKET_IMPACT)
    assert ticket.get("priority") in {level.value for level in TicketLevel}


def test_get_ticket_tracks_initial_solved_and_closed_status(glpi_environment) -> None:
    audio_uuid = uuid4()
    command = GlpiTicketCreateCommand(
        audio_uuid=audio_uuid,
        transcription="Transcription reservee aux transitions GLPI de test.",
        audio_url=f"https://audio.example.test/recordings/{audio_uuid}.wav",
        defaults=TEST_TICKET_DEFAULTS,
    )

    with httpx.Client() as http_client:
        client = _new_glpi_client(glpi_environment, http_client)
        ticket_id = client.ensure_ticket(command)

        initial = client.get_ticket(ticket_id)
        solved_transition = glpi_environment.set_ticket_status(
            int(ticket_id),
            TicketStatus.SOLVED,
        )
        solved = client.get_ticket(ticket_id)
        closed_transition = glpi_environment.set_ticket_status(
            int(ticket_id),
            TicketStatus.CLOSED,
        )
        closed = client.get_ticket(ticket_id)

    assert initial.status is TicketStatus.INCOMING
    assert solved_transition.ticket_id == ticket_id
    assert solved_transition.status is TicketStatus.SOLVED
    assert solved.status is TicketStatus.SOLVED
    assert solved.external_id == str(audio_uuid)
    assert closed_transition.ticket_id == ticket_id
    assert closed_transition.status is TicketStatus.CLOSED
    assert closed.status is TicketStatus.CLOSED
    assert closed.external_id == str(audio_uuid)


def test_get_ticket_raises_dedicated_not_found_error(glpi_environment) -> None:
    with httpx.Client() as http_client:
        client = _new_glpi_client(glpi_environment, http_client)
        with pytest.raises(GlpiTicketNotFoundError) as error_info:
            client.get_ticket(TicketId(2_147_483_647))

    error = error_info.value
    assert error.operation is GlpiOperation.GET_TICKET
    assert error.category is GlpiErrorCategory.NOT_FOUND
    assert error.technical_code == "ticket_not_found"
    assert error.retryable is False
