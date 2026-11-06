"""Static packaging contracts for the publisher distribution."""

import tomllib
from pathlib import Path


def test_publisher_is_a_workspace_distribution() -> None:
    application_root = Path(__file__).resolve().parents[2]
    repository_root = Path(__file__).resolve().parents[4]
    application_manifest = tomllib.loads(
        (application_root / "pyproject.toml").read_text(encoding="utf-8")
    )
    workspace_manifest = tomllib.loads(
        (repository_root / "pyproject.toml").read_text(encoding="utf-8")
    )

    assert application_manifest["project"]["name"] == ("mevo2glpi-worker-publisher")
    assert (
        "apps/worker-publisher"
        in workspace_manifest["tool"]["uv"]["workspace"]["members"]
    )
    assert (
        application_root / "src" / "mevo2glpi_worker_publisher" / "__init__.py"
    ).is_file()


def test_publisher_declares_its_runtime_dependencies_and_entrypoint() -> None:
    application_root = Path(__file__).resolve().parents[2]
    manifest = tomllib.loads(
        (application_root / "pyproject.toml").read_text(encoding="utf-8")
    )

    dependencies = manifest["project"]["dependencies"]

    assert dependencies == [
        "mevo2glpi-common>=0.1,<0.2",
        "mevo2glpi-database>=0.1,<0.2",
        "httpx2>=2,<3",
        "msal>=1.35,<2",
        "pydantic-settings>=2.10,<3",
        "sqlalchemy>=2.0,<3",
    ]
    assert manifest["project"]["scripts"] == {
        "mevo2glpi-worker-publisher": ("mevo2glpi_worker_publisher.application:main"),
        "mevo2glpi-worker-publisher-replay": (
            "mevo2glpi_worker_publisher.replay.application:main"
        ),
    }
    assert manifest["tool"]["uv"]["sources"] == {
        "mevo2glpi-common": {"workspace": True},
        "mevo2glpi-database": {"workspace": True},
    }
