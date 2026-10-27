from __future__ import annotations

import struct

import pytest

from glpi_ticketing_microsoft_graph_simulator.audio import (
    SyntheticAudioExtension,
    build_synthetic_audio,
)


def test_synthetic_wav_has_consistent_riff_length() -> None:
    content = build_synthetic_audio(SyntheticAudioExtension.WAV)

    assert content.startswith(b"RIFF")
    assert content[8:12] == b"WAVE"
    assert struct.unpack("<I", content[4:8])[0] == len(content) - 8


def test_synthetic_ogg_is_one_complete_page() -> None:
    content = build_synthetic_audio(SyntheticAudioExtension.OGG)

    assert content.startswith(b"OggS")
    assert content[5] == 0x02 | 0x04
    assert content[26] == 1
    assert content[27] == len(content) - 28


def test_unsupported_extension_is_rejected() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        build_synthetic_audio("mp3")  # type: ignore[arg-type]
