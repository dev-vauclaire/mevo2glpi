from pathlib import Path

import pytest
from pydantic import ValidationError

from glpi_ticketing_worker_automation.settings import WorkerSettings

TRANSCRIPTION_BASE_URL = "https://transcription.example.test"
TRANSCRIPTION_API_KEY = "synthetic-api-key"


@pytest.fixture(autouse=True)
def configure_required_transcription_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provide the mandatory transcription settings unless a test removes one."""
    monkeypatch.setenv("LEGACY_TRANSCRIPTION_BASE_URL", TRANSCRIPTION_BASE_URL)
    monkeypatch.setenv("LEGACY_TRANSCRIPTION_API_KEY", TRANSCRIPTION_API_KEY)


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
    transcription_api_key = "not-a-real-transcription-api-key"
    settings = WorkerSettings(
        public_base_url="http://localhost:8000",
        glpi_password=password,
        glpi_client_secret=client_secret,
        legacy_transcription_base_url="http://transcription.example.test",
        legacy_transcription_api_key=transcription_api_key,
        _env_file=None,
    )

    settings_repr = repr(settings)

    assert password not in settings_repr
    assert client_secret not in settings_repr
    assert transcription_api_key not in settings_repr


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


def test_legacy_transcription_settings_are_typed() -> None:
    settings = WorkerSettings(
        public_base_url="http://localhost:8000",
        legacy_transcription_base_url="https://transcription.example.test/prefix",
        legacy_transcription_api_key="synthetic-api-key",
        transcription_http_timeout_seconds=4.5,
        transcription_poll_interval_seconds=1.5,
        transcription_job_timeout_seconds=120,
        _env_file=None,
    )

    assert settings.legacy_transcription_base_url.path == "/prefix"
    assert (
        settings.legacy_transcription_api_key.get_secret_value() == "synthetic-api-key"
    )
    assert settings.transcription_http_timeout_seconds == 4.5
    assert settings.transcription_poll_interval_seconds == 1.5
    assert settings.transcription_job_timeout_seconds == 120


@pytest.mark.parametrize(
    ("missing_environment_variable", "missing_field"),
    [
        ("LEGACY_TRANSCRIPTION_BASE_URL", "legacy_transcription_base_url"),
        ("LEGACY_TRANSCRIPTION_API_KEY", "legacy_transcription_api_key"),
    ],
)
def test_transcription_credentials_are_required(
    monkeypatch: pytest.MonkeyPatch,
    missing_environment_variable: str,
    missing_field: str,
) -> None:
    monkeypatch.delenv(missing_environment_variable)

    with pytest.raises(ValidationError) as error_info:
        WorkerSettings(public_base_url="http://localhost:8000", _env_file=None)

    assert missing_field in {
        str(error["loc"][0]) for error in error_info.value.errors()
    }


def test_transcription_api_key_rejects_control_characters() -> None:
    with pytest.raises(ValidationError, match="control characters"):
        WorkerSettings(
            public_base_url="http://localhost:8000",
            legacy_transcription_base_url="http://transcription.example.test",
            legacy_transcription_api_key="synthetic\napi-key",
            _env_file=None,
        )


@pytest.mark.parametrize(
    "field_name",
    [
        "transcription_http_timeout_seconds",
        "transcription_poll_interval_seconds",
        "transcription_job_timeout_seconds",
    ],
)
def test_transcription_durations_must_be_positive_and_finite(
    field_name: str,
) -> None:
    for invalid_value in (0, -1, float("inf"), float("nan")):
        with pytest.raises(ValidationError):
            WorkerSettings(
                public_base_url="http://localhost:8000",
                **{field_name: invalid_value},
                _env_file=None,
            )
