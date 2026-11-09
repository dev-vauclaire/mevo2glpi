from __future__ import annotations

import io
import struct
from pathlib import Path
from uuid import uuid4

import pytest

from mevo2glpi_database import AudioExtension, MailIngestionErrorScope
from mevo2glpi_worker_publisher.graph import (
    AttachmentId,
    GraphAttachment,
    GraphAttachmentKind,
)
from mevo2glpi_worker_publisher.publisher.audio import (
    AudioPublicationError,
    inspect_audio_structure,
    inspect_m4a_structure,
    inspect_mp3_structure,
    inspect_ogg_structure,
    inspect_wav_structure,
    publish_or_confirm_audio,
    select_audio_attachment,
)

MAXIMUM_BYTES = 25 * 1024 * 1024
OGG_CRC_POLYNOMIAL = 0x04C11DB7
UINT32_MASK = 0xFFFFFFFF


def _attachment(
    *,
    name: str = "voice.wav",
    content_type: str = "audio/wav",
    size_bytes: int = 128,
    is_inline: bool = False,
    kind: GraphAttachmentKind = GraphAttachmentKind.FILE,
) -> GraphAttachment:
    return GraphAttachment(
        attachment_id=AttachmentId("attachment-safe-id"),
        name=name,
        content_type=content_type,
        size_bytes=size_bytes,
        is_inline=is_inline,
        kind=kind,
    )


def _wav_bytes(*, codec: int = 6, audio: bytes = b"\xd5" * 80) -> bytes:
    fmt = struct.pack("<HHIIHH", codec, 1, 8_000, 8_000, 1, 8)
    chunks = b"fmt " + struct.pack("<I", len(fmt)) + fmt
    chunks += b"data" + struct.pack("<I", len(audio)) + audio
    return b"RIFF" + struct.pack("<I", len(chunks) + 4) + b"WAVE" + chunks


def _mp3_frame(
    *,
    payload_byte: int = 0,
    version: int = 0b11,
    bitrate_index: int = 9,
    sample_rate_index: int = 0,
) -> bytes:
    header = (
        (0x7FF << 21)
        | (version << 19)
        | (0b01 << 17)
        | (1 << 16)
        | (bitrate_index << 12)
        | (sample_rate_index << 10)
    )
    if version == 0b11:
        bitrate_kbps = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128)[bitrate_index]
        sample_rate_hz = (44_100, 48_000, 32_000)[sample_rate_index]
        coefficient = 144
    else:
        bitrate_kbps = (0, 8, 16, 24, 32, 40, 48, 56, 64, 80)[bitrate_index]
        sample_rate_hz = (22_050, 24_000, 16_000)[sample_rate_index]
        coefficient = 72
    frame_size = coefficient * bitrate_kbps * 1000 // sample_rate_hz
    return header.to_bytes(4, byteorder="big") + bytes((payload_byte,)) * (
        frame_size - 4
    )


def _id3v2_tag(
    payload: bytes = b"synthetic-metadata",
    *,
    with_footer: bool = False,
) -> bytes:
    size = len(payload)
    syncsafe_size = bytes(
        (
            (size >> 21) & 0x7F,
            (size >> 14) & 0x7F,
            (size >> 7) & 0x7F,
            size & 0x7F,
        )
    )
    flags = 0x10 if with_footer else 0
    header = b"ID3" + bytes((4, 0, flags)) + syncsafe_size
    footer = b"3DI" + header[3:] if with_footer else b""
    return header + payload + footer


def _iso_box(box_type: bytes, payload: bytes, *, extended: bool = False) -> bytes:
    if extended:
        return struct.pack(">I4sQ", 1, box_type, 16 + len(payload)) + payload
    return struct.pack(">I4s", 8 + len(payload), box_type) + payload


def _m4a_bytes(
    *,
    handler_type: bytes = b"soun",
    media: bytes = b"synthetic-audio",
    extended_mdat: bool = False,
) -> bytes:
    file_type = _iso_box(b"ftyp", b"M4A " + b"\x00\x00\x00\x00" + b"isomM4A ")
    handler = _iso_box(
        b"hdlr",
        b"\x00\x00\x00\x00" + b"\x00\x00\x00\x00" + handler_type + b"\x00" * 12,
    )
    metadata = _iso_box(b"moov", _iso_box(b"trak", _iso_box(b"mdia", handler)))
    media_data = _iso_box(b"mdat", media, extended=extended_mdat)
    return file_type + metadata + media_data


def _ogg_header(
    *,
    version: int = 0,
    header_type: int = 2,
    segment_count: int = 0,
    sequence: int = 0,
) -> bytes:
    return struct.pack(
        "<4sBBQIIIB",
        b"OggS",
        version,
        header_type,
        0,
        1,
        sequence,
        0,
        segment_count,
    )


