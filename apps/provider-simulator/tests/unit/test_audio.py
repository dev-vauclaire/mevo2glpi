from __future__ import annotations

import io

import pytest

from glpi_ticketing_database import AudioExtension
from glpi_ticketing_provider_simulator.audio import (
    SyntheticAudioExtension,
    build_synthetic_audio,
)
from glpi_ticketing_worker_publisher.publisher.audio import inspect_audio_structure


@pytest.mark.parametrize(
    ("synthetic_extension", "publisher_extension"),
    [
        (SyntheticAudioExtension.WAV, AudioExtension.WAV),
        (SyntheticAudioExtension.OGG, AudioExtension.OGG),
    ],
)
def test_synthetic_audio_passes_the_real_publisher_inspection(
    synthetic_extension: SyntheticAudioExtension,
    publisher_extension: AudioExtension,
) -> None:
    content = build_synthetic_audio(synthetic_extension)

    failure = inspect_audio_structure(
        io.BytesIO(content),
        audio_extension=publisher_extension,
        size_bytes=len(content),
    )

    assert failure is None
