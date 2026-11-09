"""Pure audio validation and crash-safe publication in the shared volume."""

from __future__ import annotations

import errno
import hashlib
import struct
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from mevo2glpi_common.audio_files import (
    PublishedAudio,
    publish_audio,
    resolve_audio_storage_path,
)
from mevo2glpi_database import AudioExtension, MailIngestionErrorScope

from ..graph import GraphAttachment, GraphAttachmentKind
from .outcomes import PublisherFailure

_PUBLISHER_AUDIO_EXTENSIONS = {
    ".m4a": AudioExtension.M4A,
    ".mp3": AudioExtension.MP3,
    ".ogg": AudioExtension.OGG,
    ".wav": AudioExtension.WAV,
}
_ID3_HEADER_SIZE = 10
_MP3_FRAME_HEADER_SIZE = 4
_MP3_FRAME_SYNC_MASK = 0xFFE00000
_MP3_FRAME_SYNC = 0xFFE00000
_MP3_MPEG1_LAYER3_BITRATES_KBPS = (
    0,
    32,
    40,
    48,
    56,
    64,
    80,
    96,
    112,
    128,
    160,
    192,
    224,
    256,
    320,
    0,
)
_MP3_MPEG2_LAYER3_BITRATES_KBPS = (
    0,
    8,
    16,
    24,
    32,
    40,
    48,
    56,
    64,
    80,
    96,
    112,
    128,
    144,
    160,
    0,
)
_MP3_SAMPLE_RATES_HZ = {
    0: (11_025, 12_000, 8_000),
    2: (22_050, 24_000, 16_000),
    3: (44_100, 48_000, 32_000),
}
_ISO_BOX_HEADER_SIZE = 8
_ISO_EXTENDED_BOX_HEADER_SIZE = 16
_M4A_BRANDS = frozenset(
    {
        b"M4A ",
        b"M4B ",
        b"isom",
        b"iso2",
        b"mp41",
        b"mp42",
        b"qt  ",
    }
)
_RIFF_HEADER_SIZE = 12
_CHUNK_HEADER_SIZE = 8
_MIN_FMT_CHUNK_SIZE = 16
_OGG_FIXED_HEADER_SIZE = 27
_OGG_CAPTURE_PATTERN = b"OggS"
_OGG_STREAM_STRUCTURE_VERSION = 0
_OGG_ALLOWED_HEADER_FLAGS = 0x07
_OGG_CONTINUED_PACKET_FLAG = 0x01
_OGG_END_OF_STREAM_FLAG = 0x04
_OGG_CHECKSUM_OFFSET = 22
_OGG_CHECKSUM_SIZE = 4
_OGG_CRC_POLYNOMIAL = 0x04C11DB7
_UINT32_MASK = 0xFFFFFFFF
_HASH_BUFFER_SIZE = 64 * 1024
_RETRYABLE_STORAGE_ERRNOS = frozenset(
    {
        errno.EAGAIN,
        errno.EBUSY,
        errno.EINTR,
        errno.EIO,
        errno.ENFILE,
        errno.EMFILE,
        errno.ENOSPC,
        getattr(errno, "EDQUOT", errno.ENOSPC),
        getattr(errno, "ESTALE", errno.EIO),
    }
)


def _build_ogg_crc_table() -> tuple[int, ...]:
    table: list[int] = []
    for index in range(256):
        remainder = index << 24
        for _ in range(8):
            if remainder & 0x80000000:
                remainder = (remainder << 1) ^ _OGG_CRC_POLYNOMIAL
            else:
                remainder <<= 1
            remainder &= _UINT32_MASK
        table.append(remainder)
    return tuple(table)


_OGG_CRC_TABLE = _build_ogg_crc_table()


