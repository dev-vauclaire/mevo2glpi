"""Contract tests for the synchronous Microsoft Graph HTTP adapter."""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from uuid import UUID

import httpx2 as httpx
import pytest
from pydantic import SecretStr

from mevo2glpi_worker_publisher.graph.client import (
    DEFAULT_MAX_ATTACHMENT_BYTES,
    DOWNLOAD_CHUNK_SIZE,
    GraphClient,
)
from mevo2glpi_worker_publisher.graph.contracts import (
    GraphHttpError,
    GraphOutcomeUnknownError,
    GraphPayloadTooLargeError,
    GraphProtocolError,
    GraphTransportError,
)
from mevo2glpi_worker_publisher.graph.models import (
    AttachmentId,
    FolderId,
    GraphAccessToken,
    GraphAttachmentKind,
    GraphErrorCategory,
    GraphOperation,
    MessageId,
    MoveOutcome,
)

BASE_URL = "https://graph.example.test/v1.0"
MAILBOX_ID = UUID("f2cc915e-dc29-4efb-92c8-05699d099b5c")
ACCESS_TOKEN = "synthetic-graph-access-token"
SECOND_ACCESS_TOKEN = "synthetic-refreshed-access-token"
MESSAGE_ID = MessageId("AAMkSyntheticImmutableMessage")
ATTACHMENT_ID = AttachmentId("AAMkSyntheticAttachment")
SOURCE_FOLDER_ID = FolderId("inbox")
DESTINATION_FOLDER_ID = FolderId("AAMkProcessedFolder")
TIMEOUT_SECONDS = 3.5
IMMUTABLE_PREFERENCE = 'IdType="ImmutableId"'


class StubTokenProvider:
    def __init__(self) -> None:
        self.access_calls = 0
        self.invalidations = 0

    def get_access_token(self) -> GraphAccessToken:
        self.access_calls += 1
        value = ACCESS_TOKEN if self.invalidations == 0 else SECOND_ACCESS_TOKEN
        return GraphAccessToken(value=SecretStr(value), expires_in_seconds=3600)

    def invalidate(self) -> None:
        self.invalidations += 1


class TrackingStream(httpx.SyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.yield_count = 0

    def __iter__(self) -> Iterator[bytes]:
        for chunk in self.chunks:
            self.yield_count += 1
            yield chunk


class CountingSink:
    def __init__(self) -> None:
        self.size_bytes = 0

    def write(self, content: bytes) -> int:
        self.size_bytes += len(content)
        return len(content)


def make_client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    page_size: int = 50,
    max_attachment_bytes: int = 25 * 1024 * 1024,
) -> tuple[GraphClient, httpx.Client, StubTokenProvider]:
    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    token_provider = StubTokenProvider()
    client = GraphClient(
        mailbox_id=MAILBOX_ID,
        http_client=http_client,
        token_provider=token_provider,
        timeout=TIMEOUT_SECONDS,
        base_url=BASE_URL,
        page_size=page_size,
        max_attachment_bytes=max_attachment_bytes,
    )
    return client, http_client, token_provider


def assert_common_request(
    request: httpx.Request,
    *,
    token: str = ACCESS_TOKEN,
    accept: str = "application/json",
) -> None:
    assert request.headers["authorization"] == f"Bearer {token}"
    assert UUID(request.headers["client-request-id"])
    assert request.headers["prefer"] == IMMUTABLE_PREFERENCE
    assert request.headers["return-client-request-id"] == "true"
    assert request.headers["accept"] == accept
    assert request.extensions["timeout"] == {
        "connect": TIMEOUT_SECONDS,
        "read": TIMEOUT_SECONDS,
        "write": TIMEOUT_SECONDS,
        "pool": TIMEOUT_SECONDS,
    }


def message_payload(message_id: str, folder_id: str = "inbox") -> dict[str, object]:
    return {
        "id": message_id,
        "parentFolderId": folder_id,
        "receivedDateTime": "2026-09-08T07:30:00Z",
        "hasAttachments": True,
        "subject": (
            "Message vocale de la part de01 00 00 00 00pourMEVO ETABLISSEMENT-TEST"
        ),
    }


