"""GLPI API V2.3.0 client and stable application-facing contracts."""

from .client import GLPI_API_VERSION, GlpiClient
from .contracts import (
    GlpiClientError,
    GlpiCreateOutcomeUnknownError,
    GlpiDuplicateExternalIdError,
    GlpiHttpError,
    GlpiProtocolError,
    GlpiTicketCreationProtocol,
    GlpiTicketNotFoundError,
    GlpiTicketReaderProtocol,
    GlpiTicketRejectedError,
    GlpiTransportError,
)
from .models import (
    GlpiErrorCategory,
    GlpiOperation,
    GlpiTicketCreateCommand,
    GlpiTicketDefaults,
    GlpiTicketSnapshot,
    OAuthToken,
    TicketId,
    TicketLevel,
    TicketStatus,
    TicketType,
)

__all__ = [
    "GLPI_API_VERSION",
    "GlpiClient",
    "GlpiClientError",
    "GlpiCreateOutcomeUnknownError",
    "GlpiDuplicateExternalIdError",
    "GlpiErrorCategory",
    "GlpiHttpError",
    "GlpiOperation",
    "GlpiProtocolError",
    "GlpiTicketCreateCommand",
    "GlpiTicketCreationProtocol",
    "GlpiTicketDefaults",
    "GlpiTicketNotFoundError",
    "GlpiTicketReaderProtocol",
    "GlpiTicketRejectedError",
    "GlpiTicketSnapshot",
    "GlpiTransportError",
    "OAuthToken",
    "TicketId",
    "TicketLevel",
    "TicketStatus",
    "TicketType",
]