def _ogg_page(
    payload: bytes = b"synthetic-payload",
    *,
    version: int = 0,
    header_type: int = 0x02 | 0x04,
    sequence: int = 0,
) -> bytes:
    if len(payload) > 255:
        raise ValueError("test Ogg payload must fit in one segment")
    lacing = bytes((len(payload),)) if payload else b""
    page = bytearray(
        _ogg_header(
            version=version,
            header_type=header_type,
            segment_count=len(lacing),
            sequence=sequence,
        )
        + lacing
        + payload
    )
    page[22:26] = _ogg_crc(page).to_bytes(4, byteorder="little")
    return bytes(page)


def _ogg_crc(page: bytes | bytearray) -> int:
    checksum = 0
    for octet in page:
        checksum ^= octet << 24
        for _ in range(8):
            if checksum & 0x80000000:
                checksum = (checksum << 1) ^ OGG_CRC_POLYNOMIAL
            else:
                checksum <<= 1
            checksum &= UINT32_MASK
    return checksum


def test_attachment_policy_ignores_inline_content_and_accepts_one_wav() -> None:
    inline = _attachment(name="logo.png", is_inline=True)
    wav = _attachment()

    result = select_audio_attachment((inline, wav), maximum_bytes=MAXIMUM_BYTES)

    assert result.attachment is wav
    assert result.audio_extension is AudioExtension.WAV
    assert result.failure is None


def test_attachment_policy_accepts_ogg_case_insensitively_without_trusting_mime() -> (
    None
):
    ogg = _attachment(
        name="voice.OGG",
        content_type="application/octet-stream",
    )

    result = select_audio_attachment((ogg,), maximum_bytes=MAXIMUM_BYTES)

    assert result.attachment is ogg
    assert result.audio_extension is AudioExtension.OGG
    assert result.failure is None


@pytest.mark.parametrize(
    ("name", "expected_extension"),
    [
        ("voice.MP3", AudioExtension.MP3),
        ("voice.M4A", AudioExtension.M4A),
    ],
)
def test_attachment_policy_accepts_mp3_and_m4a_without_trusting_mime(
    name: str,
    expected_extension: AudioExtension,
) -> None:
    attachment = _attachment(name=name, content_type="application/octet-stream")

    result = select_audio_attachment((attachment,), maximum_bytes=MAXIMUM_BYTES)

    assert result.attachment is attachment
    assert result.audio_extension is expected_extension
    assert result.failure is None


@pytest.mark.parametrize(
    ("attachments", "expected_code"),
    [
        ((), "audio_attachment_missing"),
        (
            (_attachment(name="inline-logo.png", is_inline=True),),
            "audio_attachment_missing",
        ),
        ((_attachment(name="voice.flac"),), "audio_format_not_supported"),
        ((_attachment(size_bytes=0),), "audio_empty"),
        (
            (_attachment(size_bytes=MAXIMUM_BYTES + 1),),
            "audio_too_large",
        ),
        (
            (_attachment(), _attachment(name="second.wav")),
            "attachment_count_invalid",
        ),
        (
            (_attachment(kind=GraphAttachmentKind.ITEM),),
            "attachment_kind_invalid",
        ),
    ],
)
def test_attachment_policy_returns_sanitized_rejections(
    attachments: tuple[GraphAttachment, ...],
    expected_code: str,
) -> None:
    result = select_audio_attachment(attachments, maximum_bytes=MAXIMUM_BYTES)

    assert result.audio_extension is None
    assert result.failure is not None
    assert result.failure.scope is MailIngestionErrorScope.MESSAGE
    assert result.failure.technical_code == expected_code


def test_structural_wav_validation_accepts_g711_without_decoding() -> None:
    content = _wav_bytes(codec=6)

    assert inspect_wav_structure(io.BytesIO(content), size_bytes=len(content)) is None


@pytest.mark.parametrize(
    ("audio_extension", "content"),
    [
        (AudioExtension.WAV, _wav_bytes()),
        (AudioExtension.OGG, _ogg_page(b"codec-agnostic-audio")),
        (AudioExtension.MP3, _id3v2_tag() + _mp3_frame()),
        (AudioExtension.M4A, _m4a_bytes()),
    ],
)
def test_structural_audio_validation_dispatches_supported_formats(
    audio_extension: AudioExtension,
    content: bytes,
) -> None:
    assert (
        inspect_audio_structure(
            io.BytesIO(content),
            audio_extension=audio_extension,
            size_bytes=len(content),
        )
        is None
    )