def test_list_messages_uses_configured_page_size_and_follows_safe_next_link() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert_common_request(request)
        if len(requests) == 1:
            assert request.method == "GET"
            assert request.url.path.endswith(
                f"/users/{MAILBOX_ID}/mailFolders/inbox/messages"
            )
            assert request.url.params["$top"] == "2"
            assert request.url.params["$select"] == (
                "id,parentFolderId,receivedDateTime,hasAttachments,subject"
            )
            return httpx.Response(
                200,
                json={
                    "value": [message_payload("immutable-message-1")],
                    "@odata.nextLink": (
                        f"{BASE_URL}/users/{MAILBOX_ID}/mailFolders/inbox/messages"
                        "?$top=2&$skiptoken=opaque-next-page"
                    ),
                },
            )
        assert request.url.params["$skiptoken"] == "opaque-next-page"
        return httpx.Response(
            200,
            json={"value": [message_payload("immutable-message-2")]},
        )

    client, http_client, _ = make_client(handler, page_size=2)
    with http_client:
        messages = client.list_messages()

    assert [message.message_id for message in messages] == [
        "immutable-message-1",
        "immutable-message-2",
    ]
    assert messages[0].received_at == datetime(2026, 9, 8, 7, 30, tzinfo=UTC)
    assert messages[0].subject == (
        "Message vocale de la part de01 00 00 00 00pourMEVO ETABLISSEMENT-TEST"
    )
    assert "01 00 00 00 00" not in repr(messages[0])
    assert len(requests) == 2
    assert len({request.headers["client-request-id"] for request in requests}) == 2


@pytest.mark.parametrize("subject", [None, ""])
def test_list_messages_accepts_an_absent_subject(subject: object) -> None:
    payload = message_payload("immutable-message")
    payload["subject"] = subject

    client, http_client, _ = make_client(
        lambda _request: httpx.Response(200, json={"value": [payload]})
    )
    with http_client:
        messages = client.list_messages()

    assert messages[0].subject is None


@pytest.mark.parametrize("subject", [42, "x" * 1_025, "unsafe\nsubject"])
def test_list_messages_ignores_an_invalid_optional_subject(subject: object) -> None:
    payload = message_payload("immutable-message")
    payload["subject"] = subject

    client, http_client, _ = make_client(
        lambda _request: httpx.Response(200, json={"value": [payload]})
    )
    with http_client:
        messages = client.list_messages()

    assert messages[0].subject is None


@pytest.mark.parametrize(
    "unsafe_next_link",
    [
        "https://attacker.example/v1.0/users/mailbox/messages?page=2",
        "https://graph.example.test/beta/users/mailbox/messages?page=2",
        "http://graph.example.test/v1.0/users/mailbox/messages?page=2",
    ],
)
def test_list_messages_rejects_next_link_outside_configured_origin_and_api_root(
    unsafe_next_link: str,
) -> None:
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(
            200,
            json={"value": [], "@odata.nextLink": unsafe_next_link},
        )

    client, http_client, _ = make_client(handler)
    with http_client, pytest.raises(GraphProtocolError) as captured_error:
        client.list_messages()

    error = captured_error.value
    assert error.operation is GraphOperation.LIST_MESSAGES
    assert error.category is GraphErrorCategory.PROTOCOL
    assert error.retryable is False
    assert request_count == 1


def test_every_mailbox_operation_uses_the_immutable_id_preference() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        accept = (
            "application/octet-stream"
            if request.url.path.endswith("/$value")
            else "application/json"
        )
        assert_common_request(request, accept=accept)
        if request.url.path.endswith("/mailFolders/inbox/messages"):
            return httpx.Response(200, json={"value": []})
        if request.url.path.endswith(f"/messages/{MESSAGE_ID}/attachments"):
            return httpx.Response(200, json={"value": []})
        if request.url.path.endswith(
            f"/messages/{MESSAGE_ID}/attachments/{ATTACHMENT_ID}/$value"
        ):
            return httpx.Response(
                200,
                headers={"Content-Length": "3", "Content-Type": "audio/wav"},
                content=b"wav",
            )
        if request.url.path.endswith("/mailFolders"):
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "id": str(DESTINATION_FOLDER_ID),
                            "displayName": "Processed",
                        }
                    ]
                },
            )
        if request.url.path.endswith(f"/messages/{MESSAGE_ID}"):
            return httpx.Response(
                200,
                json={
                    "id": str(MESSAGE_ID),
                    "parentFolderId": str(DESTINATION_FOLDER_ID),
                },
            )
        pytest.fail(f"unexpected Graph request: {request.method} {request.url}")

    client, http_client, _ = make_client(handler)
    with http_client:
        client.list_messages()
        client.list_attachments(MESSAGE_ID)
        client.download_attachment(MESSAGE_ID, ATTACHMENT_ID, io.BytesIO())
        client.ensure_mail_folder("Processed")
        client.ensure_message_in_folder(MESSAGE_ID, DESTINATION_FOLDER_ID)

    assert len(requests) == 5
    assert all(
        request.headers["prefer"] == IMMUTABLE_PREFERENCE for request in requests
    )


