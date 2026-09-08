"""Small stateful Microsoft Graph fake used only by publisher tests.

The fake deliberately models the two properties that static response mocks cannot
express reliably: a message keeps its immutable identifier when it moves, and a
move may be applied even when the HTTP response is lost.
"""

from __future__ import annotations

import json
import socket
import threading
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit
from uuid import UUID

IMMUTABLE_ID_PREFERENCE = 'IdType="ImmutableId"'


@dataclass(frozen=True, slots=True)
class RecordedRequest:
    """Sanitized request metadata retained by the local fake."""

    method: str
    path: str
    query: dict[str, list[str]]
    headers: dict[str, str]
    body: bytes


@dataclass(slots=True)
class FakeAttachment:
    attachment_id: str
    name: str
    content_type: str
    content: bytes
    is_inline: bool = False


@dataclass(slots=True)
class FakeMessage:
    message_id: str
    parent_folder_id: str
    received_at: str = "2026-09-08T07:30:00Z"
    attachments: list[FakeAttachment] = field(default_factory=list)


@dataclass(slots=True)
class FakeGraphState:
    """Mutable Exchange-like state shared with one HTTP server."""

    mailbox_id: str
    folders: dict[str, str] = field(
        default_factory=lambda: {"inbox": "Inbox"},
    )
    messages: dict[str, FakeMessage] = field(default_factory=dict)
    requests: list[RecordedRequest] = field(default_factory=list)
    public_base_url: str = ""
    lose_next_move_response: bool = False
    lose_reconciliation_response_after_move: bool = False
    _lose_next_message_lookup_response: bool = False
    download_chunk_size: int = 64 * 1024
    _folder_sequence: int = 0
    client_request_ids: set[UUID] = field(default_factory=set)

    def add_message(
        self,
        message_id: str,
        *,
        folder_id: str = "inbox",
        attachments: list[FakeAttachment] | None = None,
    ) -> FakeMessage:
        message = FakeMessage(
            message_id=message_id,
            parent_folder_id=folder_id,
            attachments=list(attachments or ()),
        )
        self.messages[message_id] = message
        return message

    def folder_id_for_name(self, display_name: str) -> str | None:
        return next(
            (
                folder_id
                for folder_id, current_name in self.folders.items()
                if current_name == display_name
            ),
            None,
        )

    def create_folder(self, display_name: str) -> str:
        existing = self.folder_id_for_name(display_name)
        if existing is not None:
            return existing
        self._folder_sequence += 1
        folder_id = f"folder-{self._folder_sequence}"
        self.folders[folder_id] = display_name
        return folder_id


