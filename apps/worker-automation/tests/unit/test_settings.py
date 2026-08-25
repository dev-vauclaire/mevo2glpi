from pathlib import Path

import pytest
from pydantic import ValidationError

from glpi_ticketing_worker_automation.settings import WorkerSettings


def test_default_audio_directories_are_distinct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for variable_name in (
        "AUDIO_PENDING_DIR",
        "AUDIO_PROCESSING_DIR",
        "AUDIO_COMPLETED_DIR",
        "AUDIO_FAILED_DIR",
    ):
        monkeypatch.delenv(variable_name, raising=False)
        monkeypatch.delenv(variable_name.lower(), raising=False)

    settings = WorkerSettings(
        public_base_url="http://localhost:8000",
        _env_file=None,
    )

    assert settings.audio_pending_dir == Path("/data/audio-pending")
    assert settings.audio_processing_dir == Path("/data/audio-processing")
    assert settings.audio_completed_dir == Path("/data/audio-completed")
    assert settings.audio_failed_dir == Path("/data/audio-failed")


def test_failed_directory_can_be_overridden(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    failed_dir = tmp_path / "failed"
    monkeypatch.setenv("AUDIO_FAILED_DIR", str(failed_dir))

    settings = WorkerSettings(
        public_base_url="http://localhost:8000",
        _env_file=None,
    )

    assert settings.audio_failed_dir == failed_dir


def test_worker_settings_read_environment_overrides(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pending_dir = tmp_path / "pending"
    processing_dir = tmp_path / "processing"
    completed_dir = tmp_path / "completed"
    failed_dir = tmp_path / "failed"
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://tickets.example.test/poc")
    monkeypatch.setenv("AUDIO_PENDING_DIR", str(pending_dir))
    monkeypatch.setenv("AUDIO_PROCESSING_DIR", str(processing_dir))
    monkeypatch.setenv("AUDIO_COMPLETED_DIR", str(completed_dir))
    monkeypatch.setenv("AUDIO_FAILED_DIR", str(failed_dir))

    settings = WorkerSettings(_env_file=None)

    assert settings.public_base_url.scheme == "https"
    assert settings.public_base_url.host == "tickets.example.test"
    assert settings.public_base_url.path == "/poc"
    assert settings.audio_pending_dir == pending_dir
    assert settings.audio_processing_dir == processing_dir
    assert settings.audio_completed_dir == completed_dir
    assert settings.audio_failed_dir == failed_dir


def test_required_public_base_url_is_validated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("public_base_url", raising=False)

    with pytest.raises(ValidationError):
        WorkerSettings(_env_file=None)

    invalid_public_base_urls = (
        "not-an-http-url",
        "ftp://tickets.example.test",
        "https://user:password@tickets.example.test",
        "https://tickets.example.test?source=glpi",
        "https://tickets.example.test#fragment",
    )
    for invalid_public_base_url in invalid_public_base_urls:
        with pytest.raises(ValidationError):
            WorkerSettings(
                public_base_url=invalid_public_base_url,
                _env_file=None,
            )

    settings = WorkerSettings(
        public_base_url="http://serveur-poc:8000",
        _env_file=None,
    )

    assert settings.public_base_url.host == "serveur-poc"


def test_secrets_are_not_in_worker_settings_repr() -> None:
    password = "not-a-real-password"
    client_secret = "not-a-real-client-secret"
    settings = WorkerSettings(
        public_base_url="http://localhost:8000",
        glpi_password=password,
        glpi_client_secret=client_secret,
        _env_file=None,
    )

    settings_repr = repr(settings)

    assert password not in settings_repr
    assert client_secret not in settings_repr


def test_worker_audio_directories_must_be_distinct(tmp_path: Path) -> None:
    shared_dir = tmp_path / "shared"

    with pytest.raises(ValidationError, match="Audio directories must be distinct"):
        WorkerSettings(
            public_base_url="http://localhost:8000",
            audio_pending_dir=tmp_path / "pending",
            audio_processing_dir=shared_dir,
            audio_completed_dir=tmp_path / "completed",
            audio_failed_dir=shared_dir / ".." / "shared",
            _env_file=None,
        )
