"""Strict extraction of the technical originating telephone number."""

from __future__ import annotations

SUBJECT_PREFIX = "Message vocale de la part de"
SUBJECT_SEPARATOR = "pour"


def extract_originating_phone_number(subject: str | None) -> str | None:
    """Return the French source number encoded by the fixed PBX subject.

    The destination may itself contain digits, so extraction is deliberately
    bounded by the fixed prefix and the first ``pour`` marker. Any contract
    drift fails safely and leaves ticket processing unchanged.
    """
    if subject is None:
        return None
    if not isinstance(subject, str):
        raise TypeError("subject must be a string or None")
    if not subject.startswith(SUBJECT_PREFIX):
        return None

    raw_number, separator, destination = subject[len(SUBJECT_PREFIX) :].partition(
        SUBJECT_SEPARATOR
    )
    if not separator or not destination:
        return None

    normalized = raw_number.replace(" ", "")
    if (
        len(normalized) != 10
        or not normalized.isascii()
        or not normalized.isdecimal()
        or normalized[0] != "0"
        or normalized[1] not in "123456789"
    ):
        return None
    return normalized


__all__ = [
    "SUBJECT_PREFIX",
    "SUBJECT_SEPARATOR",
    "extract_originating_phone_number",
]