class FakeGraphServer:
    """Context-managed loopback server exposing a narrow Graph v1.0 surface."""

    def __init__(self, state: FakeGraphState) -> None:
        self.state = state
        handler = _handler_for(state)
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        host, port = self._server.server_address
        state.public_base_url = f"http://{host}:{port}/v1.0"
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="fake-graph-server",
            daemon=True,
        )

    @property
    def base_url(self) -> str:
        return self.state.public_base_url

    def __enter__(self) -> FakeGraphServer:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def _handler_for(state: FakeGraphState) -> type[BaseHTTPRequestHandler]:
    class FakeGraphHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
            self._dispatch()

        def log_message(self, _format: str, *args: object) -> None:
            del args

        def _dispatch(self) -> None:
            parsed = urlsplit(self.path)
            body = self._read_body()
            request = RecordedRequest(
                method=self.command,
                path=unquote(parsed.path),
                query=parse_qs(parsed.query),
                headers={name.lower(): value for name, value in self.headers.items()},
                body=body,
            )
            state.requests.append(request)

            raw_client_request_id = request.headers.get("client-request-id")
            try:
                client_request_id = UUID(raw_client_request_id or "")
            except ValueError:
                self._json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": {"code": "MissingClientRequestId"}},
                )
                return
            if (
                client_request_id in state.client_request_ids
                or request.headers.get("return-client-request-id") != "true"
            ):
                self._json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": {"code": "InvalidClientRequestId"}},
                )
                return
            state.client_request_ids.add(client_request_id)

            if request.headers.get("prefer") != IMMUTABLE_ID_PREFERENCE:
                self._json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": {"code": "MissingImmutableIdPreference"}},
                )
                return
            if request.headers.get("authorization") != "Bearer synthetic-token":
                self._json(
                    HTTPStatus.UNAUTHORIZED,
                    {"error": {"code": "InvalidAuthenticationToken"}},
                )
                return

            segments = request.path.removeprefix("/v1.0/").split("/")
            if len(segments) < 3 or segments[:2] != ["users", state.mailbox_id]:
                self._json(HTTPStatus.NOT_FOUND, {"error": {"code": "NotFound"}})
                return

            resource = segments[2:]
            if self.command == "GET" and resource == [
                "mailFolders",
                "inbox",
                "messages",
            ]:
                self._list_inbox_messages(request)
                return
            if (
                self.command == "GET"
                and len(resource) == 2
                and resource[0] == "messages"
            ):
                self._get_message(resource[1])
                return
            if (
                self.command == "GET"
                and len(resource) == 3
                and resource[0] == "messages"
                and resource[2] == "attachments"
            ):
                self._list_attachments(resource[1])
                return
            if (
                self.command == "GET"
                and len(resource) == 5
                and resource[0] == "messages"
                and resource[2] == "attachments"
                and resource[4] == "$value"
            ):
                self._download_attachment(resource[1], resource[3])
                return
            if self.command == "GET" and resource == ["mailFolders"]:
                self._list_folders(request)
                return
            if self.command == "POST" and resource == ["mailFolders"]:
                self._create_folder(body)
                return
            if (
                self.command == "POST"
                and len(resource) == 3
                and resource[0] == "messages"
                and resource[2] == "move"
            ):
                self._move_message(resource[1], body)
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": {"code": "NotFound"}})

        def _list_inbox_messages(self, request: RecordedRequest) -> None:
            try:
                page_size = int(request.query.get("$top", ["50"])[0])
                offset = int(request.query.get("$skiptoken", ["0"])[0])
            except ValueError:
                self._json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": {"code": "InvalidQuery"}},
                )
                return
            messages = sorted(
                (
                    message
                    for message in state.messages.values()
                    if message.parent_folder_id == "inbox"
                ),
                key=lambda message: message.message_id,
            )
            page = messages[offset : offset + page_size]
            payload: dict[str, object] = {
                "value": [
                    {
                        "id": message.message_id,
                        "parentFolderId": message.parent_folder_id,
                        "receivedDateTime": message.received_at,
                        "hasAttachments": bool(message.attachments),
                    }
                    for message in page
                ]
            }
            next_offset = offset + len(page)
            if next_offset < len(messages):
                payload["@odata.nextLink"] = (
                    f"{state.public_base_url}/users/{state.mailbox_id}"
                    "/mailFolders/inbox/messages"
                    f"?$top={page_size}&$skiptoken={next_offset}"
                )
            self._json(HTTPStatus.OK, payload)

        def _get_message(self, message_id: str) -> None:
            if state._lose_next_message_lookup_response:
                state._lose_next_message_lookup_response = False
                self._drop_response()
                return
            message = state.messages.get(message_id)
            if message is None:
                self._json(HTTPStatus.NOT_FOUND, {"error": {"code": "NotFound"}})
                return
            self._json(
                HTTPStatus.OK,
                {
                    "id": message.message_id,
                    "parentFolderId": message.parent_folder_id,
                },
            )

        def _list_attachments(self, message_id: str) -> None:
            message = state.messages.get(message_id)
            if message is None:
                self._json(HTTPStatus.NOT_FOUND, {"error": {"code": "NotFound"}})
                return
            self._json(
                HTTPStatus.OK,
                {
                    "value": [
                        {
                            "@odata.type": "#microsoft.graph.fileAttachment",
                            "id": attachment.attachment_id,
                            "name": attachment.name,
                            "contentType": attachment.content_type,
                            "size": len(attachment.content),
                            "isInline": attachment.is_inline,
                        }
                        for attachment in message.attachments
                    ]
                },
            )

        def _download_attachment(self, message_id: str, attachment_id: str) -> None:
            message = state.messages.get(message_id)
            attachment = next(
                (
                    current
                    for current in (message.attachments if message else ())
                    if current.attachment_id == attachment_id
                ),
                None,
            )
            if attachment is None:
                self._json(HTTPStatus.NOT_FOUND, {"error": {"code": "NotFound"}})
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", attachment.content_type)
            self.send_header("Content-Length", str(len(attachment.content)))
            self.end_headers()
            for offset in range(0, len(attachment.content), state.download_chunk_size):
                self.wfile.write(
                    attachment.content[offset : offset + state.download_chunk_size]
                )
                self.wfile.flush()

        def _list_folders(self, request: RecordedRequest) -> None:
            display_name = _display_name_filter(request.query.get("$filter", [""])[0])
            try:
                page_size = int(request.query.get("$top", ["50"])[0])
                offset = int(request.query.get("$skiptoken", ["0"])[0])
            except ValueError:
                self._json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": {"code": "InvalidQuery"}},
                )
                return
            folders = [
                {"id": folder_id, "displayName": current_name}
                for folder_id, current_name in state.folders.items()
                if display_name is None or current_name == display_name
            ]
            folders.sort(key=lambda folder: str(folder["id"]))
            page = folders[offset : offset + page_size]
            payload: dict[str, object] = {"value": page}
            next_offset = offset + len(page)
            if next_offset < len(folders):
                query = f"$top={page_size}&$skiptoken={next_offset}"
                if display_name is not None:
                    escaped_name = display_name.replace("'", "''")
                    query += f"&$filter=displayName%20eq%20%27{escaped_name}%27"
                payload["@odata.nextLink"] = (
                    f"{state.public_base_url}/users/{state.mailbox_id}/mailFolders"
                    f"?{query}"
                )
            self._json(HTTPStatus.OK, payload)

        def _create_folder(self, body: bytes) -> None:
            payload = _object_json(body)
            display_name = payload.get("displayName")
            if not isinstance(display_name, str) or not display_name:
                self._json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": {"code": "InvalidRequest"}},
                )
                return
            folder_id = state.create_folder(display_name)
            self._json(
                HTTPStatus.CREATED,
                {"id": folder_id, "displayName": display_name},
            )

        def _move_message(self, message_id: str, body: bytes) -> None:
            message = state.messages.get(message_id)
            payload = _object_json(body)
            destination_id = payload.get("destinationId")
            if message is None or destination_id not in state.folders:
                self._json(HTTPStatus.NOT_FOUND, {"error": {"code": "NotFound"}})
                return
            message.parent_folder_id = str(destination_id)
            if state.lose_next_move_response:
                state.lose_next_move_response = False
                state._lose_next_message_lookup_response = (
                    state.lose_reconciliation_response_after_move
                )
                self._drop_response()
                return
            self._json(
                HTTPStatus.CREATED,
                {
                    "id": message.message_id,
                    "parentFolderId": message.parent_folder_id,
                },
            )

        def _drop_response(self) -> None:
            self.close_connection = True
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.connection.close()

        def _read_body(self) -> bytes:
            length = int(self.headers.get("Content-Length", "0"))
            return self.rfile.read(length) if length else b""

        def _json(self, status: HTTPStatus, payload: object) -> None:
            encoded = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    return FakeGraphHandler


def _object_json(content: bytes) -> dict[str, object]:
    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _display_name_filter(expression: str) -> str | None:
    prefix = "displayName eq '"
    if expression.startswith(prefix) and expression.endswith("'"):
        return expression[len(prefix) : -1].replace("''", "'")
    return None


__all__ = [
    "FakeAttachment",
    "FakeGraphServer",
    "FakeGraphState",
    "FakeMessage",
    "IMMUTABLE_ID_PREFERENCE",
    "RecordedRequest",
]
