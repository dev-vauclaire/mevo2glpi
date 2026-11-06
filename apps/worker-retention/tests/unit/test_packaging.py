"""Static packaging contracts for the retention-worker distribution."""

import tomllib
from pathlib import Path


def test_console_script_uses_the_validated_worker_name() -> None:
    application_root = Path(__file__).resolve().parents[2]
    manifest = tomllib.loads(
        (application_root / "pyproject.toml").read_text(encoding="utf-8")
    )

    assert manifest["project"]["name"] == "mevo2glpi-worker-retention"
    assert manifest["project"]["scripts"] == {
        "mevo2glpi-worker-retention": ("mevo2glpi_worker_retention.application:main")
    }


def test_retention_image_context_and_runtime_are_minimal() -> None:
    repository_root = Path(__file__).resolve().parents[4]
    dockerignore_rules = set(
        (repository_root / ".dockerignore").read_text(encoding="utf-8").splitlines()
    )
    dockerfile = (
        repository_root / "infra/docker/apps/worker-retention/Dockerfile"
    ).read_text(encoding="utf-8")
    workspace = tomllib.loads(
        (repository_root / "pyproject.toml").read_text(encoding="utf-8")
    )

    for member in workspace["tool"]["uv"]["workspace"]["members"]:
        manifest = f"{member}/pyproject.toml"
        assert f"COPY {manifest} {manifest}" in dockerfile
        assert f"!{member}/" in dockerignore_rules
        assert f"!{manifest}" in dockerignore_rules

    assert "!apps/worker-retention/src/**" in dockerignore_rules
    for package in ("common", "database", "glpi-client"):
        source = f"packages/{package}/src"
        assert f"COPY {source} {source}" in dockerfile
        assert f"!{source}/**" in dockerignore_rules

    runtime = dockerfile.split("FROM ${PYTHON_IMAGE} AS runtime", maxsplit=1)[1]
    assert "uv sync --locked --no-dev --no-editable" in dockerfile
    assert "USER 10001:10001" in runtime
    assert "COPY --from=builder /app/.venv /app/.venv" in runtime
    assert "/usr/local/bin/uv" not in runtime
    assert "COPY apps/" not in runtime
    assert "COPY packages/" not in runtime
    assert "mevo2glpi_worker_retention.runtime.healthcheck" in runtime
    assert 'CMD ["mevo2glpi-worker-retention"]' in runtime