@pytest.mark.parametrize(
    "content",
    [
        _mp3_frame() + _mp3_frame(payload_byte=1),
        _id3v2_tag() + _mp3_frame(),
        _id3v2_tag(with_footer=True) + _mp3_frame(),
        _mp3_frame() + b"TAG" + b"\x00" * 125,
    ],
)
def test_structural_mp3_validation_accepts_complete_layer3_frames(
    content: bytes,
) -> None:
    assert inspect_mp3_structure(io.BytesIO(content), size_bytes=len(content)) is None


@pytest.mark.parametrize(
    ("content", "expected_code"),
    [
        (b"", "audio_empty"),
        (b"bad!", "mp3_header_invalid"),
        (_id3v2_tag(), "mp3_frame_missing"),
        (_id3v2_tag(with_footer=True)[:-1], "mp3_truncated"),
        (b"ID3" + bytes((4, 0, 0, 0, 0, 0, 4)), "mp3_truncated"),
        (_mp3_frame()[:-1], "mp3_truncated"),
        (_mp3_frame() + b"bad!", "mp3_header_invalid"),
        (_mp3_frame() + _mp3_frame(version=0b10), "mp3_header_invalid"),
    ],
)
def test_structural_mp3_validation_rejects_invalid_content(
    content: bytes,
    expected_code: str,
) -> None:
    failure = inspect_mp3_structure(io.BytesIO(content), size_bytes=len(content))

    assert failure is not None
    assert failure.technical_code == expected_code


@pytest.mark.parametrize(
    "content",
    [
        _m4a_bytes(),
        _m4a_bytes(extended_mdat=True),
    ],
)
def test_structural_m4a_validation_accepts_bounded_audio_containers(
    content: bytes,
) -> None:
    assert inspect_m4a_structure(io.BytesIO(content), size_bytes=len(content)) is None


@pytest.mark.parametrize(
    ("content", "expected_code"),
    [
        (b"", "audio_empty"),
        (b"short", "m4a_header_invalid"),
        (struct.pack(">I4s", 4, b"ftyp"), "m4a_box_invalid"),
        (_iso_box(b"ftyp", b"M4A " + b"\x00" * 4)[:-1], "m4a_truncated"),
        (_iso_box(b"mdat", b"audio"), "m4a_file_type_missing"),
        (
            _iso_box(b"ftyp", b"nope" + b"\x00" * 4) + _m4a_bytes()[24:],
            "m4a_file_type_invalid",
        ),
        (
            _iso_box(b"ftyp", b"M4A " + b"\x00" * 4) + _iso_box(b"mdat", b"audio"),
            "m4a_metadata_missing",
        ),
        (_m4a_bytes(handler_type=b"vide"), "m4a_audio_track_missing"),
        (_m4a_bytes(media=b""), "m4a_media_data_missing"),
    ],
)
def test_structural_m4a_validation_rejects_invalid_content(
    content: bytes,
    expected_code: str,
) -> None:
    failure = inspect_m4a_structure(io.BytesIO(content), size_bytes=len(content))

    assert failure is not None
    assert failure.technical_code == expected_code


@pytest.mark.parametrize(
    ("content", "expected_code"),
    [
        (b"", "audio_empty"),
        (b"not a wave file", "wav_header_invalid"),
        (
            b"RIFF" + struct.pack("<I", 2_000) + b"WAVE" + b"fmt ",
            "wav_truncated",
        ),
        (
            b"RIFF" + struct.pack("<I", 4) + b"WAVE",
            "wav_format_missing",
        ),
    ],
)
def test_structural_wav_validation_rejects_invalid_content(
    content: bytes,
    expected_code: str,
) -> None:
    failure = inspect_wav_structure(io.BytesIO(content), size_bytes=len(content))

    assert failure is not None
    assert failure.technical_code == expected_code


def test_structural_ogg_validation_accepts_multiple_complete_pages() -> None:
    content = _ogg_page(b"first", header_type=0x02, sequence=0) + _ogg_page(
        b"second",
        header_type=4,
        sequence=1,
    )

    assert inspect_ogg_structure(io.BytesIO(content), size_bytes=len(content)) is None


def test_structural_ogg_validation_accepts_a_packet_continued_on_the_next_page() -> (
    None
):
    content = _ogg_page(b"a" * 255, sequence=0) + _ogg_page(
        b"packet-end",
        header_type=0x01 | 0x04,
        sequence=1,
    )

    assert inspect_ogg_structure(io.BytesIO(content), size_bytes=len(content)) is None


