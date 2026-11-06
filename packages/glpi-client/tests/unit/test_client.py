import json
import logging
import math
import ssl
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from inspect import signature
from urllib.parse import parse_qs
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr

from mevo2glpi_glpi import (
    GlpiClient,
    GlpiCreateOutcomeUnknownError,
    GlpiDuplicateExternalIdError,
    GlpiErrorCategory,
    GlpiHttpError,
    GlpiOperation,
    GlpiProtocolError,
    GlpiTicketCreateCommand,
    GlpiTicketDefaults,
    GlpiTicketNotFoundError,
    GlpiTicketRejectedError,
    GlpiTicketSnapshot,
    GlpiTransportError,
    OAuthToken,
    TicketId,
    TicketLevel,
    TicketStatus,
    TicketType,
)

BASE_URL = "https://glpi.example.test"
TOKEN_PATH = "/api.php/v2.3.0/token"
TICKET_PATH = "/api.php/v2.3.0/Assistance/Ticket"
USERNAME = "mevo2glpi-test-user"
PASSWORD = "synthetic-password"
CLIENT_ID = "synthetic-client-id"
CLIENT_SECRET = "synthetic-client-secret"
ACCESS_TOKEN = "synthetic-access-token"
REFRESHED_ACCESS_TOKEN = "synthetic-refreshed-access-token"
TIMEOUT_SECONDS = 3.5
AUDIO_UUID = UUID("88ac95f5-e207-4aa4-a824-2bd4a3269814")
TRANSCRIPTION = 'Patient <script>alert("private")</script> & suite\nDeuxieme ligne'
AUDIO_URL = (
    "https://storage.example.test/audio/88ac95f5-e207-4aa4-a824-2bd4a3269814"
    "?download=1&origin=worker"
)
TICKET_TITLE = "Demande issue d'un appel audio"
TICKET_TYPE = TicketType(1)
REQUEST_SOURCE_ID = 7
TICKET_URGENCY = TicketLevel(2)
TICKET_IMPACT = TicketLevel(3)
OPENED_AT = datetime(2026, 8, 31, 9, 18, 53, tzinfo=UTC)
ORIGINATING_PHONE_NUMBER = "0678374363"
DEFAULTS = GlpiTicketDefaults(
    title=TICKET_TITLE,
    ticket_type=TICKET_TYPE,
    request_source_id=REQUEST_SOURCE_ID,
    urgency=TICKET_URGENCY,
    impact=TICKET_IMPACT,
)


class MutableClock:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def token_response(
    access_token: str = ACCESS_TOKEN,
    *,
    expires_in: object = 3600,
) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "token_type": "Bearer",
            "expires_in": expires_in,
            "access_token": access_token,
            "refresh_token": "synthetic-refresh-token",
        },
    )


def make_ticket_command(
    *,
    transcription: object = TRANSCRIPTION,
    audio_url: object = AUDIO_URL,
    audio_uuid: object = AUDIO_UUID,
    opened_at: object = OPENED_AT,
    originating_phone_number: object = None,
) -> GlpiTicketCreateCommand:
    return GlpiTicketCreateCommand(
        audio_uuid=audio_uuid,  # type: ignore[arg-type]
        transcription=transcription,  # type: ignore[arg-type]
        audio_url=audio_url,  # type: ignore[arg-type]
        opened_at=opened_at,  # type: ignore[arg-type]
        defaults=DEFAULTS,
        originating_phone_number=originating_phone_number,  # type: ignore[arg-type]
    )


def make_glpi_client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    monotonic: Callable[[], float] | None = None,
    **overrides: object,
) -> tuple[GlpiClient, httpx.Client]:
    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    arguments: dict[str, object] = {
        "base_url": BASE_URL,
        "username": USERNAME,
        "password": SecretStr(PASSWORD),
        "client_id": CLIENT_ID,
        "client_secret": SecretStr(CLIENT_SECRET),
        "http_client": http_client,
        "timeout": TIMEOUT_SECONDS,
    }
    if monotonic is not None:
        arguments["monotonic"] = monotonic
    arguments.update(overrides)
    return GlpiClient(**arguments), http_client  # type: ignore[arg-type]


def assert_timeout(request: httpx.Request) -> None:
    assert request.extensions["timeout"] == {
        "connect": TIMEOUT_SECONDS,
        "read": TIMEOUT_SECONDS,
        "write": TIMEOUT_SECONDS,
        "pool": TIMEOUT_SECONDS,
    }


def assert_token_request(request: httpx.Request) -> None:
    assert request.method == "POST"
    assert request.url == httpx.URL(f"{BASE_URL}{TOKEN_PATH}")
    assert request.headers["content-type"].startswith(
        "application/x-www-form-urlencoded"
    )
    assert "authorization" not in request.headers
    assert parse_qs(request.content.decode()) == {
        "grant_type": ["password"],
        "client_id": [CLIENT_ID],
        "client_secret": [CLIENT_SECRET],
        "username": [USERNAME],
        "password": [PASSWORD],
        "scope": ["api"],
    }
    assert_timeout(request)


def assert_find_request(
    request: httpx.Request,
    *,
    access_token: str = ACCESS_TOKEN,
) -> None:
    assert request.method == "GET"
    assert request.url.path == TICKET_PATH
    assert dict(request.url.params.multi_items()) == {
        "filter": f"external_id=={AUDIO_UUID}",
        "limit": "2",
    }
    assert request.headers["authorization"] == f"Bearer {access_token}"
    assert_timeout(request)


def test_authenticate_uses_password_grant_on_pinned_v2_endpoint() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert_token_request(request)
        return token_response()

    client, http_client = make_glpi_client(handler)
    with http_client:
        token = client.authenticate()

    assert isinstance(token, OAuthToken)
    assert token.token_type == "Bearer"
    assert token.access_token.get_secret_value() == ACCESS_TOKEN
    assert token.expires_in_seconds == 3600
    assert ACCESS_TOKEN not in repr(token)
    assert len(requests) == 1


