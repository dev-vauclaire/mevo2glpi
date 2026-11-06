"""Unit contracts for the fixed PBX subject parser."""

import pytest

from mevo2glpi_worker_publisher.publisher.originating_phone_number import (
    extract_originating_phone_number,
)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("01 23 45 67 89", "0123456789"),
        ("06 78 37 43 63", "0678374363"),
        ("09 87 65 43 21", "0987654321"),
    ],
)
def test_extracts_only_the_french_originating_number(
    source: str,
    expected: str,
) -> None:
    subject = (
        f"Message vocale de la part de{source}pourMEVO CH-VAUCLAIRE 29 34 23 12 32"
    )

    assert extract_originating_phone_number(subject) == expected


@pytest.mark.parametrize(
    "subject",
    [
        None,
        "",
        "Message vocal de la part de06 78 37 43 63pourMEVO",
        "Message vocale de la part de06 78 37 43 63",
        "Message vocale de la part depourMEVO",
        "Message vocale de la part de06 78 37 43pourMEVO",
        "Message vocale de la part de06 78 37 43 63 00pourMEVO",
        "Message vocale de la part de00 78 37 43 63pourMEVO",
        "Message vocale de la part de+33 6 78 37 43 63pourMEVO",
        "Message vocale de la part de06 78 37 43 6ApourMEVO",
        "Message vocale de la part de０6 78 37 43 63pourMEVO",
        "Message vocale de la part de06 78 37 43 63pour",
        "MEVO CH-VAUCLAIRE 29 34 23 12 32",
    ],
)
def test_rejects_absent_ambiguous_or_unsupported_subjects(
    subject: str | None,
) -> None:
    assert extract_originating_phone_number(subject) is None


def test_uses_the_first_destination_marker() -> None:
    subject = (
        "Message vocale de la part de06 78 37 43 63"
        "pourMEVO pour CH-VAUCLAIRE 29 34 23 12 32"
    )

    assert extract_originating_phone_number(subject) == "0678374363"


def test_rejects_a_non_string_subject() -> None:
    with pytest.raises(TypeError, match="subject must be a string or None"):
        extract_originating_phone_number(42)  # type: ignore[arg-type]
