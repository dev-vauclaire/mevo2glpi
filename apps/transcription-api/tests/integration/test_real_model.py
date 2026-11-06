"""Opt-in CPU smoke test for the pinned Parakeet Ultra model."""

from __future__ import annotations

import base64
import gzip
import os
import shutil
from pathlib import Path

import pytest

from mevo2glpi_transcription_api.audio import FFmpegNormalizer
from mevo2glpi_transcription_api.engines import TranscribeCppEngine

pytestmark = pytest.mark.transcription_model

FIXTURE = (
    Path(__file__).resolve().parents[4]
    / "providers"
    / "simulators"
    / "microsoft-graph-simulator"
    / "src"
    / "mevo2glpi_microsoft_graph_simulator"
    / "fixtures"
    / "valid-speech.wav.gz.b64"
)


@pytest.mark.asyncio
async def test_parakeet_ultra_transcribes_french_on_cpu() -> None:
    configured_model = os.environ.get("TRANSCRIPTION_MODEL_TEST_PATH")
    if not configured_model:
        pytest.skip("TRANSCRIPTION_MODEL_TEST_PATH is not configured")
    model_path = Path(configured_model)
    if not model_path.is_file():
        pytest.fail("TRANSCRIPTION_MODEL_TEST_PATH does not reference a file")
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.fail("FFmpeg is required for the real-model smoke test")

    encoded = FIXTURE.read_text(encoding="ascii")
    audio = gzip.decompress(base64.b64decode("".join(encoded.split()), validate=True))
    normalizer = FFmpegNormalizer(
        executable=Path(ffmpeg),
        timeout_seconds=30,
        max_audio_seconds=30,
    )
    normalized = await normalizer.normalize(audio)
    engine = TranscribeCppEngine(
        model_path=model_path,
        cpu_threads=2,
    )

    try:
        engine.load()
        output = engine.transcribe(normalized.pcm, language="fr")
    finally:
        engine.close()

    assert output.text.strip()
    assert output.language == "fr"
    assert "ceci" in output.text.casefold()
