"""Tests for transient originating-phone-number protection."""

from __future__ import annotations

import base64
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from glpi_ticketing_common import (
    OriginatingPhoneNumberCipher,
    OriginatingPhoneNumberCipherError,
    OriginatingPhoneNumberKeyError,
    format_originating_phone_number,
)

KEY = bytes(range(32))
OTHER_KEY = bytes(reversed(range(32)))
INGESTION_UUID = UUID("2dd768a9-67a0-42f2-bad6-c1b3bd904d9b")
PHONE_NUMBER = "0678374363"


def test_cipher_round_trip_is_bound_to_ingestion_uuid() -> None:
    cipher = OriginatingPhoneNumberCipher(KEY)

    ciphertext = cipher.encrypt(PHONE_NUMBER, INGESTION_UUID)

    assert cipher.decrypt(ciphertext, INGESTION_UUID) == PHONE_NUMBER
    assert PHONE_NUMBER.encode() not in ciphertext


def test_encrypt_uses_a_fresh_nonce() -> None:
    cipher = OriginatingPhoneNumberCipher(KEY)

    first = cipher.encrypt(PHONE_NUMBER, INGESTION_UUID)
    second = cipher.encrypt(PHONE_NUMBER, INGESTION_UUID)

    assert first != second


@pytest.mark.parametrize("mutation", ["wrong_uuid", "wrong_key", "tampered"])
def test_decrypt_rejects_untrusted_ciphertext(mutation: str) -> None:
    cipher = OriginatingPhoneNumberCipher(KEY)
    ciphertext = cipher.encrypt(PHONE_NUMBER, INGESTION_UUID)
    ingestion_uuid = INGESTION_UUID
    decrypting_cipher = cipher
    if mutation == "wrong_uuid":
        ingestion_uuid = uuid4()
    elif mutation == "wrong_key":
        decrypting_cipher = OriginatingPhoneNumberCipher(OTHER_KEY)
    else:
        ciphertext = ciphertext[:-1] + bytes((ciphertext[-1] ^ 1,))

    with pytest.raises(OriginatingPhoneNumberCipherError):
        decrypting_cipher.decrypt(ciphertext, ingestion_uuid)


@pytest.mark.parametrize(
    "ciphertext",
    [b"", b"unsupported", b"GTAOPN\x02" + b"0" * 38],
)
def test_decrypt_rejects_invalid_envelopes(ciphertext: bytes) -> None:
    with pytest.raises(OriginatingPhoneNumberCipherError):
        OriginatingPhoneNumberCipher(KEY).decrypt(ciphertext, INGESTION_UUID)


@pytest.mark.parametrize(
    "phone_number",
    ["", "06 78 37 43 63", "+33678374363", "0078374363", "067837436", "٠٦٧٨٣٧٤٣٦٣"],
)
def test_cipher_rejects_non_canonical_phone_numbers(phone_number: str) -> None:
    with pytest.raises(ValueError):
        OriginatingPhoneNumberCipher(KEY).encrypt(phone_number, INGESTION_UUID)


def test_format_originating_phone_number_groups_digits() -> None:
    assert format_originating_phone_number(PHONE_NUMBER) == "06 78 37 43 63"


def test_from_base64_key_file_loads_one_256_bit_key(tmp_path: Path) -> None:
    key_path = tmp_path / "originating-phone-number.key"
    key_path.write_bytes(base64.b64encode(KEY) + b"\n")

    cipher = OriginatingPhoneNumberCipher.from_base64_key_file(key_path)

    ciphertext = cipher.encrypt(PHONE_NUMBER, INGESTION_UUID)
    assert cipher.decrypt(ciphertext, INGESTION_UUID) == PHONE_NUMBER


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"not base64",
        base64.b64encode(b"too short"),
        b" " + base64.b64encode(KEY),
        base64.b64encode(KEY) + b"\n\n",
        b"A" * 300,
    ],
)
def test_from_base64_key_file_rejects_invalid_key_material(
    tmp_path: Path,
    content: bytes,
) -> None:
    key_path = tmp_path / "originating-phone-number.key"
    key_path.write_bytes(content)

    with pytest.raises(OriginatingPhoneNumberKeyError):
        OriginatingPhoneNumberCipher.from_base64_key_file(key_path)


def test_cipher_rejects_invalid_argument_types() -> None:
    cipher = OriginatingPhoneNumberCipher(KEY)

    with pytest.raises(TypeError):
        OriginatingPhoneNumberCipher("secret")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        cipher.encrypt(123, INGESTION_UUID)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        cipher.encrypt(PHONE_NUMBER, "uuid")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        cipher.decrypt("ciphertext", INGESTION_UUID)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        OriginatingPhoneNumberCipher.from_base64_key_file("key")  # type: ignore[arg-type]
