"""Synchronous adapter for the pinned Microsoft Graph v1.0 mail contract."""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import BinaryIO, TypeVar
from urllib.parse import quote
from uuid import UUID, uuid4

import httpx2

from .contracts import (
    GraphClientError,
    GraphHttpError,
    GraphOutcomeUnknownError,
    GraphPayloadTooLargeError,
    GraphProtocolError,
    GraphTokenProviderProtocol,
    GraphTransportError,
)
from .models import (
    AttachmentDownload,
    AttachmentId,
    FolderId,
    GraphAttachment,
    GraphAttachmentKind,
    GraphErrorCategory,
    GraphMailFolder,
    GraphMessage,
    GraphOperation,
    MessageId,
    MessageMoveResult,
    MoveOutcome,
)

LOGGER = logging.getLogger(__name__)

GRAPH_API_VERSION = "v1.0"
GRAPH_API_BASE_URL = f"https://graph.microsoft.com/{GRAPH_API_VERSION}"
IMMUTABLE_ID_PREFERENCE = 'IdType="ImmutableId"'
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 1_000
MAX_PAGINATION_PAGES = 1_000
DEFAULT_MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
DOWNLOAD_CHUNK_SIZE = 64 * 1024
RETRYABLE_HTTP_STATUSES = frozenset({408, 425, 429})

_ItemT = TypeVar("_ItemT")


def _utcnow() -> datetime:
    return datetime.now(UTC)