def test_request_correlation_id_is_sent_and_logged_without_remote_identifiers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    request_id = UUID("9d87702e-1a57-4648-af72-9f6e3a9f75ad")
    captured_request: httpx.Request | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_request
        captured_request = request
        return httpx.Response(200, json={"value": []})

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = GraphClient(
        mailbox_id=MAILBOX_ID,
        http_client=http_client,
        token_provider=StubTokenProvider(),
        timeout=TIMEOUT_SECONDS,
        base_url=BASE_URL,
        request_id_factory=lambda: request_id,
    )

    with (
        caplog.at_level(
            logging.DEBUG,
            logger="mevo2glpi_worker_publisher.graph.client",
        ),
        http_client,
    ):
        client.list_messages()

    assert captured_request is not None
    assert captured_request.headers["client-request-id"] == str(request_id)
    assert captured_request.headers["return-client-request-id"] == "true"
    assert f"client_request_id={request_id}" in caplog.text
    assert str(MAILBOX_ID) not in caplog.text


def test_list_attachments_keeps_only_typed_metadata() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "value": [
                    {
                        "@odata.type": "#microsoft.graph.fileAttachment",
                        "id": str(ATTACHMENT_ID),
                        "name": "message.wav",
                        "contentType": "audio/wav",
                        "size": 1240,
                        "isInline": False,
                        "contentBytes": "must-not-be-retained-by-the-model",
                    },
                    {
                        "@odata.type": "#microsoft.graph.itemAttachment",
                        "id": "item-attachment",
                        "name": "nested-message.eml",
                        "contentType": "message/rfc822",
                        "size": 90,
                        "isInline": False,
                    },
                ]
            },
        )

    client, http_client, _ = make_client(handler)
    with http_client:
        attachments = client.list_attachments(MESSAGE_ID)

    assert len(attachments) == 2
    assert attachments[0].attachment_id == ATTACHMENT_ID
    assert attachments[0].name == "message.wav"
    assert attachments[0].content_type == "audio/wav"
    assert attachments[0].size_bytes == 1240
    assert attachments[0].kind is GraphAttachmentKind.FILE
    assert attachments[1].kind is GraphAttachmentKind.ITEM
    assert "contentBytes" not in repr(attachments[0])


def test_download_stream_accepts_the_exact_configured_limit() -> None:
    stream = TrackingStream([b"1234", b"5678", b"90"])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "audio/wav"},
            stream=stream,
        )

    sink = io.BytesIO()
    client, http_client, _ = make_client(handler, max_attachment_bytes=10)
    with http_client:
        result = client.download_attachment(MESSAGE_ID, ATTACHMENT_ID, sink)

    assert result.size_bytes == 10
    assert sink.getvalue() == b"1234567890"
    assert stream.yield_count == 3


def test_download_stream_accepts_exactly_the_default_25_mib_limit() -> None:
    assert DEFAULT_MAX_ATTACHMENT_BYTES == 25 * 1024 * 1024
    full_chunk = b"x" * DOWNLOAD_CHUNK_SIZE
    stream = TrackingStream(
        [full_chunk] * (DEFAULT_MAX_ATTACHMENT_BYTES // DOWNLOAD_CHUNK_SIZE)
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "audio/wav"},
            stream=stream,
        )

    sink = CountingSink()
    client, http_client, _ = make_client(handler)
    with http_client:
        result = client.download_attachment(  # type: ignore[arg-type]
            MESSAGE_ID,
            ATTACHMENT_ID,
            sink,
        )

    assert result.size_bytes == DEFAULT_MAX_ATTACHMENT_BYTES
    assert sink.size_bytes == DEFAULT_MAX_ATTACHMENT_BYTES


