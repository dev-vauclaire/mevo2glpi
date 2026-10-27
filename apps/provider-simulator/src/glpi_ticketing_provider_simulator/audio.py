"""Generate tiny structurally valid and non-sensitive staging audio files."""

from __future__ import annotations

import struct
from enum import StrEnum

_OGG_CRC_POLYNOMIAL = 0x04C11DB7
_UINT32_MASK = 0xFFFFFFFF


class SyntheticAudioExtension(StrEnum):
    """Audio formats accepted by the publisher staging contract."""

    WAV = "wav"
    OGG = "ogg"

    @property
    def media_type(self) -> str:
        """Return the MIME type exposed by the fake Graph attachment."""
        if self is SyntheticAudioExtension.WAV:
            return "audio/wav"
        return "audio/ogg"


def build_synthetic_audio(extension: SyntheticAudioExtension) -> bytes:
    """Return deterministic container bytes without speech or personal data."""
    if extension is SyntheticAudioExtension.WAV:
        return _build_wav()
    if extension is SyntheticAudioExtension.OGG:
        return _build_ogg()
    raise ValueError("unsupported synthetic audio extension")


def _build_wav() -> bytes:
    audio = b"\xd5" * 80
    format_chunk = struct.pack("<HHIIHH", 6, 1, 8_000, 8_000, 1, 8)
    chunks = b"fmt " + struct.pack("<I", len(format_chunk)) + format_chunk
    chunks += b"data" + struct.pack("<I", len(audio)) + audio
    return b"RIFF" + struct.pack("<I", len(chunks) + 4) + b"WAVE" + chunks


def _build_ogg() -> bytes:
    payload = b"synthetic-ogg"
    header = struct.pack(
        "<4sBBQIIIB",
        b"OggS",
        0,
        0x02 | 0x04,
        0,
        1,
        0,
        0,
        1,
    )
    page = bytearray(header + bytes((len(payload),)) + payload)
    page[22:26] = _ogg_crc(page).to_bytes(4, byteorder="little")
    return bytes(page)


def _ogg_crc(page: bytes | bytearray) -> int:
    checksum = 0
    for octet in page:
        checksum ^= octet << 24
        for _ in range(8):
            if checksum & 0x80000000:
                checksum = (checksum << 1) ^ _OGG_CRC_POLYNOMIAL
            else:
                checksum <<= 1
            checksum &= _UINT32_MASK
    return checksum


__all__ = ["SyntheticAudioExtension", "build_synthetic_audio"]
