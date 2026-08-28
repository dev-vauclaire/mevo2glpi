from pathlib import Path

import pytest
from pydantic import ValidationError

from glpi_ticketing_worker_automation.glpi.models import TicketLevel, TicketType
from glpi_ticketing_worker_automation.settings import WorkerSettings

TRANSCRIPTION_BASE_URL = "https://transcription.example.test"
TRANSCRIPTION_API_KEY = "synthetic-api-key"
GLPI_BASE_URL = "https://glpi.example.test"
GLPI_USERNAME = "synthetic-worker"
GLPI_PASSWORD = "synthetic-password"
GLPI_CLIENT_ID = "synthetic-client-id"
GLPI_CLIENT_SECRET = "synthetic-client-secret"
GLPI_TICKET_TITLE = "Demande issue d'un message audio"
GLPI_TICKET_TYPE = "2"
GLPI_TICKET_LEVEL = "3"
DATABASE_URL = "postgresql+psycopg://worker:password@database:5432/jobs"


@pytest.fixture(autouse=True)
def configure_required_worker_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provide mandatory settings unless a test explicitly removes one."""
    monkeypatch.setenv("LEGACY_TRANSCRIPTION_BASE_URL", TRANSCRIPTION_BASE_URL)
    monkeypatch.setenv("LEGACY_TRANSCRIPTION_API_KEY", TRANSCRIPTION_API_KEY)
    monkeypatch.setenv("GLPI_BASE_URL", GLPI_BASE_URL)
    monkeypatch.setenv("GLPI_USERNAME", GLPI_USERNAME)
    monkeypatch.setenv("GLPI_PASSWORD", GLPI_PASSWORD)
    monkeypatch.setenv("GLPI_CLIENT_ID", GLPI_CLIENT_ID)
    monkeypatch.setenv("GLPI_CLIENT_SECRET", GLPI_CLIENT_SECRET)
    monkeypatch.setenv("GLPI_TICKET_TITLE", GLPI_TICKET_TITLE)
    monkeypatch.setenv("GLPI_TICKET_TYPE", GLPI_TICKET_TYPE)
    monkeypatch.setenv("GLPI_TICKET_URGENCY", GLPI_TICKET_LEVEL)
    monkeypatch.setenv("GLPI_TICKET_IMPACT", GLPI_TICKET_LEVEL)
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)


def test_worker_settings_expose_canonical_storage_and_required_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AUDIO_ROOT", raising=False)
    monkeypatch.delenv("DATABASE_URL")

    with pytest.raises(ValidationError, match="database_url"):
        WorkerSettings(public_base_url="http://localhost:8000", _env_file=None)

    settings = WorkerSettings(
        public_base_url="http://localhost:8000",
        database_url=DATABASE_URL,
        _env_file=None,
    )

    assert settings.audio_root == Path("/data/audios")
    assert settings.database_url.get_secret_value() == DATABASE_URL
    assert not hasattr(settings, "audio_pending_dir")
    assert not hasattr(settings, "audio_processing_dir")
    assert not hasattr(settings, "audio_completed_dir")
    assert not hasattr(settings, "audio_failed_dir")


def test_audio_root_can_be_overridden(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    audio_root = tmp_path / "audios"
    monkeypatch.setenv("AUDIO_ROOT", str(audio_root))

    settings = WorkerSettings(
        public_base_url="http://localhost:8000",
        _env_file=None,
    )

    assert settings.audio_root == audio_root


def test_worker_settings_read_environment_overrides(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    audio_root = tmp_path / "audios"
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://tickets.example.test/poc")
    monkeypatch.setenv("AUDIO_ROOT", str(audio_root))
    monkeypatch.setenv("DATABASE_CONNECT_TIMEOUT_SECONDS", "7")
    monkeypatch.setenv("WORKER_IDLE_INTERVAL_SECONDS", "1.25")
    monkeypatch.setenv("WORKER_RETRY_INITIAL_SECONDS", "4")
    monkeypatch.setenv("WORKER_RETRY_MAX_SECONDS", "120")
    monkeypatch.setenv("WORKER_SYSTEM_PROBE_INTERVAL_SECONDS", "45")

    settings = WorkerSettings(_env_file=None)

    assert settings.public_base_url.scheme == "https"
    assert settings.public_base_url.host == "tickets.example.test"
    assert settings.public_base_url.path == "/poc"
    assert settings.audio_root == audio_root
    assert settings.database_connect_timeout_seconds == 7
    assert settings.worker_idle_interval_seconds == 1.25
    assert settings.worker_retry_initial_seconds == 4
    assert settings.worker_retry_max_seconds == 120
    assert settings.worker_system_probe_interval_seconds == 45


def test_runner_intervals_have_safe_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for variable_name in (
        "WORKER_IDLE_INTERVAL_SECONDS",
        "WORKER_RETRY_INITIAL_SECONDS",
        "WORKER_RETRY_MAX_SECONDS",
        "WORKER_SYSTEM_PROBE_INTERVAL_SECONDS",
    ):
        monkeypatch.delenv(variable_name, raising=False)
        monkeypatch.delenv(variable_name.lower(), raising=False)

    settings = WorkerSettings(
        public_base_url="http://localhost:8000",
        _env_file=None,
    )

    assert settings.worker_idle_interval_seconds == 1.0
    assert settings.worker_retry_initial_seconds == 5.0
    assert settings.worker_retry_max_seconds == 300.0
    assert settings.worker_system_probe_interval_seconds == 60.0


def test_runner_intervals_read_environment_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WORKER_IDLE_INTERVAL_SECONDS", "1.25")
    monkeypatch.setenv("WORKER_RETRY_INITIAL_SECONDS", "3.5")
    monkeypatch.setenv("WORKER_RETRY_MAX_SECONDS", "90")
    monkeypatch.setenv("WORKER_SYSTEM_PROBE_INTERVAL_SECONDS", "12")

    settings = WorkerSettings(
        public_base_url="http://localhost:8000",
        _env_file=None,
    )

    assert settings.worker_idle_interval_seconds == 1.25
    assert settings.worker_retry_initial_seconds == 3.5
    assert settings.worker_retry_max_seconds == 90
    assert settings.worker_system_probe_interval_seconds == 12


@pytest.mark.parametrize(
    "field_name",
    [
        "worker_idle_interval_seconds",
        "worker_retry_initial_seconds",
        "worker_retry_max_seconds",
        "worker_system_probe_interval_seconds",
    ],
)
def test_runner_intervals_must_be_positive_and_finite(field_name: str) -> None:
    for invalid_value in (0, -1, float("inf"), float("nan")):
        with pytest.raises(ValidationError):
            WorkerSettings(
                public_base_url="http://localhost:8000",
                **{field_name: invalid_value},
                _env_file=None,
            )


def test_retry_initial_delay_cannot_exceed_cap() -> None:
    with pytest.raises(ValidationError, match="must not exceed"):
        WorkerSettings(
            public_base_url="http://localhost:8000",
            worker_retry_initial_seconds=301,
            worker_retry_max_seconds=300,
            _env_file=None,
        )


def test_first_retention_check_must_precede_completed_deadline() -> None:
    with pytest.raises(ValidationError, match="must precede"):
        WorkerSettings(
            public_base_url="http://localhost:8000",
            completed_retention_days=1,
            retention_first_check_hours=24,
            _env_file=None,
        )


@pytest.mark.parametrize(
    "invalid_url",
    ["", "not-a-url", "postgresql://worker:password@database/jobs", "sqlite:///jobs"],
)
def test_database_url_requires_psycopg_without_exposing_value(
    invalid_url: str,
) -> None:
    with pytest.raises(ValidationError) as captured_error:
        WorkerSettings(
            public_base_url="http://localhost:8000",
            database_url=invalid_url,
            _env_file=None,
        )

    if invalid_url:
        assert invalid_url not in str(captured_error.value)


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
    database_password = "not-a-real-database-password"
    database_url = f"postgresql+psycopg://worker:{database_password}@database:5432/jobs"
    settings = WorkerSettings(
        public_base_url="http://localhost:8000",
        database_url=database_url,
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
    assert database_password not in settings_repr


def test_glpi_settings_are_typed() -> None:
    settings = WorkerSettings(
        public_base_url="http://localhost:8000",
        glpi_base_url="https://glpi.example.test/subdirectory",
        glpi_username="worker-user",
        glpi_password="worker-password",
        glpi_client_id="worker-client",
        glpi_client_secret="worker-client-secret",
        glpi_http_timeout_seconds=4.5,
        glpi_ticket_title="Demande audio",
        glpi_ticket_type=2,
        glpi_ticket_urgency=4,
        glpi_ticket_impact=3,
        _env_file=None,
    )

    assert settings.glpi_base_url.path == "/subdirectory"
    assert settings.glpi_username == "worker-user"
    assert settings.glpi_password.get_secret_value() == "worker-password"
    assert settings.glpi_client_id == "worker-client"
    assert settings.glpi_client_secret.get_secret_value() == "worker-client-secret"
    assert settings.glpi_http_timeout_seconds == 4.5
    assert settings.glpi_ticket_title == "Demande audio"
    assert isinstance(settings.glpi_ticket_type, TicketType)
    assert int(settings.glpi_ticket_type) == 2
    assert isinstance(settings.glpi_ticket_urgency, TicketLevel)
    assert int(settings.glpi_ticket_urgency) == 4
    assert int(settings.glpi_ticket_impact) == 3


@pytest.mark.parametrize(
    ("missing_environment_variable", "missing_field"),
    [
        ("GLPI_BASE_URL", "glpi_base_url"),
        ("GLPI_USERNAME", "glpi_username"),
        ("GLPI_PASSWORD", "glpi_password"),
        ("GLPI_CLIENT_ID", "glpi_client_id"),
        ("GLPI_CLIENT_SECRET", "glpi_client_secret"),
        ("GLPI_TICKET_TITLE", "glpi_ticket_title"),
        ("GLPI_TICKET_TYPE", "glpi_ticket_type"),
        ("GLPI_TICKET_URGENCY", "glpi_ticket_urgency"),
        ("GLPI_TICKET_IMPACT", "glpi_ticket_impact"),
    ],
)
def test_glpi_settings_are_required(
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


@pytest.mark.parametrize(
    "invalid_base_url",
    [
        "not-an-http-url",
        "ftp://glpi.example.test",
        "https://user:password@glpi.example.test",
        "https://glpi.example.test?source=worker",
        "https://glpi.example.test#fragment",
    ],
)
def test_glpi_base_url_rejects_ambiguous_or_unsafe_values(
    invalid_base_url: str,
) -> None:
    with pytest.raises(ValidationError):
        WorkerSettings(
            public_base_url="http://localhost:8000",
            glpi_base_url=invalid_base_url,
            _env_file=None,
        )


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("glpi_username", ""),
        ("glpi_username", " padded"),
        ("glpi_username", "control\ncharacter"),
        ("glpi_client_id", ""),
        ("glpi_client_id", "padded "),
        ("glpi_client_id", "control\tcharacter"),
        ("glpi_ticket_title", ""),
        ("glpi_ticket_title", " padded"),
        ("glpi_ticket_title", "control\rcharacter"),
        ("glpi_ticket_title", "x" * 256),
    ],
)
def test_glpi_text_values_are_safe(field_name: str, invalid_value: str) -> None:
    with pytest.raises(ValidationError):
        WorkerSettings(
            public_base_url="http://localhost:8000",
            **{field_name: invalid_value},
            _env_file=None,
        )


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("glpi_password", ""),
        ("glpi_password", " padded"),
        ("glpi_password", "control\ncharacter"),
        ("glpi_client_secret", ""),
        ("glpi_client_secret", "padded "),
        ("glpi_client_secret", "control\tcharacter"),
    ],
)
def test_glpi_secrets_are_safe(field_name: str, invalid_value: str) -> None:
    with pytest.raises(ValidationError):
        WorkerSettings(
            public_base_url="http://localhost:8000",
            **{field_name: invalid_value},
            _env_file=None,
        )


@pytest.mark.parametrize(
    ("field_name", "invalid_values"),
    [
        ("glpi_ticket_type", (0, 3)),
        ("glpi_ticket_urgency", (0, 6)),
        ("glpi_ticket_impact", (0, 6)),
    ],
)
def test_glpi_ticket_enums_reject_values_outside_the_contract(
    field_name: str,
    invalid_values: tuple[int, int],
) -> None:
    for invalid_value in invalid_values:
        with pytest.raises(ValidationError):
            WorkerSettings(
                public_base_url="http://localhost:8000",
                **{field_name: invalid_value},
                _env_file=None,
            )


def test_glpi_http_timeout_must_be_positive_and_finite() -> None:
    for invalid_value in (0, -1, float("inf"), float("nan")):
        with pytest.raises(ValidationError):
            WorkerSettings(
                public_base_url="http://localhost:8000",
                glpi_http_timeout_seconds=invalid_value,
                _env_file=None,
            )


def test_glpi_http_timeout_defaults_to_ten_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GLPI_HTTP_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("glpi_http_timeout_seconds", raising=False)

    settings = WorkerSettings(
        public_base_url="http://localhost:8000",
        _env_file=None,
    )

    assert settings.glpi_http_timeout_seconds == 10.0


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