@dataclass(frozen=True, slots=True, kw_only=True)
class AttachmentValidation:
    """Either the unique supported audio attachment or a rejection reason."""

    attachment: GraphAttachment | None = None
    audio_extension: AudioExtension | None = None
    failure: PublisherFailure | None = None

    def __post_init__(self) -> None:
        if self.attachment is not None and not isinstance(
            self.attachment, GraphAttachment
        ):
            raise TypeError("attachment must be a GraphAttachment or None")
        if self.failure is not None and not isinstance(self.failure, PublisherFailure):
            raise TypeError("failure must be a PublisherFailure or None")
        if self.audio_extension is not None and not isinstance(
            self.audio_extension,
            AudioExtension,
        ):
            raise TypeError("audio_extension must be an AudioExtension or None")
        if self.failure is None:
            if self.attachment is None or self.audio_extension is None:
                raise ValueError("an accepted attachment requires its audio extension")
            return
        if self.audio_extension is not None:
            raise ValueError("a rejected attachment cannot have an audio extension")
        if self.failure is not None and (
            self.failure.scope is not MailIngestionErrorScope.MESSAGE
        ):
            raise ValueError("attachment rejection must be scoped to one message")


class AudioPublicationError(RuntimeError):
    """Sanitized local-storage failure without a filesystem path."""

    def __init__(self, *, technical_code: str, retryable: bool) -> None:
        self.technical_code = technical_code
        self.retryable = retryable
        super().__init__(f"audio publication failed ({technical_code})")


def select_audio_attachment(
    attachments: tuple[GraphAttachment, ...],
    *,
    maximum_bytes: int,
) -> AttachmentValidation:
    """Require exactly one visible, non-empty supported audio under the limit."""
    if not isinstance(attachments, tuple) or any(
        not isinstance(attachment, GraphAttachment) for attachment in attachments
    ):
        raise TypeError("attachments must be a tuple of GraphAttachment values")
    if (
        isinstance(maximum_bytes, bool)
        or not isinstance(maximum_bytes, int)
        or maximum_bytes <= 0
    ):
        raise ValueError("maximum_bytes must be a positive integer")

    visible = tuple(
        attachment for attachment in attachments if not attachment.is_inline
    )
    if not visible:
        return _attachment_rejection("invalid_attachment", "audio_attachment_missing")
    if len(visible) != 1:
        return _attachment_rejection("invalid_attachment", "attachment_count_invalid")

    attachment = visible[0]
    if attachment.kind is not GraphAttachmentKind.FILE:
        return _attachment_rejection(
            "invalid_attachment",
            "attachment_kind_invalid",
            attachment=attachment,
        )
    extension = _PUBLISHER_AUDIO_EXTENSIONS.get(Path(attachment.name).suffix.casefold())
    if extension is None:
        return _attachment_rejection(
            "invalid_attachment",
            "audio_format_not_supported",
            attachment=attachment,
        )
    if attachment.size_bytes == 0:
        return _attachment_rejection(
            "invalid_audio",
            "audio_empty",
            attachment=attachment,
        )
    if attachment.size_bytes > maximum_bytes:
        return _attachment_rejection(
            "invalid_audio",
            "audio_too_large",
            attachment=attachment,
        )
    return AttachmentValidation(
        attachment=attachment,
        audio_extension=extension,
    )


def inspect_audio_structure(
    source: BinaryIO,
    *,
    audio_extension: AudioExtension,
    size_bytes: int,
) -> PublisherFailure | None:
    """Dispatch bounded structural inspection through the selected format."""
    if not isinstance(audio_extension, AudioExtension):
        raise TypeError("audio_extension must be an AudioExtension")
    if audio_extension is AudioExtension.WAV:
        return inspect_wav_structure(source, size_bytes=size_bytes)
    if audio_extension is AudioExtension.OGG:
        return inspect_ogg_structure(source, size_bytes=size_bytes)
    if audio_extension is AudioExtension.MP3:
        return inspect_mp3_structure(source, size_bytes=size_bytes)
    if audio_extension is AudioExtension.M4A:
        return inspect_m4a_structure(source, size_bytes=size_bytes)
    raise ValueError("audio_extension is not accepted by the publisher")


