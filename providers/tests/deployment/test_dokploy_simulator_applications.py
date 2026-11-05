"""Validate the standalone Dokploy Dockerfile application contracts."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.deployment

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
PROVIDERS_ROOT = REPOSITORY_ROOT / "infra" / "docker" / "providers"

APPLICATIONS = {
    "microsoft-graph-simulator": {
        "alias": "gta-staging-microsoft-graph",
        "environment": {
            "MICROSOFT_GRAPH_SIMULATOR_HOST": "0.0.0.0",
            "MICROSOFT_GRAPH_SIMULATOR_PORT": "8080",
            "MICROSOFT_GRAPH_SIMULATOR_MAILBOX_ID": (
                "00000000-0000-4000-8000-000000000003"
            ),
            "MICROSOFT_GRAPH_SIMULATOR_ADMIN_TOKEN": (
                "CHANGE_ME_WITH_A_RANDOM_STAGING_TOKEN"
            ),
        },
    },
    "transcribe-ai-simulator": {
        "alias": "gta-staging-transcribe-ai",
        "environment": {
            "TRANSCRIBE_AI_SIMULATOR_HOST": "0.0.0.0",
            "TRANSCRIBE_AI_SIMULATOR_PORT": "8080",
            "TRANSCRIBE_AI_SIMULATOR_API_KEY": (
                "CHANGE_ME_WITH_A_RANDOM_STAGING_API_KEY"
            ),
        },
    },
}


def _example_environment(path: Path) -> dict[str, str]:
    source = path.read_text(encoding="utf-8")
    return dict(re.findall(r"^([A-Z][A-Z0-9_]*)=(.*)$", source, re.MULTILINE))


@pytest.mark.parametrize(("application_name", "contract"), APPLICATIONS.items())
def test_dokploy_simulator_environment_is_complete_and_synthetic(
    application_name: str,
    contract: dict[str, object],
) -> None:
    application_root = PROVIDERS_ROOT / application_name
    environment_source = (application_root / ".env.example").read_text(encoding="utf-8")

    assert (
        _example_environment(application_root / ".env.example")
        == contract["environment"]
    )
    assert "CHANGE_ME" in environment_source
    assert not re.search(
        r"(?i)(?:api[_-]?key|secret|password|token)=[0-9a-f]{32,}",
        environment_source,
    )


@pytest.mark.parametrize(("application_name", "contract"), APPLICATIONS.items())
def test_dokploy_simulator_readme_documents_runtime_contract(
    application_name: str,
    contract: dict[str, object],
) -> None:
    application_root = PROVIDERS_ROOT / application_name
    readme = (application_root / "README.md").read_text(encoding="utf-8")
    dockerfile_path = f"infra/docker/providers/{application_name}/Dockerfile"

    for required_text in (
        dockerfile_path,
        "Contexte Docker | `.`",
        "Port interne | `8080`",
        "Domaine / port public | aucun",
        "gta-staging-simulators-network",
        str(contract["alias"]),
        "overlay",
        "attachable",
        "internal",
        "UID/GID `10001`",
        "healthcheck",
        "politique de redémarrage automatique",
        "limite connue",
        "ne peuvent pas être imposées par le\nDockerfile seul",
    ):
        assert required_text in readme


def test_graph_dokploy_application_documents_read_only_fixture_mount() -> None:
    readme = (PROVIDERS_ROOT / "microsoft-graph-simulator" / "README.md").read_text(
        encoding="utf-8"
    )

    assert "/srv/gta-staging/audio-fixtures:/fixtures/audio:ro" in readme
    assert "UID/GID `10001`" in readme


@pytest.mark.parametrize("application_name", APPLICATIONS)
def test_dokploy_simulator_dockerfile_keeps_runtime_guards(
    application_name: str,
) -> None:
    dockerfile = (PROVIDERS_ROOT / application_name / "Dockerfile").read_text(
        encoding="utf-8"
    )

    assert "USER 10001:10001" in dockerfile
    assert "EXPOSE 8080/tcp" in dockerfile
    assert "HEALTHCHECK " in dockerfile
    assert "http://127.0.0.1:8080/health" in dockerfile


def test_combined_simulator_compose_remains_available_during_transition() -> None:
    assert (PROVIDERS_ROOT / "docker-compose.staging.yml").is_file()
    readme = (PROVIDERS_ROOT / "README.md").read_text(encoding="utf-8")
    normalized_readme = " ".join(readme.split())

    assert "mécanisme de retour arrière" in normalized_readme
    assert "ne doit pas être utilisé simultanément" in normalized_readme
