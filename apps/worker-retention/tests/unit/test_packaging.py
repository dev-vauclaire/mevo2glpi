"""Static packaging contracts for the retention-worker distribution."""

import tomllib
from pathlib import Path


def test_console_script_uses_the_validated_worker_name() -> None:
    application_root = Path(__file__).resolve().parents[2]
    manifest = tomllib.loads(
        (application_root / "pyproject.toml").read_text(encoding="utf-8")
    )

    assert manifest["project"]["name"] == "glpi-ticketing-worker-retention"
    assert manifest["project"]["scripts"] == {
        "glpi-ticketing-worker-retention": (
            "glpi_ticketing_worker_retention.application:main"
        )
    }