def inspect_mp3_structure(
    source: BinaryIO, *, size_bytes: int
) -> PublisherFailure | None:
    """Validate ID3v2 metadata and complete MPEG Layer III frames.

    The parser never decodes audio. It walks each frame exactly once, so its
    runtime is O(n frames) and its additional memory usage is O(1).
    """
    _validate_audio_source(source, size_bytes=size_bytes)
    if size_bytes <= 0:
        return _message_failure("invalid_audio", "audio_empty")
    if size_bytes < _MP3_FRAME_HEADER_SIZE:
        return _message_failure("invalid_audio", "mp3_header_invalid")

    try:
        offset = _skip_id3v2_tag(source, size_bytes=size_bytes)
        first_frame_offset = offset
        frame_count = 0
        stream_profile: tuple[int, int] | None = None
        while offset < size_bytes:
            remaining = size_bytes - offset
            if remaining == 128 and _read_at(source, offset, 3) == b"TAG":
                offset = size_bytes
                break
            if remaining < _MP3_FRAME_HEADER_SIZE:
                return _message_failure("invalid_audio", "mp3_truncated")

            header = int.from_bytes(
                _read_at(source, offset, _MP3_FRAME_HEADER_SIZE),
                byteorder="big",
            )
            frame = _mp3_frame_details(header)
            if frame is None:
                code = (
                    "mp3_frame_missing"
                    if frame_count == 0 and first_frame_offset > 0
                    else "mp3_header_invalid"
                )
                return _message_failure("invalid_audio", code)
            frame_size, version, sample_rate_hz = frame
            current_profile = (version, sample_rate_hz)
            if stream_profile is not None and current_profile != stream_profile:
                return _message_failure("invalid_audio", "mp3_header_invalid")
            if offset + frame_size > size_bytes:
                return _message_failure("invalid_audio", "mp3_truncated")
            offset += frame_size
            frame_count += 1
            stream_profile = current_profile
    except _AudioStructureError as error:
        technical_code = (
            "mp3_truncated"
            if error.technical_code == "audio_truncated"
            else error.technical_code
        )
        return _message_failure("invalid_audio", technical_code)
    except OSError as error:
        raise classify_audio_io_error(error) from None

    if frame_count == 0:
        return _message_failure("invalid_audio", "mp3_frame_missing")
    return None


def _skip_id3v2_tag(source: BinaryIO, *, size_bytes: int) -> int:
    if size_bytes < _ID3_HEADER_SIZE:
        return 0
    header = _read_at(source, 0, _ID3_HEADER_SIZE)
    if header[:3] != b"ID3":
        return 0

    major_version = header[3]
    flags = header[5]
    allowed_reserved_mask = {2: 0x3F, 3: 0x1F, 4: 0x0F}.get(major_version)
    if (
        allowed_reserved_mask is None
        or header[4] == 0xFF
        or flags & allowed_reserved_mask
        or any(value & 0x80 for value in header[6:10])
    ):
        raise _AudioStructureError("mp3_header_invalid")
    tag_size = (header[6] << 21) | (header[7] << 14) | (header[8] << 7) | header[9]
    tag_end = _ID3_HEADER_SIZE + tag_size
    if tag_end > size_bytes:
        raise _AudioStructureError("mp3_truncated")
    if major_version == 4 and flags & 0x10:
        footer_end = tag_end + _ID3_HEADER_SIZE
        if footer_end > size_bytes:
            raise _AudioStructureError("mp3_truncated")
        footer = _read_at(source, tag_end, _ID3_HEADER_SIZE)
        if footer[:3] != b"3DI" or footer[3:] != header[3:]:
            raise _AudioStructureError("mp3_header_invalid")
        return footer_end
    return tag_end


def _mp3_frame_details(header: int) -> tuple[int, int, int] | None:
    if header & _MP3_FRAME_SYNC_MASK != _MP3_FRAME_SYNC:
        return None
    version = (header >> 19) & 0x03
    layer = (header >> 17) & 0x03
    bitrate_index = (header >> 12) & 0x0F
    sample_rate_index = (header >> 10) & 0x03
    padding = (header >> 9) & 0x01
    if version == 1 or layer != 1 or sample_rate_index == 3:
        return None

    bitrate_table = (
        _MP3_MPEG1_LAYER3_BITRATES_KBPS
        if version == 3
        else _MP3_MPEG2_LAYER3_BITRATES_KBPS
    )
    bitrate_kbps = bitrate_table[bitrate_index]
    if bitrate_kbps == 0:
        return None
    sample_rate_hz = _MP3_SAMPLE_RATES_HZ[version][sample_rate_index]
    coefficient = 144 if version == 3 else 72
    frame_size = coefficient * bitrate_kbps * 1000 // sample_rate_hz + padding
    return frame_size, version, sample_rate_hz


