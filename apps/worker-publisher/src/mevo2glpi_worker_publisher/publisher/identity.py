"""Stable identities derived from immutable Exchange source metadata."""

from __future__ import annotations

from uuid import UUID, uuid5


def derive_ingestion_uuid(mailbox_id: UUID, immutable_message_id: str) -> UUID:
    """Derive one stable ingestion UUID from the exact Exchange source identity.

    Microsoft Graph identifiers are opaque and case-sensitive. They must never be
    normalized before this derivation. The mailbox UUID is the UUIDv5 namespace,
    which naturally isolates otherwise identical message identifiers per mailbox.
    """
    if not isinstance(mailbox_id, UUID):
        raise TypeError("mailbox_id must be a UUID")
    if not isinstance(immutable_message_id, str):
        raise TypeError("immutable_message_id must be a string")
    if (
        not immutable_message_id
        or immutable_message_id != immutable_message_id.strip()
        or any(not character.isprintable() for character in immutable_message_id)
    ):
        raise ValueError(
            "immutable_message_id must be non-empty, trimmed and printable"
        )
    return uuid5(mailbox_id, immutable_message_id)
