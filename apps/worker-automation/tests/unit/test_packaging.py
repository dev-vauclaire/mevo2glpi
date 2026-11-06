"""Static contracts for the automation worker runtime image context."""

import tomllib
from pathlib import Path


def test_worker_image_context_includes_its_workspace_packages() -> None:
    repository_root = Path(__file__).resolve().parents[4]
    dockerignore_rules = set(
        (repository_root / ".dockerignore").read_text(encoding="utf-8").splitlines()
    )
    dockerfile = (
        repository_root / "infra/docker/apps/worker-automation/Dockerfile"
    ).read_text(encoding="utf-8")
    workspace = tomllib.loads(
        (repository_root / "pyproject.toml").read_text(encoding="utf-8")
    )

    for member in workspace["tool"]["uv"]["workspace"]["members"]:
        manifest = f"{member}/pyproject.toml"
        assert f"COPY {manifest} {manifest}" in dockerfile
        assert f"!{member}/" in dockerignore_rules
        assert f"!{manifest}" in dockerignore_rules

    for package in ("common", "database", "glpi-client"):
        source = f"packages/{package}/src"
        assert f"COPY {source} {source}" in dockerfile
        assert f"!{source}/**" in dockerignore_rules


def test_worker_runtime_image_is_locked_minimal_and_non_root() -> None:
    repository_root = Path(__file__).resolve().parents[4]
    dockerfile = (
        repository_root / "infra/docker/apps/worker-automation/Dockerfile"
    ).read_text(encoding="utf-8")
    runtime = dockerfile.split("FROM ${PYTHON_IMAGE} AS runtime", maxsplit=1)[1]

    assert "uv sync --locked --no-dev --no-editable" in dockerfile
    assert "USER 10001:10001" in runtime
    assert "COPY --from=builder /app/.venv /app/.venv" in runtime
    assert "/usr/local/bin/uv" not in runtime
    assert "COPY apps/" not in runtime
    assert "COPY packages/" not in runtime
    assert "mevo2glpi_worker_automation.runtime.healthcheck" in runtime
    assert 'CMD ["mevo2glpi-worker-automation"]' in runtime