def inspect_m4a_structure(
    source: BinaryIO, *, size_bytes: int
) -> PublisherFailure | None:
    """Validate bounded ISO-BMFF framing and require one audio track.

    Only top-level media data and the ``moov/trak/mdia/hdlr`` path are read.
    Runtime is O(n boxes), inspection is limited to three known container
    levels and additional memory usage remains O(1).
    """
    _validate_audio_source(source, size_bytes=size_bytes)
    if size_bytes <= 0:
        return _message_failure("invalid_audio", "audio_empty")
    if size_bytes < _ISO_BOX_HEADER_SIZE:
        return _message_failure("invalid_audio", "m4a_header_invalid")

    try:
        file_type_found = False
        metadata_found = False
        audio_track_found = False
        media_data_found = False
        for box_type, payload_start, box_end in _iter_iso_boxes(
            source,
            start=0,
            end=size_bytes,
        ):
            if box_type == b"ftyp":
                _validate_m4a_file_type(source, payload_start, box_end)
                file_type_found = True
            elif box_type == b"moov":
                metadata_found = True
                audio_track_found = audio_track_found or _moov_has_audio_track(
                    source,
                    payload_start,
                    box_end,
                )
            elif box_type == b"mdat" and box_end > payload_start:
                media_data_found = True
    except _AudioStructureError as error:
        technical_code = (
            "m4a_truncated"
            if error.technical_code == "audio_truncated"
            else error.technical_code
        )
        return _message_failure("invalid_audio", technical_code)
    except OSError as error:
        raise classify_audio_io_error(error) from None

    if not file_type_found:
        return _message_failure("invalid_audio", "m4a_file_type_missing")
    if not metadata_found:
        return _message_failure("invalid_audio", "m4a_metadata_missing")
    if not audio_track_found:
        return _message_failure("invalid_audio", "m4a_audio_track_missing")
    if not media_data_found:
        return _message_failure("invalid_audio", "m4a_media_data_missing")
    return None


class _AudioStructureError(ValueError):
    def __init__(self, technical_code: str) -> None:
        self.technical_code = technical_code
        super().__init__(technical_code)


def _validate_audio_source(source: BinaryIO, *, size_bytes: int) -> None:
    if not callable(getattr(source, "read", None)) or not callable(
        getattr(source, "seek", None)
    ):
        raise TypeError("source must expose read and seek")
    if isinstance(size_bytes, bool) or not isinstance(size_bytes, int):
        raise TypeError("size_bytes must be an integer")


def _read_at(source: BinaryIO, offset: int, size: int) -> bytes:
    source.seek(offset)
    content = source.read(size)
    if not isinstance(content, bytes | bytearray) or len(content) != size:
        raise _AudioStructureError("audio_truncated")
    return bytes(content)


def _iter_iso_boxes(
    source: BinaryIO,
    *,
    start: int,
    end: int,
) -> Iterator[tuple[bytes, int, int]]:
    offset = start
    while offset < end:
        if end - offset < _ISO_BOX_HEADER_SIZE:
            raise _AudioStructureError("m4a_truncated")
        header = _read_at(source, offset, _ISO_BOX_HEADER_SIZE)
        box_size = int.from_bytes(header[:4], byteorder="big")
        box_type = header[4:8]
        header_size = _ISO_BOX_HEADER_SIZE
        if box_size == 1:
            if end - offset < _ISO_EXTENDED_BOX_HEADER_SIZE:
                raise _AudioStructureError("m4a_truncated")
            box_size = int.from_bytes(
                _read_at(source, offset + _ISO_BOX_HEADER_SIZE, 8),
                byteorder="big",
            )
            header_size = _ISO_EXTENDED_BOX_HEADER_SIZE
        elif box_size == 0:
            box_size = end - offset

        if box_size < header_size:
            raise _AudioStructureError("m4a_box_invalid")
        box_end = offset + box_size
        if box_end > end:
            raise _AudioStructureError("m4a_truncated")
        yield box_type, offset + header_size, box_end
        offset = box_end


