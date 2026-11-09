"""Load deterministic, non-sensitive audio fixtures used by staging scenarios."""

from __future__ import annotations

import base64
import gzip
import hashlib
from enum import StrEnum
from importlib.resources import files


class SyntheticAudioExtension(StrEnum):
    """Audio formats accepted by the publisher staging contract."""

    WAV = "wav"
    OGG = "ogg"
    MP3 = "mp3"
    M4A = "m4a"

    @property
    def media_type(self) -> str:
        """Return the MIME type exposed by the fake Graph attachment."""
        if self is SyntheticAudioExtension.WAV:
            return "audio/wav"
        if self is SyntheticAudioExtension.OGG:
            return "audio/ogg"
        if self is SyntheticAudioExtension.MP3:
            return "audio/mpeg"
        return "audio/mp4"


class SyntheticAudioFixture(StrEnum):
    """Versioned spoken fixtures generated from a fictitious sentence."""

    WAV_PCM = "valid-speech.wav"
    WAV_ALAW = "valid-speech-alaw.wav"
    WAV_MULAW = "valid-speech-mulaw.wav"
    OGG_OPUS = "valid-speech.ogg"
    MP3 = "valid-speech.mp3"
    M4A_AAC = "valid-speech.m4a"


_FIXTURE_SHA256 = {
    SyntheticAudioFixture.WAV_PCM: (
        "477a01ce6643d7f06ef4bd2e3564d4cb2e01f3fcfa8205e220185129c8889f7d"
    ),
    SyntheticAudioFixture.WAV_ALAW: (
        "86086f9f72d66f4b3b59f420e5dfd64a76880a0103a7cff99bfd1aaa7a1e611c"
    ),
    SyntheticAudioFixture.WAV_MULAW: (
        "0efdd63f887ac7e4024ecb7c8c9b6a8d34af0c7f4a674b3568f0d1e2afa2f5f1"
    ),
    SyntheticAudioFixture.OGG_OPUS: (
        "0eb4e93632e8530feba90abbd1e48781ffc920684538bf3c5603d1c87942dedb"
    ),
    SyntheticAudioFixture.MP3: (
        "054921ab2d3ea04d4a16cb2719a1532f16df32309d972ed4db9a9031d2476044"
    ),
    SyntheticAudioFixture.M4A_AAC: (
        "dded73ffcc52faf83dae4f640d68b66c8966839106df070798eddf91beb9d9b3"
    ),
}


def load_synthetic_audio(fixture: SyntheticAudioFixture) -> bytes:
    """Decode one packaged fixture and fail closed if it was altered."""
    if not isinstance(fixture, SyntheticAudioFixture):
        raise TypeError("fixture must be a SyntheticAudioFixture")
    encoded = (
        files("mevo2glpi_microsoft_graph_simulator")
        .joinpath("fixtures", f"{fixture.value}.gz.b64")
        .read_text(encoding="ascii")
    )
    try:
        compact = "".join(encoded.split())
        content = gzip.decompress(base64.b64decode(compact, validate=True))
    except (ValueError, gzip.BadGzipFile) as error:
        raise RuntimeError("synthetic audio fixture is invalid") from error
    if hashlib.sha256(content).hexdigest() != _FIXTURE_SHA256[fixture]:
        raise RuntimeError("synthetic audio fixture checksum mismatch")
    return content


__all__ = [
    "SyntheticAudioExtension",
    "SyntheticAudioFixture",
    "load_synthetic_audio",
]
