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
        "WEB_HOST",
        "WEB_PORT",
        "WEB_TRUST_PROXY_HEADERS",
        "MAX_UPLOAD_BYTES",
    ):
        monkeypatch.delenv(variable_name, raising=False)
        monkeypatch.delenv(variable_name.lower(), raising=False)

    settings = WebSettings(_env_file=None)

    assert settings.audio_pending_dir == Path("/data/audio-pending")
    assert settings.audio_completed_dir == Path("/data/audio-completed")
    assert settings.web_host == "127.0.0.1"
    assert settings.web_port == 8000
    assert settings.web_trust_proxy_headers is False
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
    monkeypatch.setenv("WEB_HOST", "0.0.0.0")
    monkeypatch.setenv("WEB_PORT", "8080")
    monkeypatch.setenv("WEB_TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "4096")

    settings = WebSettings(_env_file=None)

    assert settings.audio_pending_dir == pending_directory
    assert settings.audio_completed_dir == completed_directory
    assert settings.web_host == "0.0.0.0"
    assert settings.web_port == 8080
    assert settings.web_trust_proxy_headers is True
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


@pytest.mark.parametrize("invalid_port", [0, 65536, True])
def test_web_port_must_be_in_tcp_range(invalid_port: int | bool) -> None:
    with pytest.raises(ValidationError):
        WebSettings(web_port=invalid_port, _env_file=None)


@pytest.mark.parametrize(
    "invalid_host",
    ["", "localhost", " 127.0.0.1", "127.0.0.1\nforged-log"],
)
def test_web_host_must_be_an_ip_address(invalid_host: str) -> None:
    with pytest.raises(ValidationError):
        WebSettings(web_host=invalid_host, _env_file=None)