def _validate_m4a_file_type(
    source: BinaryIO,
    payload_start: int,
    box_end: int,
) -> None:
    payload_size = box_end - payload_start
    if payload_size < 8 or (payload_size - 8) % 4:
        raise _AudioStructureError("m4a_file_type_invalid")
    major_brand = _read_at(source, payload_start, 4)
    compatible_brand_found = major_brand in _M4A_BRANDS
    offset = payload_start + 8
    while offset < box_end:
        compatible_brand_found = (
            _read_at(source, offset, 4) in _M4A_BRANDS or compatible_brand_found
        )
        offset += 4
    if not compatible_brand_found:
        raise _AudioStructureError("m4a_file_type_invalid")


def _moov_has_audio_track(source: BinaryIO, start: int, end: int) -> bool:
    for box_type, payload_start, box_end in _iter_iso_boxes(
        source,
        start=start,
        end=end,
    ):
        if box_type == b"trak" and _track_is_audio(source, payload_start, box_end):
            return True
    return False


def _track_is_audio(source: BinaryIO, start: int, end: int) -> bool:
    for box_type, payload_start, box_end in _iter_iso_boxes(
        source,
        start=start,
        end=end,
    ):
        if box_type != b"mdia":
            continue
        for media_type, media_start, media_end in _iter_iso_boxes(
            source,
            start=payload_start,
            end=box_end,
        ):
            if media_type != b"hdlr":
                continue
            if media_end - media_start < 12:
                raise _AudioStructureError("m4a_audio_track_invalid")
            if _read_at(source, media_start + 8, 4) == b"soun":
                return True
    return False


def inspect_wav_structure(
    source: BinaryIO, *, size_bytes: int
) -> PublisherFailure | None:
    """Validate a bounded RIFF/WAVE container without decoding its codec.

    This accepts telephony codecs such as G.711 A-law and µ-law: only the RIFF
    structure and the presence of non-empty ``fmt `` and ``data`` chunks are
    checked. Runtime is O(n chunks) and additional memory is O(1).
    """
    if not callable(getattr(source, "read", None)) or not callable(
        getattr(source, "seek", None)
    ):
        raise TypeError("source must expose read and seek")
    if isinstance(size_bytes, bool) or not isinstance(size_bytes, int):
        raise TypeError("size_bytes must be an integer")
    if size_bytes <= 0:
        return _message_failure("invalid_audio", "audio_empty")
    if size_bytes < _RIFF_HEADER_SIZE:
        return _message_failure("invalid_audio", "wav_header_invalid")

    try:
        source.seek(0)
        header = source.read(_RIFF_HEADER_SIZE)
        if (
            not isinstance(header, bytes | bytearray)
            or len(header) != _RIFF_HEADER_SIZE
        ):
            return _message_failure("invalid_audio", "wav_header_invalid")
        riff_magic, riff_payload_size, wave_magic = struct.unpack("<4sI4s", header)
        if riff_magic != b"RIFF" or wave_magic != b"WAVE":
            return _message_failure("invalid_audio", "wav_header_invalid")

        riff_end = riff_payload_size + 8
        if riff_end > size_bytes or riff_end < _RIFF_HEADER_SIZE:
            return _message_failure("invalid_audio", "wav_truncated")

        offset = _RIFF_HEADER_SIZE
        format_found = False
        data_found = False
        while offset + _CHUNK_HEADER_SIZE <= riff_end:
            source.seek(offset)
            chunk_header = source.read(_CHUNK_HEADER_SIZE)
            if (
                not isinstance(chunk_header, bytes | bytearray)
                or len(chunk_header) != _CHUNK_HEADER_SIZE
            ):
                return _message_failure("invalid_audio", "wav_truncated")
            chunk_name, chunk_size = struct.unpack("<4sI", chunk_header)
            content_start = offset + _CHUNK_HEADER_SIZE
            content_end = content_start + chunk_size
            padded_end = content_end + (chunk_size % 2)
            if content_end > riff_end or padded_end > riff_end:
                return _message_failure("invalid_audio", "wav_truncated")

            if chunk_name == b"fmt ":
                if chunk_size < _MIN_FMT_CHUNK_SIZE:
                    return _message_failure("invalid_audio", "wav_format_invalid")
                source.seek(content_start)
                format_data = source.read(_MIN_FMT_CHUNK_SIZE)
                if (
                    not isinstance(format_data, bytes | bytearray)
                    or len(format_data) != _MIN_FMT_CHUNK_SIZE
                ):
                    return _message_failure("invalid_audio", "wav_truncated")
                audio_format, channels, sample_rate, _, block_align, _ = struct.unpack(
                    "<HHIIHH",
                    format_data,
                )
                if (
                    audio_format == 0
                    or channels == 0
                    or sample_rate == 0
                    or block_align == 0
                ):
                    return _message_failure("invalid_audio", "wav_format_invalid")
                format_found = True
            elif chunk_name == b"data":
                if chunk_size == 0:
                    return _message_failure("invalid_audio", "audio_empty")
                data_found = True
            offset = padded_end
    except OSError as error:
        # A local scratch-volume failure must never be mistaken for corrupt
        # caller content: rejecting the Exchange message would make that
        # infrastructure failure irreversible.
        raise classify_audio_io_error(error) from None
    except struct.error:
        return _message_failure("invalid_audio", "wav_structure_invalid")

    if offset != riff_end:
        return _message_failure("invalid_audio", "wav_structure_invalid")
    if not format_found:
        return _message_failure("invalid_audio", "wav_format_missing")
    if not data_found:
        return _message_failure("invalid_audio", "wav_data_missing")
    return None


