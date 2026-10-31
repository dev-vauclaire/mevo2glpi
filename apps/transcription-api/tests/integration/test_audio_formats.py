"""Exercise the real FFmpeg boundary with synthetic telephony fixtures."""

from __future__ import annotations

import base64
import gzip
import shutil
from pathlib import Path

import pytest

from glpi_ticketing_transcription_api.audio import (
    AudioNormalizationError,
    FFmpegNormalizer,
)

FIXTURES_ROOT = (
    Path(__file__).resolve().parents[4]
    / "providers"
    / "simulators"
    / "microsoft-graph-simulator"
    / "src"
    / "glpi_ticketing_microsoft_graph_simulator"
    / "fixtures"
)
FFMPEG = shutil.which("ffmpeg")


def _fixture(name: str) -> bytes:
    encoded = (FIXTURES_ROOT / f"{name}.gz.b64").read_text(encoding="ascii")
    return gzip.decompress(base64.b64decode("".join(encoded.split()), validate=True))


@pytest.mark.skipif(FFMPEG is None, reason="FFmpeg is not installed")
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fixture_name",
    [
        "valid-speech.wav",
        "valid-speech-alaw.wav",
        "valid-speech-mulaw.wav",
        "valid-speech.ogg",
    ],
)
async def test_real_ffmpeg_normalizes_supported_audio_formats(
    fixture_name: str,
) -> None:
    normalizer = FFmpegNormalizer(
        executable=Path(FFMPEG or "/usr/bin/ffmpeg"),
        timeout_seconds=10,
        max_audio_seconds=30,
    )

    result = await normalizer.normalize(_fixture(fixture_name))

    assert result.pcm
    assert len(result.pcm) % 4 == 0
    assert 0 < result.duration_seconds < 30


@pytest.mark.skipif(FFMPEG is None, reason="FFmpeg is not installed")
@pytest.mark.asyncio
async def test_real_ffmpeg_rejects_corrupt_audio_without_diagnostics() -> None:
    normalizer = FFmpegNormalizer(
        executable=Path(FFMPEG or "/usr/bin/ffmpeg"),
        timeout_seconds=10,
        max_audio_seconds=30,
    )

    with pytest.raises(AudioNormalizationError) as captured:
        await normalizer.normalize(b"synthetic-corrupt-audio")

    assert "ffmpeg" not in str(captured.value).casefold()
