"""Unit contracts for deterministic Exchange ingestion identities."""

from uuid import UUID

import pytest

from mevo2glpi_worker_publisher.publisher.identity import derive_ingestion_uuid

MAILBOX_ID = UUID("94023353-3fb5-45fa-8336-a938e1dc914c")
OTHER_MAILBOX_ID = UUID("f72de50c-b36f-4d22-aa24-4cd03c690a51")


def test_derives_stable_reference_uuid_from_exact_source_identity() -> None:
    expected = UUID("5ef27c65-14ce-5403-85a5-bb2ce4f86cdb")

    first = derive_ingestion_uuid(MAILBOX_ID, "immutable-message-reference")
    second = derive_ingestion_uuid(MAILBOX_ID, "immutable-message-reference")

    assert first == expected
    assert second == expected
    assert first.version == 5


def test_distinct_messages_in_the_same_mailbox_have_distinct_uuids() -> None:
    first = derive_ingestion_uuid(MAILBOX_ID, "immutable-message-a")
    second = derive_ingestion_uuid(MAILBOX_ID, "immutable-message-b")

    assert first != second


def test_same_message_identifier_in_distinct_mailboxes_has_distinct_uuids() -> None:
    first = derive_ingestion_uuid(MAILBOX_ID, "immutable-message-a")
    second = derive_ingestion_uuid(OTHER_MAILBOX_ID, "immutable-message-a")

    assert first != second


def test_message_identifier_derivation_is_case_sensitive() -> None:
    lowercase = derive_ingestion_uuid(MAILBOX_ID, "immutable-message-a")
    uppercase = derive_ingestion_uuid(MAILBOX_ID, "IMMUTABLE-MESSAGE-A")

    assert lowercase != uppercase


@pytest.mark.parametrize("invalid_message_id", ["", " leading", "trailing ", "bad\n"])
def test_rejects_invalid_immutable_message_identifiers(
    invalid_message_id: str,
) -> None:
    with pytest.raises(ValueError, match="immutable_message_id"):
        derive_ingestion_uuid(MAILBOX_ID, invalid_message_id)


def test_rejects_invalid_identity_types() -> None:
    with pytest.raises(TypeError, match="mailbox_id"):
        derive_ingestion_uuid("not-a-uuid", "message")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="immutable_message_id"):
        derive_ingestion_uuid(MAILBOX_ID, 42)  # type: ignore[arg-type]