def inspect_ogg_structure(
    source: BinaryIO, *, size_bytes: int
) -> PublisherFailure | None:
    """Validate bounded Ogg page framing without constraining the audio codec.

    Every page boundary, continuation marker, CRC, segment table and payload is
    checked against the downloaded size. Runtime is O(n bytes), while
    additional memory stays O(1) because an Ogg page is bounded to 65,307
    bytes by its lacing table.
    """
    if not callable(getattr(source, "read", None)) or not callable(
        getattr(source, "seek", None)
    ):
        raise TypeError("source must expose read and seek")
    if isinstance(size_bytes, bool) or not isinstance(size_bytes, int):
        raise TypeError("size_bytes must be an integer")
    if size_bytes <= 0:
        return _message_failure("invalid_audio", "audio_empty")
    if size_bytes < _OGG_FIXED_HEADER_SIZE:
        return _message_failure("invalid_audio", "ogg_header_invalid")

    try:
        source.seek(0)
        offset = 0
        payload_bytes = 0
        packet_continues = False
        last_page_ends_stream = False
        while offset < size_bytes:
            remaining = size_bytes - offset
            if remaining < _OGG_FIXED_HEADER_SIZE:
                return _message_failure("invalid_audio", "ogg_truncated")

            source.seek(offset)
            header = source.read(_OGG_FIXED_HEADER_SIZE)
            if (
                not isinstance(header, bytes | bytearray)
                or len(header) != _OGG_FIXED_HEADER_SIZE
            ):
                return _message_failure("invalid_audio", "ogg_truncated")
            if bytes(header[:4]) != _OGG_CAPTURE_PATTERN:
                return _message_failure("invalid_audio", "ogg_header_invalid")
            if header[4] != _OGG_STREAM_STRUCTURE_VERSION:
                return _message_failure("invalid_audio", "ogg_version_invalid")
            if header[5] & ~_OGG_ALLOWED_HEADER_FLAGS:
                return _message_failure("invalid_audio", "ogg_header_invalid")
            page_continues_packet = bool(header[5] & _OGG_CONTINUED_PACKET_FLAG)
            if page_continues_packet != packet_continues:
                return _message_failure(
                    "invalid_audio",
                    "ogg_continuation_invalid",
                )

            segment_count = header[26]
            segment_table = source.read(segment_count)
            if (
                not isinstance(segment_table, bytes | bytearray)
                or len(segment_table) != segment_count
            ):
                return _message_failure("invalid_audio", "ogg_truncated")

            page_payload_size = sum(segment_table)
            page_end = (
                offset + _OGG_FIXED_HEADER_SIZE + segment_count + page_payload_size
            )
            if page_end > size_bytes:
                return _message_failure("invalid_audio", "ogg_truncated")
            page_payload = source.read(page_payload_size)
            if (
                not isinstance(page_payload, bytes | bytearray)
                or len(page_payload) != page_payload_size
            ):
                return _message_failure("invalid_audio", "ogg_truncated")
            expected_checksum = int.from_bytes(
                header[
                    _OGG_CHECKSUM_OFFSET : _OGG_CHECKSUM_OFFSET + _OGG_CHECKSUM_SIZE
                ],
                byteorder="little",
            )
            if (
                _ogg_page_checksum(header, segment_table, page_payload)
                != expected_checksum
            ):
                return _message_failure("invalid_audio", "ogg_checksum_invalid")

            offset = page_end
            payload_bytes += page_payload_size
            last_page_ends_stream = bool(header[5] & _OGG_END_OF_STREAM_FLAG)
            if segment_count:
                packet_continues = segment_table[-1] == 255
    except OSError as error:
        # Scratch I/O is systemic and must never reject caller content.
        raise classify_audio_io_error(error) from None

    if packet_continues or not last_page_ends_stream:
        return _message_failure("invalid_audio", "ogg_truncated")
    if payload_bytes == 0:
        return _message_failure("invalid_audio", "audio_empty")
    return None


