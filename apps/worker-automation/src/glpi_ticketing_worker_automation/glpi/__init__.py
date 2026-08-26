"""GLPI API V2.3.0 client and stable worker-facing contracts."""

from .client import GLPI_API_VERSION, GlpiClient
from .contracts import (
    GlpiClientError,
    GlpiClientProtocol,
    GlpiCreateOutcomeUnknownError,
    GlpiDuplicateExternalIdError,
    GlpiHttpError,
    GlpiProtocolError,
    GlpiTransportError,
)
from .models import (
    GlpiErrorCategory,
    GlpiOperation,
    GlpiTicketDefaults,
    GlpiTicketInput,
    OAuthToken,
    TicketId,
    TicketLevel,
    TicketType,
)

__all__ = [
    "GLPI_API_VERSION",
    "GlpiClient",
    "GlpiClientError",
    "GlpiClientProtocol",
    "GlpiCreateOutcomeUnknownError",
    "GlpiDuplicateExternalIdError",
    "GlpiErrorCategory",
    "GlpiHttpError",
    "GlpiOperation",
    "GlpiProtocolError",
    "GlpiTicketDefaults",
    "GlpiTicketInput",
    "GlpiTransportError",
    "OAuthToken",
    "TicketId",
    "TicketLevel",
    "TicketType",
]
