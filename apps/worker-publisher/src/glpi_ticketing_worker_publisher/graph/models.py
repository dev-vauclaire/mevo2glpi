"""Typed, content-minimal models exposed by the Microsoft Graph adapter."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import NewType

from pydantic import SecretStr

MessageId = NewType("MessageId", str)
AttachmentId = NewType("AttachmentId", str)
FolderId = NewType("FolderId", str)

MAX_GRAPH_IDENTIFIER_LENGTH = 512


class GraphOperation(StrEnum):
    """Stable names for calls made to Entra ID or Microsoft Graph."""

    AUTHENTICATE = "authenticate"
    LIST_MESSAGES = "list_messages"
    GET_MESSAGE = "get_message"
    LIST_ATTACHMENTS = "list_attachments"
    DOWNLOAD_ATTACHMENT = "download_attachment"
    LIST_FOLDERS = "list_folders"
    CREATE_FOLDER = "create_folder"
    MOVE_MESSAGE = "move_message"


class GraphErrorCategory(StrEnum):
    """Sanitized failure categories understood by publisher policies."""

    UNAVAILABLE = "unavailable"
    THROTTLED = "throttled"
    AUTHENTICATION = "authentication"
    AUTHORIZATION = "authorization"
    INVALID_REQUEST = "invalid_request"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    PROTOCOL = "protocol"


class GraphAttachmentKind(StrEnum):
    """Attachment variants returned by the Graph message API."""

    FILE = "file"
    ITEM = "item"
    REFERENCE = "reference"


class MoveOutcome(StrEnum):
    """Observable result of ensuring a message is in its destination folder."""

    ALREADY_IN_DESTINATION = "already_in_destination"
    MOVED = "moved"


@dataclass(frozen=True, slots=True, kw_only=True)
class GraphAccessToken:
    """Bearer token kept secret in representations and logs."""

    value: SecretStr = field(repr=False)
    expires_in_seconds: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.value, SecretStr):
            raise TypeError("value must be a SecretStr")
        raw_value = self.value.get_secret_value()
        if (
            not raw_value
            or raw_value != raw_value.strip()
            or any(not character.isprintable() for character in raw_value)
        ):
            raise ValueError("value must be a non-empty printable token")
        if self.expires_in_seconds is not None and (
            isinstance(self.expires_in_seconds, bool)
            or not isinstance(self.expires_in_seconds, int)
            or self.expires_in_seconds <= 0
        ):
            raise ValueError("expires_in_seconds must be a positive integer or None")


@dataclass(frozen=True, slots=True, kw_only=True)
class GraphMessage:
    """Minimal Inbox message metadata required by the publisher."""

    message_id: MessageId = field(repr=False)
    parent_folder_id: FolderId = field(repr=False)
    received_at: datetime
    has_attachments: bool

    def __post_init__(self) -> None:
        _validate_graph_identifier(self.message_id, "message_id")
        _validate_graph_identifier(self.parent_folder_id, "parent_folder_id")
        _validate_aware_datetime(self.received_at, "received_at")
        if not isinstance(self.has_attachments, bool):
            raise TypeError("has_attachments must be a boolean")


@dataclass(frozen=True, slots=True, kw_only=True)
class GraphAttachment:
    """Attachment metadata without the attachment content."""

    attachment_id: AttachmentId = field(repr=False)
    name: str = field(repr=False)
    content_type: str
    size_bytes: int
    is_inline: bool
    kind: GraphAttachmentKind = GraphAttachmentKind.FILE

    def __post_init__(self) -> None:
        _validate_graph_identifier(self.attachment_id, "attachment_id")
        _validate_printable_text(self.name, "name", maximum_length=255)
        _validate_printable_text(
            self.content_type,
            "content_type",
            maximum_length=255,
        )
        if (
            isinstance(self.size_bytes, bool)
            or not isinstance(self.size_bytes, int)
            or self.size_bytes < 0
        ):
            raise ValueError("size_bytes must be a non-negative integer")
        if not isinstance(self.is_inline, bool):
            raise TypeError("is_inline must be a boolean")
        if not isinstance(self.kind, GraphAttachmentKind):
            raise TypeError("kind must be a GraphAttachmentKind")


@dataclass(frozen=True, slots=True, kw_only=True)
class GraphMailFolder:
    """Top-level mail folder used to acknowledge a processed message."""

    folder_id: FolderId = field(repr=False)
    display_name: str

    def __post_init__(self) -> None:
        _validate_graph_identifier(self.folder_id, "folder_id")
        _validate_printable_text(
            self.display_name,
            "display_name",
            maximum_length=255,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class AttachmentDownload:
    """Result of a bounded attachment stream copied into a caller-owned sink."""

    size_bytes: int

    def __post_init__(self) -> None:
        if (
            isinstance(self.size_bytes, bool)
            or not isinstance(self.size_bytes, int)
            or self.size_bytes < 0
        ):
            raise ValueError("size_bytes must be a non-negative integer")


@dataclass(frozen=True, slots=True, kw_only=True)
class MessageMoveResult:
    """Confirmed destination of an immutable message identifier."""

    message_id: MessageId = field(repr=False)
    parent_folder_id: FolderId = field(repr=False)
    outcome: MoveOutcome

    def __post_init__(self) -> None:
        _validate_graph_identifier(self.message_id, "message_id")
        _validate_graph_identifier(self.parent_folder_id, "parent_folder_id")
        if not isinstance(self.outcome, MoveOutcome):
            raise TypeError("outcome must be a MoveOutcome")


def _validate_graph_identifier(value: object, field_name: str) -> str:
    return _validate_printable_text(
        value,
        field_name,
        maximum_length=MAX_GRAPH_IDENTIFIER_LENGTH,
    )


def _validate_printable_text(
    value: object,
    field_name: str,
    *,
    maximum_length: int,
) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if (
        not value
        or value != value.strip()
        or len(value) > maximum_length
        or any(not character.isprintable() for character in value)
    ):
        raise ValueError(
            f"{field_name} must be non-empty, trimmed and printable with at most "
            f"{maximum_length} characters"
        )
    return value


def _validate_aware_datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


__all__ = [
    "AttachmentDownload",
    "AttachmentId",
    "FolderId",
    "GraphAccessToken",
    "GraphAttachment",
    "GraphAttachmentKind",
    "GraphErrorCategory",
    "GraphMailFolder",
    "GraphMessage",
    "GraphOperation",
    "MAX_GRAPH_IDENTIFIER_LENGTH",
    "MessageId",
    "MessageMoveResult",
    "MoveOutcome",
]
