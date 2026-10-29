"""Tests for provider-independent ticket title generation."""

from pathlib import Path

import pytest

from glpi_ticketing_worker_automation.title_generation import DefaultTitleGenerator
from glpi_ticketing_worker_automation.worker import CompletedOutcome

from ._pipeline_fakes import (
    TRANSCRIPTION,
    FakeGlpiClient,
    FakeState,
    FakeTranscriber,
    build_worker,
    transcription_result,
    write_audio,
)


def test_default_title_generator_returns_the_configured_fallback() -> None:
    generator = DefaultTitleGenerator("Demande audio")

    assert generator.generate(TRANSCRIPTION) == "Demande audio"
    assert TRANSCRIPTION not in repr(generator)


@pytest.mark.parametrize("title", ["", " padded ", "unsafe\n", "x" * 256])
def test_default_title_generator_rejects_invalid_titles(title: str) -> None:
    with pytest.raises(ValueError):
        DefaultTitleGenerator(title)


def test_ticket_stage_uses_the_injected_title_generator(tmp_path: Path) -> None:
    state = FakeState()
    audio_path = write_audio(tmp_path)
    glpi_client = FakeGlpiClient(state, [42])

    class FixedGenerator:
        def generate(self, transcription: str) -> str:
            assert transcription == TRANSCRIPTION
            return "Titre généré synthétique"

    worker = build_worker(
        tmp_path,
        state,
        FakeTranscriber(state, [transcription_result(audio_path)]),
        glpi_client,
        title_generator=FixedGenerator(),
    )

    outcome = worker.run_once()

    assert isinstance(outcome, CompletedOutcome)
    assert glpi_client.calls[0].defaults.title == "Titre généré synthétique"
