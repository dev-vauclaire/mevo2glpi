"""Pure WAV validation and crash-safe publication in the shared volume."""

from __future__ import annotations

import errno
import hashlib
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from glpi_ticketing_common.audio_files import (
    PublishedAudio,
    publish_audio,
    resolve_audio_storage_path,
)
from glpi_ticketing_database import MailIngestionErrorScope

from ..graph import GraphAttachment, GraphAttachmentKind
from .outcomes import PublisherFailure

_WAV_EXTENSION = ".wav"
_RIFF_HEADER_SIZE = 12
_CHUNK_HEADER_SIZE = 8
_MIN_FMT_CHUNK_SIZE = 16
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


@dataclass(frozen=True, slots=True, kw_only=True)
class AttachmentValidation:
    """Either the unique WAV attachment or a safe rejection reason."""

    attachment: GraphAttachment | None = None
    failure: PublisherFailure | None = None

    def __post_init__(self) -> None:
        if self.attachment is not None and not isinstance(
            self.attachment, GraphAttachment
        ):
            raise TypeError("attachment must be a GraphAttachment or None")
        if self.failure is not None and not isinstance(self.failure, PublisherFailure):
            raise TypeError("failure must be a PublisherFailure or None")
        if self.attachment is None and self.failure is None:
            raise ValueError("attachment or failure must be provided")
        if self.attachment is not None and self.failure is None:
            return
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


def select_wav_attachment(
    attachments: tuple[GraphAttachment, ...],
    *,
    maximum_bytes: int,
) -> AttachmentValidation:
    """Require exactly one visible, non-empty WAV file under the byte limit."""
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
        return _attachment_rejection("invalid_attachment", "wav_attachment_missing")
    if len(visible) != 1:
        return _attachment_rejection("invalid_attachment", "attachment_count_invalid")

    attachment = visible[0]
    if attachment.kind is not GraphAttachmentKind.FILE:
        return _attachment_rejection(
            "invalid_attachment",
            "attachment_kind_invalid",
            attachment=attachment,
        )
    if Path(attachment.name).suffix.casefold() != _WAV_EXTENSION:
        return _attachment_rejection(
            "invalid_attachment",
            "wav_attachment_missing",
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
    return AttachmentValidation(attachment=attachment)


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
    "inspect_wav_structure",
    "publish_or_confirm_audio",
    "select_wav_attachment",
]
