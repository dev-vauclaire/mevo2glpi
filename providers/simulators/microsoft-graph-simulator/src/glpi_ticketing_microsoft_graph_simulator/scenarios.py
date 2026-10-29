"""Bounded message scenarios exposed by the Microsoft Graph simulator."""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from enum import StrEnum

from .audio import (
    SyntheticAudioExtension,
    SyntheticAudioFixture,
    load_synthetic_audio,
)

MAX_SEED_AUDIO_BYTES = 25 * 1024 * 1024


class SyntheticAttachmentKind(StrEnum):
    """Microsoft Graph attachment variants needed by publisher tests."""

    FILE = "file"
    ITEM = "item"
    REFERENCE = "reference"

    @property
    def odata_type(self) -> str:
        """Return the discriminator expected by the real Graph adapter."""
        return f"#microsoft.graph.{self.value}Attachment"


@dataclass(frozen=True, slots=True, kw_only=True)
class SyntheticAttachmentTemplate:
    """Attachment data without the identifier assigned by the state store."""

    name: str
    media_type: str
    content: bytes = field(repr=False)
    kind: SyntheticAttachmentKind = SyntheticAttachmentKind.FILE
    is_inline: bool = False
    declared_size_bytes: int | None = None

    @property
    def size_bytes(self) -> int:
        """Return Graph metadata size, which may model an oversized payload."""
        if self.declared_size_bytes is not None:
            return self.declared_size_bytes
        return len(self.content)


class SeedScenario(StrEnum):
    """Stable operator-facing message scenarios."""

    VALID_WAV = "valid-wav"
    VALID_OGG = "valid-ogg"
    VALID_WAV_ALAW = "valid-wav-alaw"
    NO_ATTACHMENT = "no-attachment"
    TWO_AUDIO_ATTACHMENTS = "two-audio-attachments"
    EMPTY_AUDIO = "empty-audio"
    UNSUPPORTED_FORMAT = "unsupported-format"
    CORRUPT_WAV = "corrupt-wav"
    CORRUPT_OGG = "corrupt-ogg"
    TOO_LARGE = "too-large"
    VALID_AUDIO_WITH_INLINE = "valid-audio-with-inline"
    NON_FILE_ATTACHMENT = "non-file-attachment"
    SILENT_WAV = "silent-wav"

    @property
    def description(self) -> str:
        """Return one concise, non-sensitive operator description."""
        return _SCENARIO_DESCRIPTIONS[self]


_SCENARIO_DESCRIPTIONS = {
    SeedScenario.VALID_WAV: "one spoken PCM16 WAV file",
    SeedScenario.VALID_OGG: "one spoken OGG/Opus file",
    SeedScenario.VALID_WAV_ALAW: "one spoken telephony G.711 A-law WAV file",
    SeedScenario.NO_ATTACHMENT: "no attachment",
    SeedScenario.TWO_AUDIO_ATTACHMENTS: "two visible audio files",
    SeedScenario.EMPTY_AUDIO: "one empty WAV file",
    SeedScenario.UNSUPPORTED_FORMAT: "one unsupported FLAC file",
    SeedScenario.CORRUPT_WAV: "one WAV file with an invalid header",
    SeedScenario.CORRUPT_OGG: "one OGG file with an invalid structure",
    SeedScenario.TOO_LARGE: "one WAV declared larger than 25 MiB",
    SeedScenario.VALID_AUDIO_WITH_INLINE: (
        "one spoken WAV file and one ignored inline image"
    ),
    SeedScenario.NON_FILE_ATTACHMENT: "one Microsoft Graph item attachment",
    SeedScenario.SILENT_WAV: "one structurally valid silent WAV file",
}


