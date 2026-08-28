"""Typed domain models for the GLPI API adapter."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from typing import NewType
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import SecretStr

TicketId = NewType("TicketId", int)


class TicketType(IntEnum):
    """Ticket types exposed by the pinned GLPI contract."""

    INCIDENT = 1
    REQUEST = 2


class TicketLevel(IntEnum):
    """Urgency and impact levels accepted by GLPI."""

    VERY_LOW = 1
    LOW = 2
    MEDIUM = 3
    HIGH = 4
    VERY_HIGH = 5


class TicketStatus(IntEnum):
    """Ticket statuses exposed by the pinned GLPI 11.0.8 API contract."""

    INCOMING = 1
    ASSIGNED = 2
    PLANNED = 3
    WAITING = 4
    SOLVED = 5
    CLOSED = 6
    APPROVAL = 10


class GlpiOperation(StrEnum):
    """Operation performed against GLPI."""

    AUTHENTICATE = "authenticate"
    FIND_TICKET = "find_ticket"
    CREATE_TICKET = "create_ticket"
    GET_TICKET = "get_ticket"


class GlpiErrorCategory(StrEnum):
    """Stable failure categories exposed to the worker."""

    UNAVAILABLE = "unavailable"
    AUTHENTICATION = "authentication"
    AUTHORIZATION = "authorization"
    INVALID_REQUEST = "invalid_request"
    PROTOCOL = "protocol"
    CONFLICT = "conflict"
    NOT_FOUND = "not_found"


@dataclass(frozen=True, slots=True)
class OAuthToken:
    """Validated OAuth token returned by GLPI.

    The access token remains explicitly marked as a secret even when a caller
    requests authentication directly, for example in an integration health check.
    """

    token_type: str
    access_token: SecretStr = field(repr=False)
    expires_in_seconds: int


@dataclass(frozen=True, slots=True)
class GlpiTicketDefaults:
    """Validated ticket values supplied by application configuration."""

    title: str
    ticket_type: TicketType
    urgency: TicketLevel
    impact: TicketLevel

    def __post_init__(self) -> None:
        _validate_title(self.title)
        if not isinstance(self.ticket_type, TicketType):
            raise TypeError("ticket_type must be a TicketType")
        for field_name in ("urgency", "impact"):
            if not isinstance(getattr(self, field_name), TicketLevel):
                raise TypeError(f"{field_name} must be a TicketLevel")


@dataclass(frozen=True, slots=True)
class GlpiTicketCreateCommand:
    """Complete application command used to retrieve or create one ticket."""

    audio_uuid: UUID
    transcription: str = field(repr=False)
    audio_url: str
    defaults: GlpiTicketDefaults

    def __post_init__(self) -> None:
        if not isinstance(self.audio_uuid, UUID):
            raise TypeError("audio_uuid must be a UUID")
        if not isinstance(self.transcription, str):
            raise TypeError("transcription must be a string")
        if not self.transcription.strip():
            raise ValueError("transcription must not be empty")
        if any(
            (ord(character) < 32 and character not in "\r\n\t") or ord(character) == 127
            for character in self.transcription
        ):
            raise ValueError("transcription contains unsupported control characters")
        _validate_audio_url(self.audio_url)
        if not isinstance(self.defaults, GlpiTicketDefaults):
            raise TypeError("defaults must be a GlpiTicketDefaults")


@dataclass(frozen=True, slots=True)
class GlpiTicketSnapshot:
    """Minimal ticket state required by lifecycle and retention policies."""

    ticket_id: TicketId
    external_id: str
    status: TicketStatus

    def __post_init__(self) -> None:
        if (
            isinstance(self.ticket_id, bool)
            or not isinstance(self.ticket_id, int)
            or self.ticket_id <= 0
        ):
            raise ValueError("ticket_id must be a positive integer")
        if (
            not isinstance(self.external_id, str)
            or not self.external_id
            or self.external_id != self.external_id.strip()
            or any(not character.isprintable() for character in self.external_id)
        ):
            raise ValueError("external_id must be non-empty, trimmed and printable")
        if not isinstance(self.status, TicketStatus):
            raise TypeError("status must be a TicketStatus")


def _validate_title(title: object) -> None:
    if not isinstance(title, str):
        raise TypeError("title must be a string")
    if (
        not title
        or title != title.strip()
        or len(title) > 255
        or any(not character.isprintable() for character in title)
    ):
        raise ValueError(
            "title must be non-empty, trimmed, printable and at most 255 characters"
        )


def _validate_audio_url(audio_url: object) -> None:
    if not isinstance(audio_url, str):
        raise TypeError("audio_url must be a string")
    if (
        not audio_url
        or audio_url != audio_url.strip()
        or any(not character.isprintable() for character in audio_url)
    ):
        raise ValueError("audio_url must be non-empty, trimmed and printable")

    try:
        parsed_url = urlsplit(audio_url)
        host = parsed_url.hostname
        port = parsed_url.port
    except ValueError as error:
        raise ValueError("audio_url must be a valid HTTP URL") from error

    del port
    if parsed_url.scheme not in {"http", "https"} or host is None:
        raise ValueError("audio_url must be an absolute HTTP URL")
    if parsed_url.username or parsed_url.password:
        raise ValueError("audio_url must not contain credentials")
