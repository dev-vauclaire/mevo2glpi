"""Stable ports and sanitized failures for the Microsoft Graph adapter."""

from __future__ import annotations

from typing import BinaryIO, Protocol

from .models import (
    AttachmentDownload,
    AttachmentId,
    FolderId,
    GraphAccessToken,
    GraphAttachment,
    GraphErrorCategory,
    GraphMailFolder,
    GraphMessage,
    GraphOperation,
    MessageId,
    MessageMoveResult,
)


class GraphTokenProviderProtocol(Protocol):
    """Provide cached application tokens without exposing MSAL to the client."""

    def get_access_token(self) -> GraphAccessToken:
        """Return a usable Microsoft Graph application token."""
        ...

    def invalidate(self) -> None:
        """Discard the current application token after an HTTP 401 response."""
        ...


class GraphMailboxProtocol(Protocol):
    """Narrow mailbox operations used by the publisher lifecycle."""

    def list_messages(self) -> tuple[GraphMessage, ...]:
        """List all Inbox messages through bounded Graph pagination."""
        ...

    def list_attachments(
        self,
        message_id: MessageId,
    ) -> tuple[GraphAttachment, ...]:
        """List attachment metadata for one immutable message identifier."""
        ...

    def download_attachment(
        self,
        message_id: MessageId,
        attachment_id: AttachmentId,
        sink: BinaryIO,
    ) -> AttachmentDownload:
        """Copy one attachment into a caller-owned sink under a strict limit."""
        ...

    def ensure_mail_folder(self, display_name: str) -> GraphMailFolder:
        """Return or create one top-level mailbox folder idempotently."""
        ...

    def ensure_message_in_folder(
        self,
        message_id: MessageId,
        destination_folder_id: FolderId,
    ) -> MessageMoveResult:
        """Ensure that one immutable message is in the requested folder."""
        ...


class GraphClientError(Exception):
    """Base failure safe to log without HTTP bodies, identifiers, or secrets."""

    def __init__(
        self,
        *,
        operation: GraphOperation,
        category: GraphErrorCategory,
        technical_code: str,
        retryable: bool,
        outcome_unknown: bool,
        retry_after_seconds: float | None = None,
    ) -> None:
        self.operation = operation
        self.category = category
        self.technical_code = technical_code
        self.retryable = retryable
        self.outcome_unknown = outcome_unknown
        self.retry_after_seconds = retry_after_seconds
        super().__init__(
            f"Microsoft Graph {operation.value} failed "
            f"({category.value}:{technical_code})"
        )


class GraphAuthenticationError(GraphClientError):
    """Report a sanitized MSAL client-credentials failure."""

    def __init__(
        self,
        *,
        category: GraphErrorCategory,
        technical_code: str,
        retryable: bool,
    ) -> None:
        super().__init__(
            operation=GraphOperation.AUTHENTICATE,
            category=category,
            technical_code=technical_code,
            retryable=retryable,
            outcome_unknown=False,
        )


class GraphTransportError(GraphClientError):
    """Report a request that did not produce a usable HTTP response."""

    def __init__(self, operation: GraphOperation) -> None:
        super().__init__(
            operation=operation,
            category=GraphErrorCategory.UNAVAILABLE,
            technical_code="transport_error",
            retryable=True,
            outcome_unknown=False,
        )


class GraphHttpError(GraphClientError):
    """Report an unexpected HTTP response without retaining its body."""

    def __init__(
        self,
        *,
        operation: GraphOperation,
        category: GraphErrorCategory,
        status_code: int,
        retryable: bool,
        retry_after_seconds: float | None = None,
    ) -> None:
        self.status_code = status_code
        super().__init__(
            operation=operation,
            category=category,
            technical_code=f"http_{status_code}",
            retryable=retryable,
            outcome_unknown=False,
            retry_after_seconds=retry_after_seconds,
        )


class GraphProtocolError(GraphClientError):
    """Report a response incompatible with the pinned Graph v1.0 contract."""

    def __init__(self, operation: GraphOperation, technical_code: str) -> None:
        super().__init__(
            operation=operation,
            category=GraphErrorCategory.PROTOCOL,
            technical_code=technical_code,
            retryable=False,
            outcome_unknown=False,
        )


class GraphPayloadTooLargeError(GraphClientError):
    """Stop a download before more than the configured byte limit is written."""

    def __init__(
        self,
        *,
        maximum_bytes: int,
        observed_bytes: int | None = None,
    ) -> None:
        self.maximum_bytes = maximum_bytes
        self.observed_bytes = observed_bytes
        super().__init__(
            operation=GraphOperation.DOWNLOAD_ATTACHMENT,
            category=GraphErrorCategory.PAYLOAD_TOO_LARGE,
            technical_code="attachment_too_large",
            retryable=False,
            outcome_unknown=False,
        )


class GraphOutcomeUnknownError(GraphClientError):
    """Report a mutating request whose remote outcome must be reconciled."""

    def __init__(
        self,
        *,
        operation: GraphOperation,
        category: GraphErrorCategory,
        technical_code: str,
        status_code: int | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        self.status_code = status_code
        super().__init__(
            operation=operation,
            category=category,
            technical_code=technical_code,
            retryable=True,
            outcome_unknown=True,
            retry_after_seconds=retry_after_seconds,
        )


__all__ = [
    "GraphAuthenticationError",
    "GraphClientError",
    "GraphHttpError",
    "GraphMailboxProtocol",
    "GraphOutcomeUnknownError",
    "GraphPayloadTooLargeError",
    "GraphProtocolError",
    "GraphTokenProviderProtocol",
    "GraphTransportError",
]