def _ogg_page_checksum(
    header: bytes | bytearray,
    segment_table: bytes | bytearray,
    payload: bytes | bytearray,
) -> int:
    checksum_header = bytearray(header)
    checksum_header[
        _OGG_CHECKSUM_OFFSET : _OGG_CHECKSUM_OFFSET + _OGG_CHECKSUM_SIZE
    ] = b"\x00" * _OGG_CHECKSUM_SIZE
    checksum = 0
    for part in (checksum_header, segment_table, payload):
        for octet in part:
            lookup_index = ((checksum >> 24) ^ octet) & 0xFF
            checksum = ((checksum << 8) & _UINT32_MASK) ^ _OGG_CRC_TABLE[lookup_index]
    return checksum


def publish_or_confirm_audio(
    audio_root: Path,
    storage_key: str,
    source: BinaryIO,
    *,
    size_bytes: int,
) -> PublishedAudio:
    """Publish once, or reconcile an identical file left by a prior crash."""
    try:
        source.seek(0)
        return publish_audio(audio_root, storage_key, source)
    except FileExistsError:
        try:
            existing_path = resolve_audio_storage_path(audio_root, storage_key)
            if (
                not existing_path.is_file()
                or existing_path.stat().st_size != size_bytes
            ):
                raise AudioPublicationError(
                    technical_code="audio_collision",
                    retryable=False,
                )
            source.seek(0)
            with existing_path.open("rb") as existing:
                if _stream_digest(source) != _stream_digest(existing):
                    raise AudioPublicationError(
                        technical_code="audio_collision",
                        retryable=False,
                    )
            return PublishedAudio(
                storage_key=storage_key,
                path=existing_path,
                size_bytes=size_bytes,
            )
        except AudioPublicationError:
            raise
        except OSError as error:
            raise classify_audio_io_error(error) from None
    except OSError as error:
        raise classify_audio_io_error(error) from None


def _stream_digest(source: BinaryIO) -> bytes:
    digest = hashlib.sha256()
    while True:
        chunk = source.read(_HASH_BUFFER_SIZE)
        if not isinstance(chunk, bytes | bytearray):
            raise OSError("binary source returned invalid data")
        if not chunk:
            return digest.digest()
        digest.update(chunk)


def classify_audio_io_error(error: OSError) -> AudioPublicationError:
    """Normalize a local audio I/O failure without retaining its path."""
    if not isinstance(error, OSError):
        raise TypeError("error must be an OSError")
    retryable = error.errno is None or error.errno in _RETRYABLE_STORAGE_ERRNOS
    return AudioPublicationError(
        technical_code=("storage_unavailable" if retryable else "storage_invalid"),
        retryable=retryable,
    )


def _attachment_rejection(
    category: str,
    code: str,
    *,
    attachment: GraphAttachment | None = None,
) -> AttachmentValidation:
    return AttachmentValidation(
        attachment=attachment,
        failure=_message_failure(category, code),
    )


def _message_failure(category: str, code: str) -> PublisherFailure:
    return PublisherFailure(
        scope=MailIngestionErrorScope.MESSAGE,
        category=category,
        technical_code=code,
    )


__all__ = [
    "AttachmentValidation",
    "AudioPublicationError",
    "classify_audio_io_error",
    "inspect_audio_structure",
    "inspect_m4a_structure",
    "inspect_mp3_structure",
    "inspect_ogg_structure",
    "inspect_wav_structure",
    "publish_or_confirm_audio",
    "select_audio_attachment",
]
