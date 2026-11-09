from __future__ import annotations

import wave
from io import BytesIO

import pytest

from mevo2glpi_microsoft_graph_simulator import scenarios
from mevo2glpi_microsoft_graph_simulator.audio import (
    SyntheticAudioExtension,
    SyntheticAudioFixture,
    load_synthetic_audio,
)
from mevo2glpi_microsoft_graph_simulator.scenarios import (
    MAX_SEED_AUDIO_BYTES,
    SeedScenario,
    SyntheticAttachmentKind,
    build_scenario_attachments,
    build_uploaded_audio_attachment,
)


def test_spoken_pcm_wav_fixture_is_decodable() -> None:
    content = load_synthetic_audio(SyntheticAudioFixture.WAV_PCM)

    with wave.open(BytesIO(content), "rb") as audio:
        assert audio.getnchannels() == 1
        assert audio.getsampwidth() == 2
        assert audio.getframerate() == 16_000
        assert audio.getnframes() > 16_000


def test_spoken_ogg_fixture_contains_an_opus_stream() -> None:
    content = load_synthetic_audio(SyntheticAudioFixture.OGG_OPUS)

    assert content.startswith(b"OggS")
    assert b"OpusHead" in content
    assert b"OpusTags" in content


def test_spoken_mp3_fixture_contains_an_id3_header_and_audio_frames() -> None:
    content = load_synthetic_audio(SyntheticAudioFixture.MP3)

    assert content.startswith(b"ID3")
    assert b"\xff\xf3" in content


def test_spoken_m4a_fixture_contains_an_m4a_container() -> None:
    content = load_synthetic_audio(SyntheticAudioFixture.M4A_AAC)

    assert content[4:8] == b"ftyp"
    assert b"M4A " in content[:32]


def test_spoken_telephony_fixture_is_alaw_at_8_khz() -> None:
    content = load_synthetic_audio(SyntheticAudioFixture.WAV_ALAW)

    assert content.startswith(b"RIFF")
    assert content[8:12] == b"WAVE"
    assert int.from_bytes(content[20:22], "little") == 6
    assert int.from_bytes(content[24:28], "little") == 8_000


def test_spoken_telephony_fixture_is_mulaw_at_8_khz() -> None:
    content = load_synthetic_audio(SyntheticAudioFixture.WAV_MULAW)

    assert content.startswith(b"RIFF")
    assert content[8:12] == b"WAVE"
    assert int.from_bytes(content[20:22], "little") == 7
    assert int.from_bytes(content[24:28], "little") == 8_000


def test_catalogue_exposes_the_approved_scenarios() -> None:
    assert tuple(scenario.value for scenario in SeedScenario) == (
        "valid-wav",
        "valid-ogg",
        "valid-mp3",
        "valid-m4a",
        "valid-wav-alaw",
        "valid-wav-mulaw",
        "no-attachment",
        "two-audio-attachments",
        "empty-audio",
        "unsupported-format",
        "corrupt-wav",
        "corrupt-ogg",
        "corrupt-mp3",
        "corrupt-m4a",
        "too-large",
        "valid-audio-with-inline",
        "non-file-attachment",
        "silent-wav",
    )
    assert all(scenario.description for scenario in SeedScenario)


@pytest.mark.parametrize(
    ("scenario", "expected_count"),
    [
        (SeedScenario.VALID_WAV, 1),
        (SeedScenario.VALID_OGG, 1),
        (SeedScenario.VALID_MP3, 1),
        (SeedScenario.VALID_M4A, 1),
        (SeedScenario.VALID_WAV_ALAW, 1),
        (SeedScenario.VALID_WAV_MULAW, 1),
        (SeedScenario.NO_ATTACHMENT, 0),
        (SeedScenario.TWO_AUDIO_ATTACHMENTS, 2),
        (SeedScenario.EMPTY_AUDIO, 1),
        (SeedScenario.UNSUPPORTED_FORMAT, 1),
        (SeedScenario.CORRUPT_WAV, 1),
        (SeedScenario.CORRUPT_OGG, 1),
        (SeedScenario.CORRUPT_MP3, 1),
        (SeedScenario.CORRUPT_M4A, 1),
        (SeedScenario.TOO_LARGE, 1),
        (SeedScenario.VALID_AUDIO_WITH_INLINE, 2),
        (SeedScenario.NON_FILE_ATTACHMENT, 1),
        (SeedScenario.SILENT_WAV, 1),
    ],
)
def test_each_scenario_builds_the_expected_attachment_count(
    scenario: SeedScenario,
    expected_count: int,
) -> None:
    assert len(build_scenario_attachments(scenario)) == expected_count


def test_special_scenarios_expose_the_expected_graph_metadata() -> None:
    too_large = build_scenario_attachments(SeedScenario.TOO_LARGE)[0]
    with_inline = build_scenario_attachments(SeedScenario.VALID_AUDIO_WITH_INLINE)
    non_file = build_scenario_attachments(SeedScenario.NON_FILE_ATTACHMENT)[0]

    assert too_large.size_bytes == MAX_SEED_AUDIO_BYTES + 1
    assert [item.is_inline for item in with_inline] == [False, True]
    assert non_file.kind is SyntheticAttachmentKind.ITEM


@pytest.mark.parametrize("extension", list(SyntheticAudioExtension))
def test_operator_audio_keeps_exact_bytes_without_keeping_its_original_name(
    extension: SyntheticAudioExtension,
) -> None:
    content = b"operator-owned-test-audio"

    attachment = build_uploaded_audio_attachment(extension, content)

    assert attachment.name == f"operator-audio.{extension.value}"
    assert attachment.content == content
    assert attachment.size_bytes == len(content)


def test_operator_audio_size_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scenarios, "MAX_SEED_AUDIO_BYTES", 1)

    with pytest.raises(ValueError):
        build_uploaded_audio_attachment(SyntheticAudioExtension.WAV, b"")
    with pytest.raises(ValueError):
        build_uploaded_audio_attachment(SyntheticAudioExtension.WAV, b"xx")