def test_download_rejects_content_length_above_limit_before_reading_body() -> None:
    stream = TrackingStream([b"sensitive-body-must-not-be-read"])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Length": "11", "Content-Type": "audio/wav"},
            stream=stream,
        )

    sink = io.BytesIO()
    client, http_client, _ = make_client(handler, max_attachment_bytes=10)
    with http_client, pytest.raises(GraphPayloadTooLargeError) as captured_error:
        client.download_attachment(MESSAGE_ID, ATTACHMENT_ID, sink)

    assert captured_error.value.maximum_bytes == 10
    assert captured_error.value.observed_bytes == 11
    assert sink.getvalue() == b""
    assert stream.yield_count == 0


def test_download_never_writes_a_chunk_that_would_cross_the_limit() -> None:
    stream = TrackingStream([b"123456", b"78901"])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "audio/wav"},
            stream=stream,
        )

    sink = io.BytesIO()
    client, http_client, _ = make_client(handler, max_attachment_bytes=10)
    with http_client, pytest.raises(GraphPayloadTooLargeError) as captured_error:
        client.download_attachment(MESSAGE_ID, ATTACHMENT_ID, sink)

    assert captured_error.value.observed_bytes == 11
    assert sink.getvalue() in {b"", b"123456"}
    assert len(sink.getvalue()) <= 10


def test_ensure_mail_folder_is_idempotent() -> None:
    folder_id: str | None = None
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal folder_id
        methods.append(request.method)
        if request.method == "GET":
            value = (
                []
                if folder_id is None
                else [{"id": folder_id, "displayName": "Processed"}]
            )
            return httpx.Response(200, json={"value": value})
        assert request.method == "POST"
        assert json.loads(request.content) == {"displayName": "Processed"}
        folder_id = str(DESTINATION_FOLDER_ID)
        return httpx.Response(
            201,
            json={"id": folder_id, "displayName": "Processed"},
        )

    client, http_client, _ = make_client(handler)
    with http_client:
        created = client.ensure_mail_folder("Processed")
        found = client.ensure_mail_folder("Processed")

    assert created == found
    assert created.folder_id == DESTINATION_FOLDER_ID
    assert methods == ["GET", "POST", "GET"]


def test_move_is_a_no_op_when_message_is_already_in_destination() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": str(MESSAGE_ID),
                "parentFolderId": str(DESTINATION_FOLDER_ID),
            },
        )

    client, http_client, _ = make_client(handler)
    with http_client:
        result = client.ensure_message_in_folder(MESSAGE_ID, DESTINATION_FOLDER_ID)

    assert result.message_id == MESSAGE_ID
    assert result.parent_folder_id == DESTINATION_FOLDER_ID
    assert result.outcome is MoveOutcome.ALREADY_IN_DESTINATION
    assert [request.method for request in requests] == ["GET"]


def test_lost_move_response_is_classified_as_an_unknown_outcome() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "id": str(MESSAGE_ID),
                    "parentFolderId": str(SOURCE_FOLDER_ID),
                },
            )
        raise httpx.ReadError("synthetic lost response", request=request)

    client, http_client, _ = make_client(handler)
    with http_client, pytest.raises(GraphOutcomeUnknownError) as captured_error:
        client.ensure_message_in_folder(MESSAGE_ID, DESTINATION_FOLDER_ID)

    error = captured_error.value
    assert error.operation is GraphOperation.MOVE_MESSAGE
    assert error.category is GraphErrorCategory.UNAVAILABLE
    assert error.technical_code == "transport_error"
    assert error.retryable is True
    assert error.outcome_unknown is True


def test_invalid_move_reconciliation_preserves_the_unknown_remote_outcome() -> None:
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        if request_count == 1:
            return httpx.Response(
                200,
                json={
                    "id": str(MESSAGE_ID),
                    "parentFolderId": str(SOURCE_FOLDER_ID),
                },
            )
        if request_count == 2:
            raise httpx.ReadError("synthetic lost move response", request=request)
        return httpx.Response(200, text="not-json")

    client, http_client, _ = make_client(handler)
    with http_client, pytest.raises(GraphOutcomeUnknownError) as captured_error:
        client.ensure_message_in_folder(MESSAGE_ID, DESTINATION_FOLDER_ID)

    error = captured_error.value
    assert error.operation is GraphOperation.MOVE_MESSAGE
    assert error.technical_code == "transport_error"
    assert error.retryable is True
    assert error.outcome_unknown is True