@pytest.mark.parametrize(
    ("content", "expected_code"),
    [
        (b"", "audio_empty"),
        (b"not an ogg", "ogg_header_invalid"),
        (b"Bad!" + _ogg_page()[4:], "ogg_header_invalid"),
        (_ogg_page(version=1), "ogg_version_invalid"),
        (_ogg_page(header_type=0x80), "ogg_header_invalid"),
        (_ogg_header(segment_count=1), "ogg_truncated"),
        (_ogg_header(segment_count=1) + b"\x05xx", "ogg_truncated"),
        (_ogg_page(b""), "audio_empty"),
        (_ogg_page(b"audio") + b"tail", "ogg_truncated"),
    ],
)
def test_structural_ogg_validation_rejects_invalid_content(
    content: bytes,
    expected_code: str,
) -> None:
    failure = inspect_ogg_structure(io.BytesIO(content), size_bytes=len(content))

    assert failure is not None
    assert failure.technical_code == expected_code


def test_structural_ogg_validation_rejects_a_bad_page_checksum() -> None:
    content = bytearray(_ogg_page(b"synthetic-audio"))
    content[-1] ^= 0x01

    failure = inspect_ogg_structure(io.BytesIO(content), size_bytes=len(content))

    assert failure is not None
    assert failure.technical_code == "ogg_checksum_invalid"


@pytest.mark.parametrize(
    ("content", "expected_code"),
    [
        (_ogg_page(b"a" * 255, header_type=0x02), "ogg_truncated"),
        (_ogg_page(b"complete", header_type=0x02), "ogg_truncated"),
        (
            _ogg_page(b"a" * 255, header_type=0x02)
            + _ogg_page(b"end", header_type=0x04),
            "ogg_continuation_invalid",
        ),
        (
            _ogg_page(b"unexpected", header_type=0x01),
            "ogg_continuation_invalid",
        ),
    ],
)
def test_structural_ogg_validation_rejects_invalid_packet_continuation(
    content: bytes,
    expected_code: str,
) -> None:
    failure = inspect_ogg_structure(io.BytesIO(content), size_bytes=len(content))

    assert failure is not None
    assert failure.technical_code == expected_code


def test_structural_wav_validation_never_classifies_local_io_as_bad_content() -> None:
    class UnavailableScratchFile(io.BytesIO):
        def seek(self, *_args: object, **_kwargs: object) -> int:
            raise OSError("synthetic local I/O failure")

    with pytest.raises(AudioPublicationError) as raised:
        inspect_wav_structure(UnavailableScratchFile(), size_bytes=128)

    assert raised.value.technical_code == "storage_unavailable"
    assert raised.value.retryable is True


@pytest.mark.parametrize("audio_extension", [AudioExtension.MP3, AudioExtension.M4A])
def test_new_format_validation_never_classifies_local_io_as_bad_content(
    audio_extension: AudioExtension,
) -> None:
    class UnavailableScratchFile(io.BytesIO):
        def seek(self, *_args: object, **_kwargs: object) -> int:
            raise OSError("synthetic local I/O failure")

    with pytest.raises(AudioPublicationError) as raised:
        inspect_audio_structure(
            UnavailableScratchFile(),
            audio_extension=audio_extension,
            size_bytes=128,
        )

    assert raised.value.technical_code == "storage_unavailable"
    assert raised.value.retryable is True


@pytest.mark.parametrize(
    ("extension", "content"),
    [
        ("wav", _wav_bytes()),
        ("ogg", _ogg_page()),
        ("mp3", _mp3_frame()),
        ("m4a", _m4a_bytes()),
    ],
)
def test_publication_is_atomic_and_identical_replay_is_idempotent(
    tmp_path: Path,
    extension: str,
    content: bytes,
) -> None:
    storage_key = f"{uuid4()}.{extension}"

    first = publish_or_confirm_audio(
        tmp_path,
        storage_key,
        io.BytesIO(content),
        size_bytes=len(content),
    )
    replay = publish_or_confirm_audio(
        tmp_path,
        storage_key,
        io.BytesIO(content),
        size_bytes=len(content),
    )

    assert first.path == replay.path
    assert replay.path.read_bytes() == content
    assert not tuple(tmp_path.glob("*.part"))


def test_publication_never_overwrites_a_different_existing_audio(
    tmp_path: Path,
) -> None:
    original = _wav_bytes(audio=b"\xd5" * 80)
    replacement = _wav_bytes(audio=b"\x55" * 80)
    storage_key = f"{uuid4()}.wav"
    destination = tmp_path / storage_key
    destination.write_bytes(original)

    with pytest.raises(AudioPublicationError) as raised:
        publish_or_confirm_audio(
            tmp_path,
            storage_key,
            io.BytesIO(replacement),
            size_bytes=len(replacement),
        )

    assert raised.value.retryable is False
    assert raised.value.technical_code == "audio_collision"
    assert destination.read_bytes() == original
