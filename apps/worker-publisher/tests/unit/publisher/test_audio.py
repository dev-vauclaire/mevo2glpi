from __future__ import annotations

import io
import struct
from pathlib import Path
from uuid import uuid4

import pytest

from glpi_ticketing_database import MailIngestionErrorScope
from glpi_ticketing_worker_publisher.graph import (
    AttachmentId,
    GraphAttachment,
    GraphAttachmentKind,
)
from glpi_ticketing_worker_publisher.publisher.audio import (
    AudioPublicationError,
    inspect_wav_structure,
    publish_or_confirm_audio,
    select_wav_attachment,
)

MAXIMUM_BYTES = 25 * 1024 * 1024


def _attachment(
    *,
    name: str = "voice.wav",
    size_bytes: int = 128,
    is_inline: bool = False,
    kind: GraphAttachmentKind = GraphAttachmentKind.FILE,
) -> GraphAttachment:
    return GraphAttachment(
        attachment_id=AttachmentId("attachment-safe-id"),
        name=name,
        content_type="audio/wav",
        size_bytes=size_bytes,
        is_inline=is_inline,
        kind=kind,
    )


def _wav_bytes(*, codec: int = 6, audio: bytes = b"\xd5" * 80) -> bytes:
    fmt = struct.pack("<HHIIHH", codec, 1, 8_000, 8_000, 1, 8)
    chunks = b"fmt " + struct.pack("<I", len(fmt)) + fmt
    chunks += b"data" + struct.pack("<I", len(audio)) + audio
    return b"RIFF" + struct.pack("<I", len(chunks) + 4) + b"WAVE" + chunks


def test_attachment_policy_ignores_inline_content_and_accepts_one_wav() -> None:
    inline = _attachment(name="logo.png", is_inline=True)
    wav = _attachment()

    result = select_wav_attachment((inline, wav), maximum_bytes=MAXIMUM_BYTES)

    assert result.attachment is wav
    assert result.failure is None


@pytest.mark.parametrize(
    ("attachments", "expected_code"),
    [
        ((), "wav_attachment_missing"),
        ((_attachment(name="voice.mp3"),), "wav_attachment_missing"),
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
    result = select_wav_attachment(attachments, maximum_bytes=MAXIMUM_BYTES)

    assert result.failure is not None
    assert result.failure.scope is MailIngestionErrorScope.MESSAGE
    assert result.failure.technical_code == expected_code


def test_structural_wav_validation_accepts_g711_without_decoding() -> None:
    content = _wav_bytes(codec=6)

    assert inspect_wav_structure(io.BytesIO(content), size_bytes=len(content)) is None


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


def test_structural_wav_validation_never_classifies_local_io_as_bad_content() -> None:
    class UnavailableScratchFile(io.BytesIO):
        def seek(self, *_args: object, **_kwargs: object) -> int:
            raise OSError("synthetic local I/O failure")

    with pytest.raises(AudioPublicationError) as raised:
        inspect_wav_structure(UnavailableScratchFile(), size_bytes=128)

    assert raised.value.technical_code == "storage_unavailable"
    assert raised.value.retryable is True


def test_publication_is_atomic_and_identical_replay_is_idempotent(
    tmp_path: Path,
) -> None:
    content = _wav_bytes()
    storage_key = f"{uuid4()}.wav"

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