def test_invalid_folder_reconciliation_preserves_the_unknown_remote_outcome() -> None:
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        if request_count == 1:
            return httpx.Response(200, json={"value": []})
        if request_count == 2:
            raise httpx.ReadError("synthetic lost create response", request=request)
        return httpx.Response(200, text="not-json")

    client, http_client, _ = make_client(handler)
    with http_client, pytest.raises(GraphOutcomeUnknownError) as captured_error:
        client.ensure_mail_folder("Processed")

    error = captured_error.value
    assert error.operation is GraphOperation.CREATE_FOLDER
    assert error.technical_code == "transport_error"
    assert error.retryable is True
    assert error.outcome_unknown is True


def test_http_401_invalidates_the_token_once_and_replays_with_a_fresh_token() -> None:
    authorizations: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        authorizations.append(request.headers["authorization"])
        if len(authorizations) == 1:
            return httpx.Response(401, json={"error": {"code": "InvalidToken"}})
        return httpx.Response(200, json={"value": []})

    client, http_client, token_provider = make_client(handler)
    with http_client:
        assert client.list_messages() == ()

    assert token_provider.invalidations == 1
    assert token_provider.access_calls == 2
    assert authorizations == [
        f"Bearer {ACCESS_TOKEN}",
        f"Bearer {SECOND_ACCESS_TOKEN}",
    ]


@pytest.mark.parametrize(
    (
        "status_code",
        "expected_category",
        "retryable",
        "expected_retry_after",
    ),
    [
        (400, GraphErrorCategory.INVALID_REQUEST, False, None),
        (403, GraphErrorCategory.AUTHORIZATION, False, None),
        (404, GraphErrorCategory.NOT_FOUND, False, None),
        (409, GraphErrorCategory.CONFLICT, True, None),
        (410, GraphErrorCategory.UNAVAILABLE, True, None),
        (412, GraphErrorCategory.CONFLICT, True, None),
        (423, GraphErrorCategory.UNAVAILABLE, True, None),
        (408, GraphErrorCategory.UNAVAILABLE, True, None),
        (429, GraphErrorCategory.THROTTLED, True, 17.0),
        (500, GraphErrorCategory.UNAVAILABLE, True, None),
        (503, GraphErrorCategory.UNAVAILABLE, True, None),
        (507, GraphErrorCategory.UNAVAILABLE, True, None),
        (509, GraphErrorCategory.THROTTLED, True, 17.0),
    ],
)
def test_http_failures_have_sanitized_stable_classification(
    status_code: int,
    expected_category: GraphErrorCategory,
    retryable: bool,
    expected_retry_after: float | None,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        headers = {"Retry-After": "17"} if status_code in {429, 509} else {}
        return httpx.Response(
            status_code,
            headers=headers,
            text="sensitive remote Graph response",
        )

    client, http_client, _ = make_client(handler)
    with http_client, pytest.raises(GraphHttpError) as captured_error:
        client.list_messages()

    error = captured_error.value
    assert error.status_code == status_code
    assert error.category is expected_category
    assert error.retryable is retryable
    assert error.outcome_unknown is False
    assert error.retry_after_seconds == expected_retry_after
    assert "sensitive remote Graph response" not in str(error)


def test_retry_after_http_date_uses_the_injected_utc_clock() -> None:
    now = datetime(2026, 9, 8, 7, 30, tzinfo=UTC)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"Retry-After": "Tue, 08 Sep 2026 07:30:17 GMT"},
        )

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = GraphClient(
        mailbox_id=MAILBOX_ID,
        http_client=http_client,
        token_provider=StubTokenProvider(),
        timeout=TIMEOUT_SECONDS,
        base_url=BASE_URL,
        utcnow=lambda: now,
    )

    with http_client, pytest.raises(GraphHttpError) as captured_error:
        client.list_messages()

    assert captured_error.value.retry_after_seconds == 17.0


def test_non_finite_retry_after_is_ignored() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "9" * 400})

    client, http_client, _ = make_client(handler)

    with http_client, pytest.raises(GraphHttpError) as captured_error:
        client.list_messages()

    assert captured_error.value.retry_after_seconds is None


def test_transport_diagnostic_never_logs_token_or_exception_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sensitive_error = "contains-sensitive-mail-metadata"

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(sensitive_error, request=request)

    client, http_client, _ = make_client(handler)
    with (
        caplog.at_level(
            logging.DEBUG,
            logger="mevo2glpi_worker_publisher.graph.client",
        ),
        http_client,
        pytest.raises(GraphTransportError),
    ):
        client.list_messages()

    assert "ConnectError" in caplog.text
    assert "client_request_id=" in caplog.text
    assert ACCESS_TOKEN not in caplog.text
    assert sensitive_error not in caplog.text
