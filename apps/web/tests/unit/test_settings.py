from pathlib import Path

import pytest
from pydantic import ValidationError

from glpi_ticketing_web.settings import DEFAULT_MAX_UPLOAD_BYTES, WebSettings


def test_web_settings_only_expose_authorized_default_directories(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for variable_name in (
        "AUDIO_PENDING_DIR",
        "AUDIO_COMPLETED_DIR",
        "MAX_UPLOAD_BYTES",
    ):
        monkeypatch.delenv(variable_name, raising=False)
        monkeypatch.delenv(variable_name.lower(), raising=False)

    settings = WebSettings(_env_file=None)

    assert settings.audio_pending_dir == Path("/data/audio-pending")
    assert settings.audio_completed_dir == Path("/data/audio-completed")
    assert settings.max_upload_bytes == DEFAULT_MAX_UPLOAD_BYTES
    assert not hasattr(settings, "audio_processing_dir")
    assert not hasattr(settings, "audio_failed_dir")


def test_web_settings_read_environment_overrides(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pending_directory = tmp_path / "pending"
    completed_directory = tmp_path / "completed"
    monkeypatch.setenv("AUDIO_PENDING_DIR", str(pending_directory))
    monkeypatch.setenv("AUDIO_COMPLETED_DIR", str(completed_directory))
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "4096")

    settings = WebSettings(_env_file=None)

    assert settings.audio_pending_dir == pending_directory
    assert settings.audio_completed_dir == completed_directory
    assert settings.max_upload_bytes == 4096


def test_web_audio_directories_must_be_distinct(tmp_path: Path) -> None:
    shared_directory = tmp_path / "shared"

    with pytest.raises(
        ValidationError,
        match="Web audio directories must be distinct",
    ):
        WebSettings(
            audio_pending_dir=shared_directory,
            audio_completed_dir=shared_directory / ".." / "shared",
            _env_file=None,
        )


@pytest.mark.parametrize("invalid_limit", [0, -1, True])
def test_web_upload_limit_must_be_a_positive_integer(
    invalid_limit: int | bool,
) -> None:
    with pytest.raises(ValidationError):
        WebSettings(max_upload_bytes=invalid_limit, _env_file=None)
