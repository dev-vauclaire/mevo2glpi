from pathlib import Path

import pytest
from pydantic import ValidationError

from glpi_ticketing_transcription_api.settings import TranscriptionSettings

API_KEY = "synthetic-transcription-key"


def _settings(**overrides: object) -> TranscriptionSettings:
    values: dict[str, object] = {"transcription_api_key": API_KEY}
    values.update(overrides)
    return TranscriptionSettings(**values, _env_file=None)


def test_settings_expose_safe_cpu_defaults() -> None:
    settings = _settings()

    assert settings.transcription_api_key.get_secret_value() == API_KEY
    assert settings.transcription_model_path == Path("/models/parakeet-ultra-Q8_0.gguf")
    assert settings.transcription_model_name == "parakeet-ultra"
    assert settings.transcription_cpu_threads == 2
    assert settings.transcription_host == "127.0.0.1"
    assert settings.transcription_port == 5092
    assert settings.transcription_max_upload_bytes == 25 * 1024 * 1024
    assert settings.transcription_max_audio_seconds == 600
    assert settings.transcription_ffmpeg_timeout_seconds == 60


def test_api_key_is_required_and_hidden_from_errors() -> None:
    with pytest.raises(ValidationError, match="transcription_api_key"):
        TranscriptionSettings(_env_file=None)

    secret = "too-short"
    with pytest.raises(ValidationError) as captured:
        _settings(transcription_api_key=secret)
    assert secret not in str(captured.value)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("transcription_cpu_threads", True),
        ("transcription_port", 0),
        ("transcription_port", True),
        ("transcription_max_upload_bytes", 0),
        ("transcription_max_audio_seconds", float("inf")),
        ("transcription_max_audio_seconds", True),
        ("transcription_ffmpeg_timeout_seconds", float("nan")),
    ],
)
def test_numeric_limits_reject_unsafe_values(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        _settings(**{field: value})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("transcription_model_path", "relative/model.gguf"),
        ("transcription_ffmpeg_path", "ffmpeg"),
    ],
)
def test_runtime_paths_must_be_absolute(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        _settings(**{field: value})


def test_bind_host_must_be_an_ip_address() -> None:
    with pytest.raises(ValidationError):
        _settings(transcription_host="localhost")