def build_scenario_attachments(
    scenario: SeedScenario,
) -> tuple[SyntheticAttachmentTemplate, ...]:
    """Build fresh, deterministic attachments for one named scenario."""
    if not isinstance(scenario, SeedScenario):
        raise TypeError("scenario must be a SeedScenario")
    if scenario is SeedScenario.VALID_WAV:
        return (_fixture_attachment("audio.wav", SyntheticAudioFixture.WAV_PCM),)
    if scenario is SeedScenario.VALID_OGG:
        return (_fixture_attachment("audio.ogg", SyntheticAudioFixture.OGG_OPUS),)
    if scenario is SeedScenario.VALID_WAV_ALAW:
        return (
            _fixture_attachment(
                "audio-telephony.wav",
                SyntheticAudioFixture.WAV_ALAW,
            ),
        )
    if scenario is SeedScenario.NO_ATTACHMENT:
        return ()
    if scenario is SeedScenario.TWO_AUDIO_ATTACHMENTS:
        return (
            _fixture_attachment("audio-one.wav", SyntheticAudioFixture.WAV_PCM),
            _fixture_attachment("audio-two.ogg", SyntheticAudioFixture.OGG_OPUS),
        )
    if scenario is SeedScenario.EMPTY_AUDIO:
        return (_file_attachment("audio.wav", "audio/wav", b""),)
    if scenario is SeedScenario.UNSUPPORTED_FORMAT:
        return (_file_attachment("audio.flac", "audio/flac", b"fLaC\x00"),)
    if scenario is SeedScenario.CORRUPT_WAV:
        return (_file_attachment("audio.wav", "audio/wav", b"not-a-wave-file"),)
    if scenario is SeedScenario.CORRUPT_OGG:
        return (_file_attachment("audio.ogg", "audio/ogg", b"OggS-corrupt"),)
    if scenario is SeedScenario.TOO_LARGE:
        return (
            SyntheticAttachmentTemplate(
                name="audio.wav",
                media_type="audio/wav",
                content=b"RIFF",
                declared_size_bytes=MAX_SEED_AUDIO_BYTES + 1,
            ),
        )
    if scenario is SeedScenario.VALID_AUDIO_WITH_INLINE:
        return (
            _fixture_attachment("audio.wav", SyntheticAudioFixture.WAV_PCM),
            SyntheticAttachmentTemplate(
                name="signature.png",
                media_type="image/png",
                content=b"\x89PNG\r\n\x1a\n",
                is_inline=True,
            ),
        )
    if scenario is SeedScenario.NON_FILE_ATTACHMENT:
        return (
            SyntheticAttachmentTemplate(
                name="attached-message.eml",
                media_type="message/rfc822",
                content=b"",
                kind=SyntheticAttachmentKind.ITEM,
                declared_size_bytes=1,
            ),
        )
    if scenario is SeedScenario.SILENT_WAV:
        return (_file_attachment("audio.wav", "audio/wav", _silent_wav()),)
    raise AssertionError("unhandled seed scenario")


def build_uploaded_audio_attachment(
    extension: SyntheticAudioExtension,
    content: bytes,
) -> SyntheticAttachmentTemplate:
    """Create one privacy-preserving attachment from operator-provided bytes."""
    if not isinstance(extension, SyntheticAudioExtension):
        raise TypeError("extension must be a SyntheticAudioExtension")
    if not isinstance(content, bytes):
        raise TypeError("content must be bytes")
    if not content:
        raise ValueError("uploaded audio must not be empty")
    if len(content) > MAX_SEED_AUDIO_BYTES:
        raise ValueError("uploaded audio exceeds the maximum size")
    return _file_attachment(
        f"operator-audio.{extension.value}",
        extension.media_type,
        content,
    )


def _fixture_attachment(
    name: str,
    fixture: SyntheticAudioFixture,
) -> SyntheticAttachmentTemplate:
    extension = (
        SyntheticAudioExtension.OGG
        if fixture is SyntheticAudioFixture.OGG_OPUS
        else SyntheticAudioExtension.WAV
    )
    return _file_attachment(
        name,
        extension.media_type,
        load_synthetic_audio(fixture),
    )


def _file_attachment(
    name: str,
    media_type: str,
    content: bytes,
) -> SyntheticAttachmentTemplate:
    return SyntheticAttachmentTemplate(
        name=name,
        media_type=media_type,
        content=content,
    )


def _silent_wav() -> bytes:
    sample_rate = 16_000
    audio = b"\x00\x00" * sample_rate
    format_chunk = struct.pack(
        "<HHIIHH",
        1,
        1,
        sample_rate,
        sample_rate * 2,
        2,
        16,
    )
    chunks = b"fmt " + struct.pack("<I", len(format_chunk)) + format_chunk
    chunks += b"data" + struct.pack("<I", len(audio)) + audio
    return b"RIFF" + struct.pack("<I", len(chunks) + 4) + b"WAVE" + chunks


__all__ = [
    "MAX_SEED_AUDIO_BYTES",
    "SeedScenario",
    "SyntheticAttachmentKind",
    "SyntheticAttachmentTemplate",
    "build_scenario_attachments",
    "build_uploaded_audio_attachment",
]
