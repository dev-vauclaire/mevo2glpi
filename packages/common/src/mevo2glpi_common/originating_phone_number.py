"""Authenticated protection for transient originating phone numbers."""

from __future__ import annotations

import base64
import binascii
import os
import re
from pathlib import Path
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_AES_256_KEY_BYTES = 32
_NONCE_BYTES = 12
_MAGIC = b"MEVOPN"
_VERSION = 1
_HEADER = _MAGIC + bytes((_VERSION,))
_AUTHENTICATION_TAG_BYTES = 16
_PHONE_NUMBER_BYTES = 10
_ENVELOPE_BYTES = (
    len(_HEADER) + _NONCE_BYTES + _PHONE_NUMBER_BYTES + _AUTHENTICATION_TAG_BYTES
)
_MAX_KEY_FILE_BYTES = 256
_PHONE_NUMBER_PATTERN = re.compile(r"0[1-9][0-9]{8}\Z", flags=re.ASCII)
_AAD_PREFIX = b"mevo2glpi:originating-phone-number:v1:"


class OriginatingPhoneNumberCipherError(ValueError):
    """Report that an encrypted originating number cannot be trusted."""


class OriginatingPhoneNumberKeyError(ValueError):
    """Report an invalid or unreadable originating-number key."""


class OriginatingPhoneNumberCipher:
    """Encrypt one canonical French number with an ingestion-bound AES-GCM key."""

    __slots__ = ("_cipher",)

    def __init__(self, key: bytes) -> None:
        if not isinstance(key, bytes):
            raise TypeError("key must be bytes")
        if len(key) != _AES_256_KEY_BYTES:
            raise OriginatingPhoneNumberKeyError(
                "key must contain exactly 32 decoded bytes"
            )
        self._cipher = AESGCM(key)

    @classmethod
    def from_base64_key_file(cls, path: Path) -> OriginatingPhoneNumberCipher:
        """Load one base64-encoded 256-bit key without retaining its text form."""
        if not isinstance(path, Path):
            raise TypeError("path must be a Path")
        try:
            if not path.is_file():
                raise OriginatingPhoneNumberKeyError("key path must be a file")
            encoded_key = path.read_bytes()
        except OriginatingPhoneNumberKeyError:
            raise
        except OSError:
            raise OriginatingPhoneNumberKeyError("key file must be readable") from None
        if not encoded_key or len(encoded_key) > _MAX_KEY_FILE_BYTES:
            raise OriginatingPhoneNumberKeyError(
                "key file must contain one bounded base64 value"
            )
        normalized_key = _strip_one_line_ending(encoded_key)
        if not normalized_key or any(
            character in normalized_key for character in b" \t\r\n"
        ):
            raise OriginatingPhoneNumberKeyError(
                "key file must contain one base64 value"
            )
        try:
            key = base64.b64decode(normalized_key, validate=True)
        except (binascii.Error, ValueError):
            raise OriginatingPhoneNumberKeyError(
                "key file must contain valid base64"
            ) from None
        return cls(key)

    def encrypt(self, phone_number: str, ingestion_uuid: UUID) -> bytes:
        """Return a randomized, versioned envelope bound to one ingestion UUID."""
        normalized_number = _validate_phone_number(phone_number)
        normalized_uuid = _validate_ingestion_uuid(ingestion_uuid)
        nonce = os.urandom(_NONCE_BYTES)
        encrypted = self._cipher.encrypt(
            nonce,
            normalized_number.encode("ascii"),
            _associated_data(normalized_uuid),
        )
        return _HEADER + nonce + encrypted

    def decrypt(self, ciphertext: bytes, ingestion_uuid: UUID) -> str:
        """Authenticate and return one canonical number without logging its value."""
        if not isinstance(ciphertext, bytes):
            raise TypeError("ciphertext must be bytes")
        normalized_uuid = _validate_ingestion_uuid(ingestion_uuid)
        if len(ciphertext) != _ENVELOPE_BYTES or not ciphertext.startswith(_HEADER):
            raise OriginatingPhoneNumberCipherError(
                "originating phone number envelope is invalid"
            )
        nonce_start = len(_HEADER)
        nonce_end = nonce_start + _NONCE_BYTES
        nonce = ciphertext[nonce_start:nonce_end]
        encrypted = ciphertext[nonce_end:]
        try:
            plaintext = self._cipher.decrypt(
                nonce,
                encrypted,
                _associated_data(normalized_uuid),
            )
            phone_number = plaintext.decode("ascii")
            return _validate_phone_number(phone_number)
        except (InvalidTag, UnicodeDecodeError, ValueError):
            raise OriginatingPhoneNumberCipherError(
                "originating phone number authentication failed"
            ) from None


def format_originating_phone_number(phone_number: str) -> str:
    """Format one canonical French number in five groups of two digits."""
    normalized_number = _validate_phone_number(phone_number)
    return " ".join(
        normalized_number[index : index + 2]
        for index in range(0, len(normalized_number), 2)
    )


def _validate_phone_number(phone_number: object) -> str:
    if not isinstance(phone_number, str):
        raise TypeError("phone_number must be a string")
    if _PHONE_NUMBER_PATTERN.fullmatch(phone_number) is None:
        raise ValueError("phone_number must be a canonical French phone number")
    return phone_number


def _validate_ingestion_uuid(ingestion_uuid: object) -> UUID:
    if not isinstance(ingestion_uuid, UUID):
        raise TypeError("ingestion_uuid must be a UUID")
    return ingestion_uuid


def _associated_data(ingestion_uuid: UUID) -> bytes:
    return _AAD_PREFIX + ingestion_uuid.bytes


def _strip_one_line_ending(value: bytes) -> bytes:
    if value.endswith(b"\r\n"):
        return value[:-2]
    if value.endswith(b"\n"):
        return value[:-1]
    return value


__all__ = [
    "OriginatingPhoneNumberCipher",
    "OriginatingPhoneNumberCipherError",
    "OriginatingPhoneNumberKeyError",
    "format_originating_phone_number",
]