def test_client_constructor_contains_no_ticket_policy_defaults() -> None:
    assert "ticket_defaults" not in signature(GlpiClient).parameters


def test_authenticate_reuses_a_non_expired_token() -> None:
    request_count = 0
    clock = MutableClock(100.0)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return token_response()

    client, http_client = make_glpi_client(handler, monotonic=clock)
    with http_client:
        first_token = client.authenticate()
        clock.value = 200.0
        second_token = client.authenticate()

    assert first_token is second_token
    assert request_count == 1


def test_authenticate_refreshes_an_expired_token() -> None:
    request_count = 0
    clock = MutableClock(100.0)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return token_response(f"token-{request_count}")

    client, http_client = make_glpi_client(handler, monotonic=clock)
    with http_client:
        first_token = client.authenticate()
        clock.value = 4000.0
        second_token = client.authenticate()

    assert first_token.access_token.get_secret_value() == "token-1"
    assert second_token.access_token.get_secret_value() == "token-2"
    assert request_count == 2


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="not-json"),
        httpx.Response(200, json=[]),
        httpx.Response(200, json={}),
        httpx.Response(
            200,
            json={
                "token_type": "Bearer",
                "access_token": "",
                "expires_in": 3600,
            },
        ),
        httpx.Response(
            200,
            json={
                "token_type": "Basic",
                "access_token": ACCESS_TOKEN,
                "expires_in": 3600,
            },
        ),
        httpx.Response(
            200,
            json={
                "token_type": "Bearer",
                "access_token": ACCESS_TOKEN,
                "expires_in": 0,
            },
        ),
        httpx.Response(
            200,
            json={
                "token_type": "Bearer",
                "access_token": ACCESS_TOKEN,
                "expires_in": True,
            },
        ),
    ],
)
def test_authenticate_rejects_invalid_token_responses(
    response: httpx.Response,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return response

    client, http_client = make_glpi_client(handler)
    with http_client, pytest.raises(GlpiProtocolError) as error_info:
        client.authenticate()

    assert error_info.value.operation is GlpiOperation.AUTHENTICATE
    assert error_info.value.category is GlpiErrorCategory.PROTOCOL
    assert error_info.value.retryable is False
    assert error_info.value.outcome_unknown is False
    assert ACCESS_TOKEN not in str(error_info.value)


@pytest.mark.parametrize(
    ("status_code", "category", "retryable"),
    [
        (400, GlpiErrorCategory.AUTHENTICATION, False),
        (401, GlpiErrorCategory.AUTHENTICATION, False),
        (403, GlpiErrorCategory.AUTHORIZATION, False),
        (408, GlpiErrorCategory.UNAVAILABLE, True),
        (425, GlpiErrorCategory.UNAVAILABLE, True),
        (429, GlpiErrorCategory.UNAVAILABLE, True),
        (500, GlpiErrorCategory.UNAVAILABLE, True),
        (503, GlpiErrorCategory.UNAVAILABLE, True),
    ],
)
def test_authenticate_classifies_http_errors(
    status_code: int,
    category: GlpiErrorCategory,
    retryable: bool,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            text=f"sensitive {PASSWORD} {CLIENT_SECRET}",
        )

    client, http_client = make_glpi_client(handler)
    with http_client, pytest.raises(GlpiHttpError) as error_info:
        client.authenticate()

    error = error_info.value
    assert error.operation is GlpiOperation.AUTHENTICATE
    assert error.status_code == status_code
    assert error.category is category
    assert error.retryable is retryable
    assert error.outcome_unknown is False
    assert PASSWORD not in str(error)
    assert CLIENT_SECRET not in str(error)


def test_authenticate_classifies_transport_errors_without_leaking_secrets() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout(
            f"sensitive {PASSWORD} {CLIENT_SECRET}",
            request=request,
        )

    client, http_client = make_glpi_client(handler)
    with http_client, pytest.raises(GlpiTransportError) as error_info:
        client.authenticate()

    error = error_info.value
    assert error.operation is GlpiOperation.AUTHENTICATE
    assert error.category is GlpiErrorCategory.UNAVAILABLE
    assert error.retryable is True
    assert error.outcome_unknown is False
    assert PASSWORD not in str(error)
    assert CLIENT_SECRET not in str(error)


def test_debug_transport_diagnostic_exposes_tls_metadata_without_secrets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        tls_error = ssl.SSLCertVerificationError(
            1,
            f"sensitive {PASSWORD} {CLIENT_SECRET}",
        )
        tls_error.verify_code = 20
        tls_error.verify_message = "unable to get local issuer certificate"
        raise httpx.ConnectError(
            f"sensitive {PASSWORD} {CLIENT_SECRET}",
            request=request,
        ) from tls_error

    client, http_client = make_glpi_client(handler)
    with (
        http_client,
        caplog.at_level(logging.DEBUG, logger="mevo2glpi_glpi.client"),
        pytest.raises(GlpiTransportError),
    ):
        client.authenticate()

    rendered = caplog.text
    assert "GLPI transport diagnostic" in rendered
    assert "operation=authenticate" in rendered
    assert "host=glpi.example.test" in rendered
    assert "error_type=ConnectError" in rendered
    assert "root_cause_type=SSLCertVerificationError" in rendered
    assert "tls_verify_code=20" in rendered
    assert "tls_verify_message=unable to get local issuer certificate" in rendered
    assert PASSWORD not in rendered
    assert CLIENT_SECRET not in rendered
    diagnostic_records = [
        record
        for record in caplog.records
        if record.getMessage().startswith("GLPI transport diagnostic")
    ]
    assert len(diagnostic_records) == 1
    assert diagnostic_records[0].levelno == logging.DEBUG
    assert diagnostic_records[0].exc_info is None


def test_transport_diagnostic_is_silent_at_info(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("synthetic timeout", request=request)

    client, http_client = make_glpi_client(handler)
    with (
        http_client,
        caplog.at_level(logging.INFO, logger="mevo2glpi_glpi.client"),
        pytest.raises(GlpiTransportError),
    ):
        client.authenticate()

    assert "GLPI transport diagnostic" not in caplog.text


def test_get_ticket_uses_the_direct_id_endpoint() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == TOKEN_PATH:
            return token_response()
        assert request.method == "GET"
        assert request.url == httpx.URL(f"{BASE_URL}{TICKET_PATH}/73")
        assert not request.url.params
        assert request.headers["authorization"] == f"Bearer {ACCESS_TOKEN}"
        assert_timeout(request)
        return httpx.Response(
            200,
            json={
                "id": 73,
                "external_id": str(AUDIO_UUID),
                "status": {"id": 5, "name": "Résolu"},
            },
        )

    client, http_client = make_glpi_client(handler)
    with http_client:
        snapshot = client.get_ticket(TicketId(73))

    assert snapshot == GlpiTicketSnapshot(
        ticket_id=TicketId(73),
        external_id=str(AUDIO_UUID),
        status=TicketStatus.SOLVED,
    )
    assert "Résolu" not in repr(snapshot)
    assert len(requests) == 2


def test_get_ticket_refreshes_an_unauthorized_token_only_once() -> None:
    token_count = 0
    get_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal get_count, token_count
        if request.url.path == TOKEN_PATH:
            token_count += 1
            return token_response(f"token-{token_count}")

        get_count += 1
        assert request.headers["authorization"] == f"Bearer token-{get_count}"
        if get_count == 1:
            return httpx.Response(401)
        return httpx.Response(
            200,
            json={
                "id": 73,
                "external_id": str(AUDIO_UUID),
                "status": {"id": 5, "name": "localized and ignored"},
            },
        )

    client, http_client = make_glpi_client(handler)
    with http_client:
        snapshot = client.get_ticket(TicketId(73))

    assert snapshot.ticket_id == 73
    assert token_count == 2
    assert get_count == 2


@pytest.mark.parametrize("status", list(TicketStatus))
def test_get_ticket_maps_every_pinned_status_id(status: TicketStatus) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return token_response()
        return httpx.Response(
            200,
            json={
                "id": 73,
                "external_id": str(AUDIO_UUID),
                "status": {"id": int(status), "name": "ignored"},
            },
        )

    client, http_client = make_glpi_client(handler)
    with http_client:
        snapshot = client.get_ticket(TicketId(73))

    assert snapshot.status is status


def test_ticket_status_enum_is_pinned_to_glpi_11_0_8() -> None:
    assert {status.name: status.value for status in TicketStatus} == {
        "INCOMING": 1,
        "ASSIGNED": 2,
        "PLANNED": 3,
        "WAITING": 4,
        "SOLVED": 5,
        "CLOSED": 6,
        "APPROVAL": 10,
    }


def test_get_ticket_does_not_refresh_after_a_second_unauthorized_response() -> None:
    token_count = 0
    get_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal get_count, token_count
        if request.url.path == TOKEN_PATH:
            token_count += 1
            return token_response(f"token-{token_count}")
        get_count += 1
        return httpx.Response(401)

    client, http_client = make_glpi_client(handler)
    with http_client, pytest.raises(GlpiHttpError) as error_info:
        client.get_ticket(TicketId(73))

    error = error_info.value
    assert error.operation is GlpiOperation.GET_TICKET
    assert error.category is GlpiErrorCategory.AUTHENTICATION
    assert error.retryable is False
    assert token_count == 2
    assert get_count == 2


def test_get_ticket_classifies_not_found_without_retaining_the_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return token_response()
        return httpx.Response(404, text=f"sensitive {CLIENT_SECRET}")

    client, http_client = make_glpi_client(handler)
    with http_client, pytest.raises(GlpiTicketNotFoundError) as error_info:
        client.get_ticket(TicketId(73))

    error = error_info.value
    assert error.operation is GlpiOperation.GET_TICKET
    assert error.category is GlpiErrorCategory.NOT_FOUND
    assert error.technical_code == "ticket_not_found"
    assert error.retryable is False
    assert CLIENT_SECRET not in str(error)


@pytest.mark.parametrize("status_code", [408, 425, 429, 500, 503])
def test_get_ticket_classifies_service_unavailability(status_code: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return token_response()
        return httpx.Response(status_code, text=f"sensitive {PASSWORD}")

    client, http_client = make_glpi_client(handler)
    with http_client, pytest.raises(GlpiHttpError) as error_info:
        client.get_ticket(TicketId(73))

    error = error_info.value
    assert error.operation is GlpiOperation.GET_TICKET
    assert error.category is GlpiErrorCategory.UNAVAILABLE
    assert error.retryable is True
    assert error.outcome_unknown is False
    assert PASSWORD not in str(error)


@pytest.mark.parametrize(
    ("status_code", "category"),
    [
        (400, GlpiErrorCategory.INVALID_REQUEST),
        (403, GlpiErrorCategory.AUTHORIZATION),
    ],
)
def test_get_ticket_classifies_non_retryable_http_errors(
    status_code: int,
    category: GlpiErrorCategory,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return token_response()
        return httpx.Response(status_code, text=f"sensitive {CLIENT_SECRET}")

    client, http_client = make_glpi_client(handler)
    with http_client, pytest.raises(GlpiHttpError) as error_info:
        client.get_ticket(TicketId(73))

    error = error_info.value
    assert error.operation is GlpiOperation.GET_TICKET
    assert error.category is category
    assert error.retryable is False
    assert CLIENT_SECRET not in str(error)


@pytest.mark.parametrize("status_code", [201, 204, 301, 302])
def test_get_ticket_rejects_unexpected_success_or_redirect_statuses(
    status_code: int,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return token_response()
        return httpx.Response(status_code)

    client, http_client = make_glpi_client(handler)
    with http_client, pytest.raises(GlpiProtocolError) as error_info:
        client.get_ticket(TicketId(73))

    error = error_info.value
    assert error.operation is GlpiOperation.GET_TICKET
    assert error.technical_code == "unexpected_get_status"
    assert error.retryable is False


def test_get_ticket_classifies_transport_unavailability(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return token_response()
        raise httpx.ReadTimeout(f"sensitive {PASSWORD}", request=request)

    client, http_client = make_glpi_client(handler)
    with (
        http_client,
        caplog.at_level(logging.DEBUG, logger="mevo2glpi_glpi.client"),
        pytest.raises(GlpiTransportError) as error_info,
    ):
        client.get_ticket(TicketId(73))

    error = error_info.value
    assert error.operation is GlpiOperation.GET_TICKET
    assert error.category is GlpiErrorCategory.UNAVAILABLE
    assert error.retryable is True
    assert error.outcome_unknown is False
    assert PASSWORD not in str(error)
    assert "operation=get_ticket" in caplog.text
    assert PASSWORD not in caplog.text


@pytest.mark.parametrize(
    ("response", "technical_code"),
    [
        (httpx.Response(200, text="not-json"), "invalid_json"),
        (httpx.Response(200, json=[]), "expected_json_object"),
        (
            httpx.Response(
                200,
                json={
                    "id": 74,
                    "external_id": str(AUDIO_UUID),
                    "status": {"id": 5},
                },
            ),
            "mismatched_ticket_id",
        ),
        (
            httpx.Response(
                200,
                json={"id": 73, "external_id": "", "status": {"id": 5}},
            ),
            "invalid_ticket_external_id",
        ),
        (
            httpx.Response(
                200,
                json={"id": 73, "external_id": str(AUDIO_UUID), "status": True},
            ),
            "invalid_ticket_status",
        ),
        (
            httpx.Response(
                200,
                json={
                    "id": 73,
                    "external_id": str(AUDIO_UUID),
                    "status": {"id": True},
                },
            ),
            "invalid_ticket_status",
        ),
        (
            httpx.Response(
                200,
                json={
                    "id": 73,
                    "external_id": str(AUDIO_UUID),
                    "status": {"id": 7, "name": "unknown"},
                },
            ),
            "unsupported_ticket_status",
        ),
    ],
)
def test_get_ticket_rejects_invalid_contract_responses(
    response: httpx.Response,
    technical_code: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return token_response()
        return response

    client, http_client = make_glpi_client(handler)
    with http_client, pytest.raises(GlpiProtocolError) as error_info:
        client.get_ticket(TicketId(73))

    error = error_info.value
    assert error.operation is GlpiOperation.GET_TICKET
    assert error.technical_code == technical_code
    assert error.retryable is False


def test_ensure_ticket_searches_then_creates_with_the_expected_payload() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == TOKEN_PATH:
            assert_token_request(request)
            return token_response()
        if request.method == "GET":
            assert_find_request(request)
            return httpx.Response(200, json=[])

        assert request.method == "POST"
        assert request.url == httpx.URL(f"{BASE_URL}{TICKET_PATH}")
        assert request.headers["authorization"] == f"Bearer {ACCESS_TOKEN}"
        assert request.headers["content-type"].startswith("application/json")
        assert_timeout(request)
        payload = json.loads(request.content)
        assert payload["name"] == TICKET_TITLE
        assert str(AUDIO_UUID) not in payload["name"]
        assert payload["external_id"] == str(AUDIO_UUID)
        assert payload["date"] == "2026-08-31T11:18:53+02:00"
        assert "date_creation" not in payload
        assert payload["type"] == 1
        assert payload["request_type"] == {"id": REQUEST_SOURCE_ID}
        assert payload["urgency"] == 2
        assert payload["impact"] == 3
        assert "priority" not in payload
        assert "<script>" not in payload["content"]
        assert "&lt;script&gt;" in payload["content"]
        assert "&amp; suite" in payload["content"]
        assert "?download=1&amp;origin=worker" in payload["content"]
        assert TRANSCRIPTION not in payload["content"]
        return httpx.Response(
            201,
            json={"id": 73, "href": "/Assistance/Ticket/73"},
        )

    client, http_client = make_glpi_client(handler)
    with http_client:
        ticket_id = client.ensure_ticket(make_ticket_command())

    assert ticket_id == 73
    assert [request.method for request in requests] == ["POST", "GET", "POST"]


def test_ensure_ticket_labels_originating_phone_number_without_using_it_as_callback() -> (
    None
):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return token_response()
        if request.method == "GET":
            return httpx.Response(200, json=[])

        content = json.loads(request.content)["content"]
        assert "Numéro d’origine : 06 78 37 43 63" in content
        assert "peut différer du numéro de rappel demandé" in content
        return httpx.Response(201, json={"id": 73})

    client, http_client = make_glpi_client(handler)
    with http_client:
        ticket_id = client.ensure_ticket(
            make_ticket_command(
                originating_phone_number=ORIGINATING_PHONE_NUMBER,
            )
        )

    assert ticket_id == 73


@pytest.mark.parametrize(
    ("opened_at", "expected_glpi_date"),
    [
        (
            datetime(2026, 8, 31, 9, 18, 53, 987654, tzinfo=UTC),
            "2026-08-31T11:18:53+02:00",
        ),
        (
            datetime(2026, 12, 31, 9, 18, 53, 987654, tzinfo=UTC),
            "2026-12-31T10:18:53+01:00",
        ),
    ],
)
def test_ensure_ticket_serializes_opening_date_in_europe_paris(
    opened_at: datetime,
    expected_glpi_date: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return token_response()
        if request.method == "GET":
            return httpx.Response(200, json=[])
        payload = json.loads(request.content)
        assert payload["date"] == expected_glpi_date
        assert "date_creation" not in payload
        return httpx.Response(201, json={"id": 73})

    client, http_client = make_glpi_client(handler)
    with http_client:
        assert client.ensure_ticket(make_ticket_command(opened_at=opened_at)) == 73


def test_ownership_loss_after_search_prevents_ticket_creation() -> None:
    requests: list[httpx.Request] = []
    guard_calls = 0

    def ownership_guard() -> None:
        nonlocal guard_calls
        guard_calls += 1
        if guard_calls == 6:
            raise RuntimeError("synthetic ownership loss")

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == TOKEN_PATH:
            return token_response()
        assert request.method == "GET"
        return httpx.Response(200, json=[])

    client, http_client = make_glpi_client(
        handler,
        ownership_guard=ownership_guard,
    )
    with http_client, pytest.raises(RuntimeError, match="ownership loss"):
        client.ensure_ticket(make_ticket_command())

    assert [request.method for request in requests] == ["POST", "GET"]


def test_ensure_ticket_returns_an_existing_ticket_without_posting() -> None:
    request_methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        request_methods.append(request.method)
        if request.url.path == TOKEN_PATH:
            return token_response()
        assert_find_request(request)
        return httpx.Response(
            200,
            json=[{"id": 41, "external_id": str(AUDIO_UUID)}],
        )

    client, http_client = make_glpi_client(handler)
    with http_client:
        ticket_id = client.ensure_ticket(make_ticket_command())

    assert ticket_id == 41
    assert request_methods == ["POST", "GET"]


def test_ensure_ticket_rejects_duplicate_external_ids_without_posting() -> None:
    request_methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        request_methods.append(request.method)
        if request.url.path == TOKEN_PATH:
            return token_response()
        return httpx.Response(
            200,
            json=[
                {"id": 41, "external_id": str(AUDIO_UUID)},
                {"id": 42, "external_id": str(AUDIO_UUID)},
            ],
        )

    client, http_client = make_glpi_client(handler)
    with http_client, pytest.raises(GlpiDuplicateExternalIdError) as error_info:
        client.ensure_ticket(make_ticket_command())

    error = error_info.value
    assert error.operation is GlpiOperation.FIND_TICKET
    assert error.category is GlpiErrorCategory.CONFLICT
    assert error.retryable is False
    assert error.outcome_unknown is False
    assert request_methods == ["POST", "GET"]


@pytest.mark.parametrize(
    "payload",
    [
        "not-json",
        {},
        [{"external_id": str(AUDIO_UUID)}],
        [{"id": True, "external_id": str(AUDIO_UUID)}],
        [{"id": 0, "external_id": str(AUDIO_UUID)}],
    ],
)
def test_ensure_ticket_rejects_invalid_search_responses(payload: object) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return token_response()
        if payload == "not-json":
            return httpx.Response(200, text="not-json")
        return httpx.Response(200, json=payload)

    client, http_client = make_glpi_client(handler)
    with http_client, pytest.raises(GlpiProtocolError) as error_info:
        client.ensure_ticket(make_ticket_command())

    assert error_info.value.operation is GlpiOperation.FIND_TICKET
    assert error_info.value.category is GlpiErrorCategory.PROTOCOL


def test_search_transport_error_never_creates_a_ticket(
    caplog: pytest.LogCaptureFixture,
) -> None:
    request_methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        request_methods.append(request.method)
        if request.url.path == TOKEN_PATH:
            return token_response()
        raise httpx.ConnectError("GLPI unavailable", request=request)

    client, http_client = make_glpi_client(handler)
    with (
        http_client,
        caplog.at_level(logging.DEBUG, logger="mevo2glpi_glpi.client"),
        pytest.raises(GlpiTransportError) as error_info,
    ):
        client.ensure_ticket(make_ticket_command())

    error = error_info.value
    assert error.operation is GlpiOperation.FIND_TICKET
    assert error.category is GlpiErrorCategory.UNAVAILABLE
    assert error.retryable is True
    assert error.outcome_unknown is False
    assert request_methods == ["POST", "GET"]
    assert "operation=find_ticket" in caplog.text


def test_create_transport_diagnostic_identifies_the_ambiguous_operation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    get_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal get_count
        if request.url.path == TOKEN_PATH:
            return token_response()
        if request.method == "GET":
            get_count += 1
            return httpx.Response(200, json=[])
        raise httpx.ReadTimeout(
            f"sensitive {PASSWORD} {CLIENT_SECRET}",
            request=request,
        )

    client, http_client = make_glpi_client(handler)
    with (
        http_client,
        caplog.at_level(logging.DEBUG, logger="mevo2glpi_glpi.client"),
        pytest.raises(GlpiCreateOutcomeUnknownError),
    ):
        client.ensure_ticket(make_ticket_command())

    assert get_count == 2
    assert "operation=create_ticket" in caplog.text
    assert "error_type=ReadTimeout" in caplog.text
    assert PASSWORD not in caplog.text
    assert CLIENT_SECRET not in caplog.text


@pytest.mark.parametrize(
    "create_failure",
    [
        "transport",
        "http_503",
        "http_200",
        "http_204",
        "http_302",
        "invalid_json",
    ],
)
def test_ambiguous_create_failure_is_reconciled_without_a_second_post(
    create_failure: str,
) -> None:
    get_count = 0
    post_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal get_count, post_count
        if request.url.path == TOKEN_PATH:
            return token_response()
        if request.method == "GET":
            get_count += 1
            if get_count == 1:
                return httpx.Response(200, json=[])
            return httpx.Response(
                200,
                json=[{"id": 74, "external_id": str(AUDIO_UUID)}],
            )

        post_count += 1
        if create_failure == "transport":
            raise httpx.ReadTimeout("ambiguous create", request=request)
        if create_failure == "http_503":
            return httpx.Response(503, text="temporarily unavailable")
        if create_failure == "http_200":
            return httpx.Response(200, json={"id": 74})
        if create_failure == "http_204":
            return httpx.Response(204)
        if create_failure == "http_302":
            return httpx.Response(302, headers={"Location": "/unexpected"})
        return httpx.Response(201, text="not-json")

    client, http_client = make_glpi_client(handler)
    with http_client:
        ticket_id = client.ensure_ticket(make_ticket_command())

    assert ticket_id == 74
    assert get_count == 2
    assert post_count == 1


@pytest.mark.parametrize(
    "create_failure",
    [
        "transport",
        "http_503",
        "http_200",
        "http_204",
        "http_302",
        "invalid_json",
    ],
)
def test_unreconciled_create_failure_is_not_reposted_within_the_same_call(
    create_failure: str,
) -> None:
    get_count = 0
    post_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal get_count, post_count
        if request.url.path == TOKEN_PATH:
            return token_response()
        if request.method == "GET":
            get_count += 1
            return httpx.Response(200, json=[])

        post_count += 1
        if create_failure == "transport":
            raise httpx.ReadTimeout("ambiguous create", request=request)
        if create_failure == "http_503":
            return httpx.Response(503, text="temporarily unavailable")
        if create_failure == "http_200":
            return httpx.Response(200, json={"id": 74})
        if create_failure == "http_204":
            return httpx.Response(204)
        if create_failure == "http_302":
            return httpx.Response(302, headers={"Location": "/unexpected"})
        return httpx.Response(201, text="not-json")

    client, http_client = make_glpi_client(handler)
    with http_client, pytest.raises(GlpiCreateOutcomeUnknownError) as error_info:
        client.ensure_ticket(make_ticket_command())

    error = error_info.value
    assert error.operation is GlpiOperation.CREATE_TICKET
    assert error.category in {
        GlpiErrorCategory.UNAVAILABLE,
        GlpiErrorCategory.PROTOCOL,
    }
    assert error.retryable is True
    assert error.outcome_unknown is True
    assert get_count == 2
    assert post_count == 1


def test_create_401_refreshes_token_and_restarts_with_a_lookup() -> None:
    token_count = 0
    get_count = 0
    post_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_count, get_count, post_count
        if request.url.path == TOKEN_PATH:
            token_count += 1
            token = ACCESS_TOKEN if token_count == 1 else REFRESHED_ACCESS_TOKEN
            return token_response(token)
        if request.method == "GET":
            get_count += 1
            expected_token = ACCESS_TOKEN if get_count == 1 else REFRESHED_ACCESS_TOKEN
            assert_find_request(request, access_token=expected_token)
            if get_count == 1:
                return httpx.Response(200, json=[])
            return httpx.Response(
                200,
                json=[{"id": 75, "external_id": str(AUDIO_UUID)}],
            )

        post_count += 1
        assert request.headers["authorization"] == f"Bearer {ACCESS_TOKEN}"
        return httpx.Response(401, json={"error": "expired_token"})

    client, http_client = make_glpi_client(handler)
    with http_client:
        ticket_id = client.ensure_ticket(make_ticket_command())

    assert ticket_id == 75
    assert token_count == 2
    assert get_count == 2
    assert post_count == 1


def test_search_401_refreshes_token_and_retries_the_lookup_once() -> None:
    token_count = 0
    get_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_count, get_count
        if request.url.path == TOKEN_PATH:
            token_count += 1
            token = ACCESS_TOKEN if token_count == 1 else REFRESHED_ACCESS_TOKEN
            return token_response(token)
        get_count += 1
        expected_token = ACCESS_TOKEN if get_count == 1 else REFRESHED_ACCESS_TOKEN
        assert_find_request(request, access_token=expected_token)
        if get_count == 1:
            return httpx.Response(401, json={"error": "expired_token"})
        return httpx.Response(
            200,
            json=[{"id": 76, "external_id": str(AUDIO_UUID)}],
        )

    client, http_client = make_glpi_client(handler)
    with http_client:
        ticket_id = client.ensure_ticket(make_ticket_command())

    assert ticket_id == 76
    assert token_count == 2
    assert get_count == 2


def test_second_search_401_is_terminal_and_never_creates_a_ticket() -> None:
    token_count = 0
    get_count = 0
    post_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_count, get_count, post_count
        if request.url.path == TOKEN_PATH:
            token_count += 1
            return token_response(f"token-{token_count}")
        if request.method == "GET":
            get_count += 1
            return httpx.Response(401, json={"error": "expired_token"})
        post_count += 1
        return httpx.Response(201, json={"id": 99})

    client, http_client = make_glpi_client(handler)
    with http_client, pytest.raises(GlpiHttpError) as error_info:
        client.ensure_ticket(make_ticket_command())

    assert error_info.value.status_code == 401
    assert error_info.value.category is GlpiErrorCategory.AUTHENTICATION
    assert token_count == 2
    assert get_count == 2
    assert post_count == 0


def test_reconciliation_401_refreshes_once_without_reposting() -> None:
    token_count = 0
    get_count = 0
    post_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_count, get_count, post_count
        if request.url.path == TOKEN_PATH:
            token_count += 1
            return token_response(f"token-{token_count}")
        if request.method == "GET":
            get_count += 1
            if get_count == 2:
                return httpx.Response(401, json={"error": "expired_token"})
            return httpx.Response(200, json=[])
        post_count += 1
        raise httpx.ReadTimeout("ambiguous create", request=request)

    client, http_client = make_glpi_client(handler)
    with http_client, pytest.raises(GlpiCreateOutcomeUnknownError):
        client.ensure_ticket(make_ticket_command())

    assert token_count == 2
    assert get_count == 3
    assert post_count == 1


@pytest.mark.parametrize(
    ("status_code", "category", "retryable"),
    [
        (400, GlpiErrorCategory.INVALID_REQUEST, False),
        (403, GlpiErrorCategory.AUTHORIZATION, False),
        (409, GlpiErrorCategory.INVALID_REQUEST, False),
        (422, GlpiErrorCategory.INVALID_REQUEST, False),
    ],
)
def test_generic_create_http_errors_are_classified_without_reconciliation(
    status_code: int,
    category: GlpiErrorCategory,
    retryable: bool,
) -> None:
    get_count = 0
    post_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal get_count, post_count
        if request.url.path == TOKEN_PATH:
            return token_response()
        if request.method == "GET":
            get_count += 1
            return httpx.Response(200, json=[])
        post_count += 1
        return httpx.Response(
            status_code,
            text=f"sensitive {TRANSCRIPTION} {CLIENT_SECRET}",
        )

    client, http_client = make_glpi_client(handler)
    with http_client, pytest.raises(GlpiHttpError) as error_info:
        client.ensure_ticket(make_ticket_command())

    error = error_info.value
    assert error.operation is GlpiOperation.CREATE_TICKET
    assert error.status_code == status_code
    assert error.category is category
    assert error.retryable is retryable
    assert error.outcome_unknown is False
    assert get_count == 1
    assert post_count == 1
    assert TRANSCRIPTION not in str(error)
    assert CLIENT_SECRET not in str(error)


@pytest.mark.parametrize(
    ("field_name", "violation_code"),
    [
        ("content", "maxLength"),
        ("content", "pattern"),
        ("external_id", "maxLength"),
        ("external_id", "pattern"),
    ],
)
def test_create_raises_explicit_rejection_for_allowlisted_dynamic_validation(
    field_name: str,
    violation_code: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return token_response()
        if request.method == "GET":
            return httpx.Response(200, json=[])
        return httpx.Response(
            400,
            json={
                "status": "ERROR_INVALID_PARAMETER",
                "title": f"sensitive {TRANSCRIPTION}",
                "detail": {
                    field_name: [
                        {
                            "error": violation_code,
                            "message": f"sensitive {CLIENT_SECRET}",
                        }
                    ]
                },
            },
        )

    client, http_client = make_glpi_client(handler)
    with http_client, pytest.raises(GlpiTicketRejectedError) as error_info:
        client.ensure_ticket(make_ticket_command())

    error = error_info.value
    assert error.operation is GlpiOperation.CREATE_TICKET
    assert error.category is GlpiErrorCategory.INVALID_REQUEST
    assert error.technical_code == "ticket_input_rejected"
    assert error.retryable is False
    assert error.outcome_unknown is False
    rendered_error = f"{error!s} {error!r}"
    assert TRANSCRIPTION not in rendered_error
    assert CLIENT_SECRET not in rendered_error


@pytest.mark.parametrize(
    ("status_code", "payload"),
    [
        (
            400,
            {
                "status": "ERROR_INVALID_PARAMETER",
                "detail": {"name": [{"error": "maxLength"}]},
            },
        ),
        (
            400,
            {
                "status": "ERROR_INVALID_PARAMETER",
                "detail": {"content": [{"error": "required"}]},
            },
        ),
        (
            400,
            {"status": "ERROR_INVALID_PARAMETER", "detail": None},
        ),
        (
            400,
            {
                "status": "ERROR",
                "detail": {"content": [{"error": "maxLength"}]},
            },
        ),
        (
            409,
            {
                "status": "ERROR_INVALID_PARAMETER",
                "detail": {"content": [{"error": "maxLength"}]},
            },
        ),
        (
            422,
            {
                "status": "ERROR_INVALID_PARAMETER",
                "detail": {"content": [{"error": "maxLength"}]},
            },
        ),
    ],
)
def test_create_keeps_unallowlisted_error_responses_generic(
    status_code: int,
    payload: object,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return token_response()
        if request.method == "GET":
            return httpx.Response(200, json=[])
        return httpx.Response(status_code, json=payload)

    client, http_client = make_glpi_client(handler)
    with http_client, pytest.raises(GlpiHttpError) as error_info:
        client.ensure_ticket(make_ticket_command())

    error = error_info.value
    assert error.operation is GlpiOperation.CREATE_TICKET
    assert error.status_code == status_code
    assert error.retryable is False
    assert error.outcome_unknown is False


def test_client_errors_and_logs_never_expose_sensitive_values(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sensitive_response = " ".join(
        [PASSWORD, CLIENT_SECRET, ACCESS_TOKEN, TRANSCRIPTION]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return token_response()
        if request.method == "GET":
            return httpx.Response(200, json=[])
        return httpx.Response(400, text=sensitive_response)

    client, http_client = make_glpi_client(handler)
    with http_client, pytest.raises(GlpiHttpError) as error_info:
        client.ensure_ticket(make_ticket_command())

    rendered_error = f"{error_info.value!s} {error_info.value!r}"
    rendered_logs = caplog.text
    for sensitive_value in (PASSWORD, CLIENT_SECRET, ACCESS_TOKEN, TRANSCRIPTION):
        assert sensitive_value not in rendered_error
        assert sensitive_value not in rendered_logs


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("base_url", "relative/path"),
        ("base_url", "ftp://glpi.example.test"),
        ("base_url", "https://user:password@glpi.example.test"),
        ("base_url", "https://glpi.example.test?unsafe=true"),
        ("base_url", "https://glpi.example.test#fragment"),
        ("username", ""),
        ("username", " surrounded "),
        ("username", "contains\ncontrol"),
        ("password", SecretStr("")),
        ("password", SecretStr(" surrounded ")),
        ("client_id", ""),
        ("client_id", " surrounded "),
        ("client_secret", SecretStr("")),
        ("client_secret", SecretStr(" surrounded ")),
        ("timeout", 0),
        ("timeout", -1),
        ("timeout", math.inf),
        ("timeout", math.nan),
        ("timeout", True),
    ],
)
def test_client_rejects_invalid_constructor_values(
    field: str,
    invalid_value: object,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("invalid configuration must be rejected before any request")

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    arguments: dict[str, object] = {
        "base_url": BASE_URL,
        "username": USERNAME,
        "password": SecretStr(PASSWORD),
        "client_id": CLIENT_ID,
        "client_secret": SecretStr(CLIENT_SECRET),
        "http_client": http_client,
        "timeout": TIMEOUT_SECONDS,
    }
    arguments[field] = invalid_value

    with http_client, pytest.raises((TypeError, ValueError)):
        GlpiClient(**arguments)  # type: ignore[arg-type]


@pytest.mark.parametrize("ticket_id", [0, -1, True, "73", None])
def test_get_ticket_rejects_invalid_identifiers_without_request(
    ticket_id: object,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("invalid ticket IDs must be rejected before any request")

    client, http_client = make_glpi_client(handler)
    with http_client, pytest.raises(ValueError):
        client.get_ticket(ticket_id)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("audio_uuid", str(AUDIO_UUID)),
        ("transcription", 42),
        ("transcription", ""),
        ("transcription", "   "),
        ("transcription", "invalid\x00control"),
        ("transcription", "invalid\x7fcontrol"),
        ("audio_url", "relative/audio.wav"),
        ("audio_url", "ftp://storage.example.test/audio.wav"),
        ("audio_url", "https://user:password@storage.example.test/audio.wav"),
        ("opened_at", "2026-08-31T09:18:53Z"),
        ("opened_at", datetime(2026, 8, 31, 9, 18, 53)),
        (
            "opened_at",
            datetime(
                2026,
                8,
                31,
                11,
                18,
                53,
                tzinfo=timezone(timedelta(hours=2)),
            ),
        ),
        ("defaults", object()),
        ("originating_phone_number", 6_783_743_663),
        ("originating_phone_number", "06 78 37 43 63"),
        ("originating_phone_number", "0078374363"),
        ("originating_phone_number", "067837436"),
        ("originating_phone_number", "０６７８３７４３６３"),
    ],
)
def test_ticket_create_command_rejects_invalid_values(
    field: str,
    invalid_value: object,
) -> None:
    arguments: dict[str, object] = {
        "audio_uuid": AUDIO_UUID,
        "transcription": TRANSCRIPTION,
        "audio_url": AUDIO_URL,
        "opened_at": OPENED_AT,
        "defaults": DEFAULTS,
        "originating_phone_number": None,
    }
    arguments[field] = invalid_value

    with pytest.raises((TypeError, ValueError)):
        GlpiTicketCreateCommand(**arguments)  # type: ignore[arg-type]


def test_ticket_create_command_repr_hides_sensitive_content() -> None:
    command = make_ticket_command(
        originating_phone_number=ORIGINATING_PHONE_NUMBER,
    )

    assert TRANSCRIPTION not in repr(command)
    assert ORIGINATING_PHONE_NUMBER not in repr(command)


@pytest.mark.parametrize(
    "arguments",
    [
        {
            "title": "",
        },
        {
            "title": " surrounded ",
        },
        {
            "ticket_type": 99,
        },
        {
            "urgency": 0,
        },
    ],
)
def test_ticket_defaults_reject_invalid_ticket_values(
    arguments: dict[str, object],
) -> None:
    default_arguments: dict[str, object] = {
        "title": TICKET_TITLE,
        "ticket_type": TICKET_TYPE,
        "request_source_id": REQUEST_SOURCE_ID,
        "urgency": TICKET_URGENCY,
        "impact": TICKET_IMPACT,
    }
    default_arguments.update(arguments)

    with pytest.raises((TypeError, ValueError)):
        GlpiTicketDefaults(**default_arguments)  # type: ignore[arg-type]


@pytest.mark.parametrize("invalid_request_source_id", [True, False, "7", 7.0, None])
def test_ticket_defaults_rejects_non_integer_request_source_id(
    invalid_request_source_id: object,
) -> None:
    with pytest.raises(TypeError, match="request_source_id must be an integer"):
        GlpiTicketDefaults(
            title=TICKET_TITLE,
            ticket_type=TICKET_TYPE,
            request_source_id=invalid_request_source_id,  # type: ignore[arg-type]
            urgency=TICKET_URGENCY,
            impact=TICKET_IMPACT,
        )


@pytest.mark.parametrize("invalid_request_source_id", [0, -1])
def test_ticket_defaults_rejects_non_positive_request_source_id(
    invalid_request_source_id: int,
) -> None:
    with pytest.raises(
        ValueError, match="request_source_id must be a positive integer"
    ):
        GlpiTicketDefaults(
            title=TICKET_TITLE,
            ticket_type=TICKET_TYPE,
            request_source_id=invalid_request_source_id,
            urgency=TICKET_URGENCY,
            impact=TICKET_IMPACT,
        )