class GraphClient:
    """Read and acknowledge one mailbox through Microsoft Graph v1.0."""

    def __init__(
        self,
        *,
        mailbox_id: UUID,
        http_client: httpx2.Client,
        token_provider: GraphTokenProviderProtocol,
        timeout: float | httpx2.Timeout,
        base_url: str | httpx2.URL = GRAPH_API_BASE_URL,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_attachment_bytes: int = DEFAULT_MAX_ATTACHMENT_BYTES,
        utcnow: Callable[[], datetime] = _utcnow,
        request_id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        if not isinstance(mailbox_id, UUID):
            raise TypeError("mailbox_id must be a UUID")
        if not callable(getattr(http_client, "request", None)) or not callable(
            getattr(http_client, "stream", None)
        ):
            raise TypeError("http_client must expose request and stream")
        if not callable(getattr(token_provider, "get_access_token", None)):
            raise TypeError("token_provider must expose get_access_token")
        if not callable(getattr(token_provider, "invalidate", None)):
            raise TypeError("token_provider must expose invalidate")
        if (
            isinstance(page_size, bool)
            or not isinstance(page_size, int)
            or not 1 <= page_size <= MAX_PAGE_SIZE
        ):
            raise ValueError(f"page_size must be between 1 and {MAX_PAGE_SIZE}")
        if (
            isinstance(max_attachment_bytes, bool)
            or not isinstance(max_attachment_bytes, int)
            or max_attachment_bytes <= 0
        ):
            raise ValueError("max_attachment_bytes must be a positive integer")
        if not callable(utcnow):
            raise TypeError("utcnow must be callable")
        if not callable(request_id_factory):
            raise TypeError("request_id_factory must be callable")
        self._mailbox_id = mailbox_id
        self._http_client = http_client
        self._token_provider = token_provider
        self._timeout = _validate_timeout(timeout)
        self._base_url = _validate_base_url(base_url)
        self._page_size = page_size
        self._max_attachment_bytes = max_attachment_bytes
        self._utcnow = utcnow
        self._request_id_factory = request_id_factory

    def list_messages(self) -> tuple[GraphMessage, ...]:
        """Return every Inbox message, oldest first, following safe next links."""
        return self._collect_pages(
            initial_url=self._url(
                "users",
                str(self._mailbox_id),
                "mailFolders",
                "inbox",
                "messages",
            ),
            initial_params={
                "$select": "id,parentFolderId,receivedDateTime,hasAttachments",
                "$orderby": "receivedDateTime asc",
                "$top": str(self._page_size),
            },
            operation=GraphOperation.LIST_MESSAGES,
            item_parser=lambda item: _parse_message(
                item,
                GraphOperation.LIST_MESSAGES,
            ),
        )

    def list_attachments(
        self,
        message_id: MessageId,
    ) -> tuple[GraphAttachment, ...]:
        """Return metadata only; attachment bytes are fetched separately."""
        message_id = MessageId(_validate_identifier(message_id, "message_id"))
        return self._collect_pages(
            initial_url=self._url(
                "users",
                str(self._mailbox_id),
                "messages",
                message_id,
                "attachments",
            ),
            initial_params={
                "$select": "id,name,contentType,size,isInline",
                "$top": str(self._page_size),
            },
            operation=GraphOperation.LIST_ATTACHMENTS,
            item_parser=lambda item: _parse_attachment(
                item,
                GraphOperation.LIST_ATTACHMENTS,
            ),
        )

    def download_attachment(
        self,
        message_id: MessageId,
        attachment_id: AttachmentId,
        sink: BinaryIO,
    ) -> AttachmentDownload:
        """Stream an attachment without ever writing past the configured limit."""
        message_id = MessageId(_validate_identifier(message_id, "message_id"))
        attachment_id = AttachmentId(
            _validate_identifier(attachment_id, "attachment_id")
        )
        if not callable(getattr(sink, "write", None)):
            raise TypeError("sink must expose write")

        url = self._url(
            "users",
            str(self._mailbox_id),
            "messages",
            message_id,
            "attachments",
            attachment_id,
            "$value",
        )
        operation = GraphOperation.DOWNLOAD_ATTACHMENT

        for authentication_attempt in range(2):
            token = self._token_provider.get_access_token()
            headers = self._headers(
                token.value.get_secret_value(),
                accept="application/octet-stream",
            )
            self._log_request(operation, headers["client-request-id"])
            try:
                with self._http_client.stream(
                    "GET",
                    url,
                    headers=headers,
                    timeout=self._timeout,
                    follow_redirects=False,
                ) as response:
                    if (
                        response.status_code == httpx2.codes.UNAUTHORIZED
                        and authentication_attempt == 0
                    ):
                        self._token_provider.invalidate()
                        continue
                    _require_status(
                        response,
                        operation,
                        httpx2.codes.OK,
                        now=self._read_utcnow(),
                    )
                    declared_size = _read_content_length(response, operation)
                    if (
                        declared_size is not None
                        and declared_size > self._max_attachment_bytes
                    ):
                        raise GraphPayloadTooLargeError(
                            maximum_bytes=self._max_attachment_bytes,
                            observed_bytes=declared_size,
                        )

                    downloaded_size = 0
                    for chunk in response.iter_bytes(chunk_size=DOWNLOAD_CHUNK_SIZE):
                        if not chunk:
                            continue
                        next_size = downloaded_size + len(chunk)
                        if next_size > self._max_attachment_bytes:
                            raise GraphPayloadTooLargeError(
                                maximum_bytes=self._max_attachment_bytes,
                                observed_bytes=next_size,
                            )
                        _write_all(sink, chunk)
                        downloaded_size = next_size
                    return AttachmentDownload(size_bytes=downloaded_size)
            except httpx2.RequestError as error:
                self._log_transport_error(
                    operation,
                    error,
                    client_request_id=headers["client-request-id"],
                )
                raise GraphTransportError(operation) from None

        raise AssertionError("authentication retry loop must return or raise")

    def ensure_mail_folder(self, display_name: str) -> GraphMailFolder:
        """Find or create a top-level folder and reconcile uncertain creation."""
        display_name = _validate_folder_name(display_name)
        existing_folder = self._find_mail_folder(display_name)
        if existing_folder is not None:
            return existing_folder

        operation = GraphOperation.CREATE_FOLDER
        try:
            response = self._request(
                "POST",
                self._url("users", str(self._mailbox_id), "mailFolders"),
                operation=operation,
                json={"displayName": display_name},
                outcome_unknown_on_transport=True,
            )
            if response.status_code == httpx2.codes.CONFLICT:
                raise _http_error(
                    operation,
                    response,
                    outcome_unknown=False,
                    now=self._read_utcnow(),
                )
            _require_status(
                response,
                operation,
                httpx2.codes.CREATED,
                outcome_unknown=True,
                now=self._read_utcnow(),
            )
            try:
                created_folder = _parse_folder(
                    _decode_object(response, operation),
                    operation,
                )
            except GraphProtocolError:
                raise GraphOutcomeUnknownError(
                    operation=operation,
                    category=GraphErrorCategory.PROTOCOL,
                    technical_code="invalid_create_folder_response",
                ) from None
            if created_folder.display_name.casefold() != display_name.casefold():
                raise GraphOutcomeUnknownError(
                    operation=operation,
                    category=GraphErrorCategory.PROTOCOL,
                    technical_code="mismatched_created_folder",
                )
            return created_folder
        except GraphHttpError as error:
            if error.status_code != httpx2.codes.CONFLICT:
                raise
            return self._reconcile_folder(display_name, error)
        except GraphOutcomeUnknownError as error:
            return self._reconcile_folder(display_name, error)

    def ensure_message_in_folder(
        self,
        message_id: MessageId,
        destination_folder_id: FolderId,
    ) -> MessageMoveResult:
        """Move once, or confirm a prior move through the immutable identifier."""
        message_id = MessageId(_validate_identifier(message_id, "message_id"))
        destination_folder_id = FolderId(
            _validate_identifier(destination_folder_id, "destination_folder_id")
        )
        current_location = self._get_message_location(message_id)
        if current_location is None:
            raise GraphHttpError(
                operation=GraphOperation.MOVE_MESSAGE,
                category=GraphErrorCategory.NOT_FOUND,
                status_code=httpx2.codes.NOT_FOUND,
                retryable=False,
            )
        if current_location.parent_folder_id == destination_folder_id:
            return MessageMoveResult(
                message_id=current_location.message_id,
                parent_folder_id=current_location.parent_folder_id,
                outcome=MoveOutcome.ALREADY_IN_DESTINATION,
            )

        operation = GraphOperation.MOVE_MESSAGE
        try:
            response = self._request(
                "POST",
                self._url(
                    "users",
                    str(self._mailbox_id),
                    "messages",
                    message_id,
                    "move",
                ),
                operation=operation,
                json={"destinationId": destination_folder_id},
                outcome_unknown_on_transport=True,
            )
            if response.status_code in {
                httpx2.codes.NOT_FOUND,
                httpx2.codes.CONFLICT,
            }:
                raise _http_error(
                    operation,
                    response,
                    outcome_unknown=False,
                    now=self._read_utcnow(),
                )
            _require_status(
                response,
                operation,
                httpx2.codes.CREATED,
                outcome_unknown=True,
                now=self._read_utcnow(),
            )
            try:
                moved = _parse_message_location(
                    _decode_object(response, operation),
                    operation,
                )
            except GraphProtocolError:
                raise GraphOutcomeUnknownError(
                    operation=operation,
                    category=GraphErrorCategory.PROTOCOL,
                    technical_code="invalid_move_response",
                ) from None
            if (
                moved.message_id != message_id
                or moved.parent_folder_id != destination_folder_id
            ):
                raise GraphOutcomeUnknownError(
                    operation=operation,
                    category=GraphErrorCategory.PROTOCOL,
                    technical_code="mismatched_move_response",
                )
            return moved
        except GraphHttpError as error:
            if error.status_code not in {
                httpx2.codes.NOT_FOUND,
                httpx2.codes.CONFLICT,
            }:
                raise
            return self._reconcile_move(message_id, destination_folder_id, error)
        except GraphOutcomeUnknownError as error:
            return self._reconcile_move(message_id, destination_folder_id, error)

    def _collect_pages(
        self,
        *,
        initial_url: httpx2.URL,
        initial_params: Mapping[str, str],
        operation: GraphOperation,
        item_parser: Callable[[Mapping[str, object]], _ItemT],
    ) -> tuple[_ItemT, ...]:
        items: list[_ItemT] = []
        next_url: httpx2.URL | None = initial_url
        params: Mapping[str, str] | None = initial_params
        seen_urls: set[str] = set()

        while next_url is not None:
            page_number = len(seen_urls) + 1
            if page_number > MAX_PAGINATION_PAGES:
                raise GraphProtocolError(operation, "pagination_limit_exceeded")
            normalized_url = str(next_url)
            if normalized_url in seen_urls:
                raise GraphProtocolError(operation, "pagination_cycle")
            seen_urls.add(normalized_url)

            response = self._request(
                "GET",
                next_url,
                operation=operation,
                params=params,
            )
            _require_status(
                response,
                operation,
                httpx2.codes.OK,
                now=self._read_utcnow(),
            )
            payload = _decode_object(response, operation)
            raw_items = payload.get("value")
            if not isinstance(raw_items, list):
                raise GraphProtocolError(operation, "invalid_page_items")
            for raw_item in raw_items:
                if not isinstance(raw_item, Mapping):
                    raise GraphProtocolError(operation, "invalid_page_item")
                items.append(item_parser(raw_item))

            next_url = self._read_next_link(payload, operation)
            params = None

        return tuple(items)

    def _read_next_link(
        self,
        payload: Mapping[str, object],
        operation: GraphOperation,
    ) -> httpx2.URL | None:
        raw_next_link = payload.get("@odata.nextLink")
        if raw_next_link is None:
            return None
        if not isinstance(raw_next_link, str) or not raw_next_link:
            raise GraphProtocolError(operation, "invalid_next_link")
        try:
            next_url = httpx2.URL(raw_next_link)
        except (TypeError, ValueError):
            raise GraphProtocolError(operation, "invalid_next_link") from None
        if not _is_safe_next_link(next_url, self._base_url):
            raise GraphProtocolError(operation, "unsafe_next_link")
        return next_url

    def _find_mail_folder(self, display_name: str) -> GraphMailFolder | None:
        folders = self._collect_pages(
            initial_url=self._url("users", str(self._mailbox_id), "mailFolders"),
            initial_params={
                "$select": "id,displayName",
                "$top": str(self._page_size),
                "includeHiddenFolders": "false",
            },
            operation=GraphOperation.LIST_FOLDERS,
            item_parser=lambda item: _parse_folder(
                item,
                GraphOperation.LIST_FOLDERS,
            ),
        )
        matches = tuple(
            folder
            for folder in folders
            if folder.display_name.casefold() == display_name.casefold()
        )
        if len(matches) > 1:
            raise GraphProtocolError(
                GraphOperation.LIST_FOLDERS,
                "duplicate_folder_name",
            )
        return matches[0] if matches else None

    def _get_message_location(
        self,
        message_id: MessageId,
    ) -> MessageMoveResult | None:
        operation = GraphOperation.GET_MESSAGE
        response = self._request(
            "GET",
            self._url(
                "users",
                str(self._mailbox_id),
                "messages",
                message_id,
            ),
            operation=operation,
            params={"$select": "id,parentFolderId"},
        )
        if response.status_code == httpx2.codes.NOT_FOUND:
            return None
        _require_status(
            response,
            operation,
            httpx2.codes.OK,
            now=self._read_utcnow(),
        )
        return _parse_message_location(
            _decode_object(response, operation),
            operation,
        )

    def _reconcile_folder(
        self,
        display_name: str,
        original_error: GraphClientError,
    ) -> GraphMailFolder:
        try:
            folder = self._find_mail_folder(display_name)
        except GraphClientError as reconciliation_error:
            if original_error.outcome_unknown:
                raise original_error from None
            raise reconciliation_error from None
        if folder is not None:
            return folder
        raise original_error from None

    def _reconcile_move(
        self,
        message_id: MessageId,
        destination_folder_id: FolderId,
        original_error: GraphClientError,
    ) -> MessageMoveResult:
        try:
            current_location = self._get_message_location(message_id)
        except GraphClientError as reconciliation_error:
            if original_error.outcome_unknown:
                raise original_error from None
            raise reconciliation_error from None
        if (
            current_location is not None
            and current_location.parent_folder_id == destination_folder_id
        ):
            return current_location
        raise original_error from None

    def _request(
        self,
        method: str,
        url: httpx2.URL,
        *,
        operation: GraphOperation,
        params: Mapping[str, str] | None = None,
        json: Mapping[str, object] | None = None,
        outcome_unknown_on_transport: bool = False,
    ) -> httpx2.Response:
        for authentication_attempt in range(2):
            token = self._token_provider.get_access_token()
            headers = self._headers(token.value.get_secret_value())
            self._log_request(operation, headers["client-request-id"])
            try:
                response = self._http_client.request(
                    method,
                    url,
                    headers=headers,
                    params=params,
                    json=json,
                    timeout=self._timeout,
                    follow_redirects=False,
                )
            except httpx2.RequestError as error:
                self._log_transport_error(
                    operation,
                    error,
                    client_request_id=headers["client-request-id"],
                )
                if outcome_unknown_on_transport:
                    raise GraphOutcomeUnknownError(
                        operation=operation,
                        category=GraphErrorCategory.UNAVAILABLE,
                        technical_code="transport_error",
                    ) from None
                raise GraphTransportError(operation) from None

            if (
                response.status_code == httpx2.codes.UNAUTHORIZED
                and authentication_attempt == 0
            ):
                self._token_provider.invalidate()
                continue
            return response

        raise AssertionError("authentication retry loop must return or raise")

    def _headers(
        self,
        access_token: str,
        *,
        accept: str = "application/json",
    ) -> dict[str, str]:
        client_request_id = self._request_id_factory()
        if not isinstance(client_request_id, UUID):
            raise TypeError("request_id_factory must return a UUID")
        return {
            "Accept": accept,
            "Authorization": f"Bearer {access_token}",
            "client-request-id": str(client_request_id),
            "Prefer": IMMUTABLE_ID_PREFERENCE,
            "return-client-request-id": "true",
        }

    def _url(self, *path_segments: object) -> httpx2.URL:
        encoded_path = "/".join(
            quote(str(segment), safe="") for segment in path_segments
        )
        return httpx2.URL(f"{str(self._base_url).rstrip('/')}/{encoded_path}")

    def _log_transport_error(
        self,
        operation: GraphOperation,
        error: httpx2.RequestError,
        *,
        client_request_id: str,
    ) -> None:
        LOGGER.debug(
            "Microsoft Graph transport diagnostic operation=%s host=%s "
            "client_request_id=%s error_type=%s",
            operation.value,
            self._base_url.host,
            client_request_id,
            type(error).__name__,
        )

    def _log_request(
        self,
        operation: GraphOperation,
        client_request_id: str,
    ) -> None:
        LOGGER.info(
            "Microsoft Graph request operation=%s host=%s client_request_id=%s",
            operation.value,
            self._base_url.host,
            client_request_id,
        )

    def _read_utcnow(self) -> datetime:
        now = self._utcnow()
        if not isinstance(now, datetime):
            raise TypeError("utcnow must return a datetime")
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("utcnow must return a timezone-aware datetime")
        return now.astimezone(UTC)


def _validate_base_url(base_url: str | httpx2.URL) -> httpx2.URL:
    try:
        url = httpx2.URL(base_url)
    except (TypeError, ValueError):
        raise ValueError("base_url must be a valid HTTP URL") from None
    if url.scheme not in {"http", "https"} or url.host is None:
        raise ValueError("base_url must be an absolute HTTP URL")
    if url.username or url.password or url.query or url.fragment:
        raise ValueError(
            "base_url must not contain credentials, a query, or a fragment"
        )
    normalized_path = url.path.rstrip("/")
    if not normalized_path.endswith(f"/{GRAPH_API_VERSION}"):
        raise ValueError(f"base_url must target the pinned {GRAPH_API_VERSION} API")
    return httpx2.URL(f"{url.scheme}://{_authority(url)}{normalized_path}")


def _validate_timeout(
    timeout: float | httpx2.Timeout,
) -> float | httpx2.Timeout:
    if isinstance(timeout, bool):
        raise TypeError("timeout must be a positive number or httpx2.Timeout")
    if isinstance(timeout, int | float):
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be finite and greater than zero")
        return float(timeout)
    if isinstance(timeout, httpx2.Timeout):
        values = (timeout.connect, timeout.read, timeout.write, timeout.pool)
        if any(
            value is None or not math.isfinite(value) or value <= 0 for value in values
        ):
            raise ValueError("all timeout phases must be finite and greater than zero")
        return timeout
    raise TypeError("timeout must be a positive number or httpx2.Timeout")


def _validate_identifier(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if (
        not value
        or value != value.strip()
        or len(value) > 512
        or any(not character.isprintable() for character in value)
    ):
        raise ValueError(
            f"{field_name} must be non-empty, trimmed and printable with at most "
            "512 characters"
        )
    return value


def _validate_folder_name(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("display_name must be a string")
    if (
        not value
        or value != value.strip()
        or len(value) > 255
        or any(not character.isprintable() for character in value)
    ):
        raise ValueError(
            "display_name must be non-empty, trimmed and printable with at most "
            "255 characters"
        )
    return value


def _is_safe_next_link(next_url: httpx2.URL, base_url: httpx2.URL) -> bool:
    if (
        next_url.scheme not in {"http", "https"}
        or next_url.host is None
        or next_url.username
        or next_url.password
        or next_url.fragment
    ):
        return False
    if (
        next_url.scheme.casefold() != base_url.scheme.casefold()
        or next_url.host.casefold() != base_url.host.casefold()
        or _effective_port(next_url) != _effective_port(base_url)
    ):
        return False
    base_path = base_url.path.rstrip("/")
    return next_url.path == base_path or next_url.path.startswith(f"{base_path}/")


def _effective_port(url: httpx2.URL) -> int:
    if url.port is not None:
        return url.port
    return 443 if url.scheme == "https" else 80


def _authority(url: httpx2.URL) -> str:
    host = url.host
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    default_port = 443 if url.scheme == "https" else 80
    return host if url.port in {None, default_port} else f"{host}:{url.port}"


def _require_status(
    response: httpx2.Response,
    operation: GraphOperation,
    expected_status: int,
    *,
    now: datetime,
    outcome_unknown: bool = False,
) -> None:
    if response.status_code == expected_status:
        return
    raise _http_error(
        operation,
        response,
        outcome_unknown=outcome_unknown,
        now=now,
    )


def _http_error(
    operation: GraphOperation,
    response: httpx2.Response,
    *,
    outcome_unknown: bool,
    now: datetime,
) -> GraphClientError:
    status_code = response.status_code
    retry_after_seconds = _parse_retry_after(
        response.headers.get("Retry-After"),
        now=now,
    )
    category, retryable = _classify_http_status(status_code)
    if outcome_unknown and (retryable or 200 <= status_code < 300):
        return GraphOutcomeUnknownError(
            operation=operation,
            category=category,
            technical_code=f"http_{status_code}",
            status_code=status_code,
            retry_after_seconds=retry_after_seconds,
        )
    return GraphHttpError(
        operation=operation,
        category=category,
        status_code=status_code,
        retryable=retryable,
        retry_after_seconds=retry_after_seconds,
    )


def _classify_http_status(status_code: int) -> tuple[GraphErrorCategory, bool]:
    if status_code == httpx2.codes.UNAUTHORIZED:
        return GraphErrorCategory.AUTHENTICATION, False
    if status_code == httpx2.codes.FORBIDDEN:
        return GraphErrorCategory.AUTHORIZATION, False
    if status_code == httpx2.codes.NOT_FOUND:
        return GraphErrorCategory.NOT_FOUND, False
    if status_code in {httpx2.codes.CONFLICT, httpx2.codes.PRECONDITION_FAILED}:
        return GraphErrorCategory.CONFLICT, True
    if status_code == httpx2.codes.CONTENT_TOO_LARGE:
        return GraphErrorCategory.PAYLOAD_TOO_LARGE, False
    if status_code == httpx2.codes.TOO_MANY_REQUESTS:
        return GraphErrorCategory.THROTTLED, True
    if status_code == 509:
        return GraphErrorCategory.THROTTLED, True
    if status_code in {
        httpx2.codes.GONE,
        httpx2.codes.LOCKED,
        httpx2.codes.INSUFFICIENT_STORAGE,
    }:
        return GraphErrorCategory.UNAVAILABLE, True
    if status_code in RETRYABLE_HTTP_STATUSES or 500 <= status_code < 600:
        return GraphErrorCategory.UNAVAILABLE, True
    if 300 <= status_code < 400:
        return GraphErrorCategory.PROTOCOL, False
    if 400 <= status_code < 500:
        return GraphErrorCategory.INVALID_REQUEST, False
    return GraphErrorCategory.PROTOCOL, False


def _parse_retry_after(value: str | None, *, now: datetime) -> float | None:
    if value is None:
        return None
    normalized = value.strip()
    if normalized.isdecimal():
        seconds = float(normalized)
        return seconds if math.isfinite(seconds) else None
    try:
        retry_at = parsedate_to_datetime(normalized)
    except (TypeError, ValueError, OverflowError):
        return None
    if retry_at.tzinfo is None or retry_at.utcoffset() is None:
        return None
    return max(0.0, (retry_at.astimezone(UTC) - now).total_seconds())


def _decode_object(
    response: httpx2.Response,
    operation: GraphOperation,
) -> Mapping[str, object]:
    try:
        payload = response.json()
    except ValueError:
        raise GraphProtocolError(operation, "invalid_json_response") from None
    if not isinstance(payload, Mapping):
        raise GraphProtocolError(operation, "invalid_json_object")
    return payload


def _parse_message(
    payload: Mapping[str, object],
    operation: GraphOperation,
) -> GraphMessage:
    return GraphMessage(
        message_id=MessageId(_require_string(payload, "id", operation)),
        parent_folder_id=FolderId(
            _require_string(payload, "parentFolderId", operation)
        ),
        received_at=_require_datetime(payload, "receivedDateTime", operation),
        has_attachments=_require_bool(payload, "hasAttachments", operation),
    )


def _parse_attachment(
    payload: Mapping[str, object],
    operation: GraphOperation,
) -> GraphAttachment:
    raw_kind = payload.get("@odata.type", "#microsoft.graph.fileAttachment")
    kind = {
        "#microsoft.graph.fileAttachment": GraphAttachmentKind.FILE,
        "#microsoft.graph.itemAttachment": GraphAttachmentKind.ITEM,
        "#microsoft.graph.referenceAttachment": GraphAttachmentKind.REFERENCE,
    }.get(raw_kind)
    if kind is None:
        raise GraphProtocolError(operation, "unsupported_attachment_type")
    return GraphAttachment(
        attachment_id=AttachmentId(_require_string(payload, "id", operation)),
        name=_require_string(payload, "name", operation, maximum_length=255),
        content_type=_require_string(
            payload,
            "contentType",
            operation,
            maximum_length=255,
        ),
        size_bytes=_require_non_negative_integer(payload, "size", operation),
        is_inline=_require_bool(payload, "isInline", operation),
        kind=kind,
    )


def _parse_folder(
    payload: Mapping[str, object],
    operation: GraphOperation,
) -> GraphMailFolder:
    return GraphMailFolder(
        folder_id=FolderId(_require_string(payload, "id", operation)),
        display_name=_require_string(
            payload,
            "displayName",
            operation,
            maximum_length=255,
        ),
    )


def _parse_message_location(
    payload: Mapping[str, object],
    operation: GraphOperation,
) -> MessageMoveResult:
    return MessageMoveResult(
        message_id=MessageId(_require_string(payload, "id", operation)),
        parent_folder_id=FolderId(
            _require_string(payload, "parentFolderId", operation)
        ),
        outcome=MoveOutcome.MOVED,
    )


def _require_string(
    payload: Mapping[str, object],
    key: str,
    operation: GraphOperation,
    *,
    maximum_length: int = 512,
) -> str:
    value = payload.get(key)
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum_length
        or any(not character.isprintable() for character in value)
    ):
        raise GraphProtocolError(operation, f"invalid_{_snake_case(key)}")
    return value


def _require_bool(
    payload: Mapping[str, object],
    key: str,
    operation: GraphOperation,
) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise GraphProtocolError(operation, f"invalid_{_snake_case(key)}")
    return value


def _require_non_negative_integer(
    payload: Mapping[str, object],
    key: str,
    operation: GraphOperation,
) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise GraphProtocolError(operation, f"invalid_{_snake_case(key)}")
    return value


def _require_datetime(
    payload: Mapping[str, object],
    key: str,
    operation: GraphOperation,
) -> datetime:
    raw_value = _require_string(payload, key, operation, maximum_length=64)
    try:
        value = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
    except ValueError:
        raise GraphProtocolError(operation, f"invalid_{_snake_case(key)}") from None
    if value.tzinfo is None or value.utcoffset() is None:
        raise GraphProtocolError(operation, f"invalid_{_snake_case(key)}")
    return value.astimezone(UTC)


def _read_content_length(
    response: httpx2.Response,
    operation: GraphOperation,
) -> int | None:
    raw_value = response.headers.get("Content-Length")
    if raw_value is None:
        return None
    try:
        value = int(raw_value, 10)
    except ValueError:
        raise GraphProtocolError(operation, "invalid_content_length") from None
    if value < 0:
        raise GraphProtocolError(operation, "invalid_content_length")
    return value


def _write_all(sink: BinaryIO, content: bytes) -> None:
    written = sink.write(content)
    if written is not None and written != len(content):
        raise OSError("attachment sink performed a partial write")


def _snake_case(value: str) -> str:
    characters: list[str] = []
    for character in value:
        if character.isupper():
            characters.extend(("_", character.casefold()))
        else:
            characters.append(character)
    return "".join(characters).lstrip("_")


__all__ = [
    "DEFAULT_MAX_ATTACHMENT_BYTES",
    "DEFAULT_PAGE_SIZE",
    "DOWNLOAD_CHUNK_SIZE",
    "GRAPH_API_BASE_URL",
    "GRAPH_API_VERSION",
    "GraphClient",
    "IMMUTABLE_ID_PREFERENCE",
]
